"""Local durable Edge-TTS batch, preview and verified MP3 artifact pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import os
import email.utils
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import aiohttp
import edge_tts

from app.artifacts import ArtifactConflict, ArtifactError, ArtifactPublicationService
from app.database import Database, IdempotencyConflict, utc_now
from app.jobs import JobConflict, JobInputError, JobNotFound, JobService
from app.media import FFprobe
from app.translation_revisions import TranslationRevisionError, TranslationRevisionService

EDGE_TTS_VERSION = "7.3.1"
TTS_ADAPTER_VERSION = "edge-tts-python-async-v1"
TTS_SYNTHESIS_VERSION = "edge-tts-mp3-24k-mono-v1"
TTS_TEXT_POLICY_VERSION = "edge-incompatible-controls-to-space-v1"
TTS_FINGERPRINT_VERSION = 1
TTS_OUTPUT_ENCODING = "audio-24khz-48kbitrate-mono-mp3"
TTS_DEFAULT_VOICE = "vi-VN-NamMinhNeural"
TTS_CONCURRENCY = 3
TTS_MAX_ROUNDS = 3
TTS_ROUND_DELAY_SECONDS = 5.0
TTS_REQUEST_INTERVAL_SECONDS = 1.0
TTS_MAX_PREVIEW_TEXT = 4000
TTS_PREVIEW_CACHE_COUNT = 8
TTS_PREVIEW_CACHE_BYTES = 25 * 1024 * 1024
TTS_AUDIO_VERIFIER_VERSION = "ffmpeg-ffprobe-mp3-speech-v1"
TTS_AUDIO_MAX_DECODE_SECONDS = 300
_PROVIDER_SLOTS = threading.BoundedSemaphore(TTS_CONCURRENCY)


class _RequestPacer:
    """Process-wide cancellable request spacing for batch and preview calls."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait_turn(self, cancelled: Callable[[], bool]) -> None:
        with self._lock:
            delay = max(0.0, self._next_at - time.monotonic())
            if cancelled():
                raise TTSError("job_cancelled")
            until = time.monotonic() + delay
            while time.monotonic() < until:
                if cancelled():
                    raise TTSError("job_cancelled")
                time.sleep(min(0.05, until - time.monotonic()))
            if cancelled():
                raise TTSError("job_cancelled")
            self._next_at = time.monotonic() + TTS_REQUEST_INTERVAL_SECONDS


_REQUEST_PACER = _RequestPacer()


class TTSError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, review_required: bool = False,
                 retry_after_at: str | None = None, http_status: int | None = None):
        self.code = code
        self.retryable = retryable
        self.review_required = review_required
        self.retry_after_at = retry_after_at
        self.http_status = http_status
        super().__init__(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value).encode("utf-8", errors="strict")
    return hashlib.sha256(raw).hexdigest()


def _now_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


class EdgeTTSAdapter:
    """Thin provider adapter using only the edge-tts public Python API."""

    def __init__(self):
        self._voices: list[dict[str, Any]] | None = None
        self._voices_at = 0.0
        self._voices_lock = threading.Lock()

    def _slot(self, cancelled: Callable[[], bool] | None = None) -> None:
        while not _PROVIDER_SLOTS.acquire(timeout=0.1):
            if cancelled and cancelled():
                raise TTSError("job_cancelled")

    def list_voices(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._voices_lock:
            if not refresh and self._voices is not None and time.monotonic() - self._voices_at < 900:
                voices = list(self._voices)
                cached = True
            else:
                self._slot()
                try:
                    raw = asyncio.run(edge_tts.list_voices())
                except Exception as exc:
                    raise TTSError("tts_voice_catalog_unavailable", retryable=True) from exc
                finally:
                    _PROVIDER_SLOTS.release()
                voices = []
                for item in raw:
                    value = dict(item)
                    short = value.get("ShortName") or value.get("shortName")
                    locale = value.get("Locale") or value.get("locale")
                    if isinstance(short, str) and isinstance(locale, str):
                        voices.append({"short_name": short, "locale": locale,
                            "gender": value.get("Gender") or value.get("gender")})
                self._voices, self._voices_at = voices, time.monotonic()
                cached = False
        vi = [voice for voice in voices if voice["locale"].lower() == "vi-vn"]
        return {"provider": "edge", "provider_version": EDGE_TTS_VERSION, "voices": vi,
            "default_voice": TTS_DEFAULT_VOICE if any(v["short_name"] == TTS_DEFAULT_VOICE for v in vi) else None,
            "cached": cached}

    @staticmethod
    def _validate_text_integrity(text: str) -> str:
        if not isinstance(text, str) or not text.strip() or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
            raise TTSError("invalid_tts_text")
        from edge_tts.communicate import remove_incompatible_characters
        cleaned = remove_incompatible_characters(text)
        allowed = {*(range(0, 9)), 11, 12, *(range(14, 32))}
        if any(a != b and ord(a) not in allowed for a, b in zip(text, cleaned, strict=True)):
            raise TTSError("tts_text_transform_invalid")
        return cleaned

    def synthesize(self, text: str, target: Path, *, voice: str, rate: int, pitch_hz: int,
                   volume: int, cancelled: Callable[[], bool] | None = None) -> None:
        cleaned = self._validate_text_integrity(text)
        self._slot(cancelled)
        try:
            communicate = edge_tts.Communicate(cleaned, voice, rate=f"{rate:+d}%",
                pitch=f"{pitch_hz:+d}Hz", volume=f"{volume:+d}%", receive_timeout=60)
            # `Communicate.texts` is a one-shot generator. Buffer it for the
            # integrity check and restore it before save(), or synthesis writes
            # an empty file despite a successful provider response.
            text_chunks = list(communicate.texts)
            chunks = [html.unescape(bytes(item).decode("utf-8", errors="strict")) for item in text_chunks]
            # edge-tts splits at natural whitespace/UTF-8/XML boundaries; confirm that
            # all speech tokens and their order survive the library's internal chunks.
            expected_tokens = cleaned.split()
            actual_tokens = [token for chunk in chunks for token in chunk.split()]
            if expected_tokens != actual_tokens:
                raise TTSError("tts_long_text_lossless_split_failed", review_required=True)
            communicate.texts = text_chunks

            async def save_cancellable() -> None:
                task = asyncio.create_task(communicate.save(str(target)))
                try:
                    while not task.done():
                        done, _pending = await asyncio.wait({task}, timeout=0.2)
                        if cancelled and cancelled():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            raise TTSError("job_cancelled")
                        if done:
                            break
                    await task
                except asyncio.CancelledError as exc:
                    raise TTSError("job_cancelled") from exc

            timeout = min(1800, 120 + math.ceil(len(cleaned.encode("utf-8")) / 4096) * 60)
            try:
                asyncio.run(asyncio.wait_for(save_cancellable(), timeout=timeout))
            except asyncio.TimeoutError as exc:
                raise TTSError("tts_provider_timeout", retryable=True) from exc
            except aiohttp.ClientResponseError as exc:
                if exc.status == 429:
                    delay = 0.0
                    try:
                        header=exc.headers.get("Retry-After", "0")
                        try: delay=float(header)
                        except (TypeError,ValueError):
                            parsed=email.utils.parsedate_to_datetime(header)
                            delay=(parsed.astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()
                        delay=max(0.0,min(3600.0,delay))
                    except (AttributeError, OverflowError, TypeError, ValueError):
                        pass
                    raise TTSError("tts_provider_throttled", retryable=True,
                        retry_after_at=_now_after(delay) if delay else None,http_status=429) from exc
                raise TTSError("tts_provider_temporary_failure" if exc.status >= 500 else "tts_provider_rejected",
                    retryable=exc.status >= 500,http_status=exc.status) from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise TTSError("tts_network_failure", retryable=True) from exc
            except edge_tts.exceptions.NoAudioReceived as exc:
                raise TTSError("tts_no_audio_received", retryable=True) from exc
            except (edge_tts.exceptions.WebSocketError, edge_tts.exceptions.UnexpectedResponse) as exc:
                raise TTSError("tts_provider_temporary_failure", retryable=True) from exc
            except TTSError:
                raise
            except Exception as exc:
                raise TTSError("tts_provider_failure") from exc
        finally:
            _PROVIDER_SLOTS.release()


class TTSAudioValidator:
    """Validates provider MP3 and measures decoded PCM samples, not metadata duration."""

    version = TTS_AUDIO_VERIFIER_VERSION

    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = ffmpeg or os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")

    def validate(self, path: Path, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if not self.ffmpeg or not self.ffprobe:
            raise ArtifactError("tts_media_tools_unavailable")
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise ArtifactError("tts_audio_empty")
            probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries",
                "format=format_name:stream=codec_type,codec_name,sample_rate,channels", "-of", "json", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
            if probe.returncode:
                raise ArtifactError("tts_audio_corrupt")
            value = json.loads(probe.stdout.decode("utf-8", errors="strict"))
            stream = next((s for s in value.get("streams", []) if s.get("codec_type") == "audio"), None)
            container = str(value.get("format", {}).get("format_name", ""))
            if stream is None or stream.get("codec_name") != "mp3" or "mp3" not in container.split(","):
                raise ArtifactError("tts_audio_format_invalid")
            sample_rate, channels = int(stream.get("sample_rate", 0)), int(stream.get("channels", 0))
            if sample_rate != 24000 or channels != 1:
                raise ArtifactError("tts_audio_format_invalid")
        except ArtifactError:
            raise
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ArtifactError("tts_audio_corrupt") from exc

        with tempfile.TemporaryFile() as decoded:
            try:
                process = subprocess.Popen([self.ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
                    "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1", "pipe:1"],
                    stdout=decoded, stderr=subprocess.PIPE)
                deadline = time.monotonic() + TTS_AUDIO_MAX_DECODE_SECONDS
                while process.poll() is None:
                    if cancelled and cancelled():
                        process.kill()
                        process.communicate()
                        raise ArtifactError("job_cancelled")
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        raise ArtifactError("tts_audio_decode_timeout")
                    time.sleep(0.05)
                _stdout, stderr = process.communicate()
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise ArtifactError("tts_audio_decode_timeout") from exc
            except OSError as exc:
                raise ArtifactError("tts_media_tools_unavailable") from exc
            if process.returncode != 0:
                raise ArtifactError("tts_audio_decode_failed")
            decoded.seek(0)
            count = 0
            sum_squares = 0
            peak = 0
            while raw := decoded.read(128 * 1024):
                if cancelled and cancelled():
                    raise ArtifactError("job_cancelled")
                usable = len(raw) - (len(raw) % 2)
                for (sample,) in struct.iter_unpack("<h", raw[:usable]):
                    count += 1
                    sum_squares += sample * sample
                    peak = max(peak, abs(sample))
            rms = math.sqrt(sum_squares / count) if count else 0.0
        if count <= 0:
            raise ArtifactError("tts_audio_no_samples")
        # -60 dBFS speech-energy floor; this rejects encoded silence, not quiet speech.
        if peak < 32 or rms < 32:
            raise ArtifactError("tts_audio_silent")
        duration_us = count * 1_000_000 // sample_rate
        return {"container": "mp3", "codec": "mp3", "sample_rate": sample_rate,
            "channels": channels, "sample_count": count, "duration_us": duration_us,
            "non_silent": True, "provider_version": EDGE_TTS_VERSION,
            "adapter_version": TTS_ADAPTER_VERSION, "synthesis_version": TTS_SYNTHESIS_VERSION,
            "text_policy_version": TTS_TEXT_POLICY_VERSION, "verification_version": self.version}


class TTSService:
    def __init__(self, database: Database, jobs: JobService, artifacts: ArtifactPublicationService,
                 revisions: TranslationRevisionService, adapter: EdgeTTSAdapter | None = None,
                 validator: TTSAudioValidator | None = None):
        self.database, self.jobs, self.artifacts, self.revisions = database, jobs, artifacts, revisions
        self.adapter = adapter or EdgeTTSAdapter()
        self.validator = validator or TTSAudioValidator()
        self.artifacts.validators.update({"tts_audio": self.validator, "tts_preview": self.validator,
            "tts_run_manifest": TTSManifestValidator()})

    @staticmethod
    def validate_config(config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict) or set(config) - {"voice", "rate_percent", "pitch_hz", "volume_percent"}:
            raise TTSError("invalid_tts_config")
        voice = config.get("voice")
        if not isinstance(voice, str) or not 1 <= len(voice) <= 128:
            raise TTSError("invalid_tts_voice")
        values = (config.get("rate_percent", 0), config.get("pitch_hz", 0), config.get("volume_percent", 0))
        bounds = ((-50, 50), (-50, 50), (-50, 50))
        for value, (low, high) in zip(values, bounds, strict=True):
            if type(value) is not int or not low <= value <= high:
                raise TTSError("invalid_tts_config")
        return {"language": "vi-VN", "voice": voice, "rate_percent": values[0],
            "pitch_hz": values[1], "volume_percent": values[2], "output_encoding": TTS_OUTPUT_ENCODING}

    def voice_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.adapter.list_voices(refresh=refresh)

    def _ensure_voice(self, voice: str) -> None:
        catalog = self.voice_catalog()
        if not any(item["short_name"] == voice for item in catalog["voices"]):
            raise TTSError("tts_voice_unavailable")

    def _snapshot(self, project_id: str, revision_id: str, config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with closing(self.database.connect()) as connection:
            revision = connection.execute("""SELECT r.project_id,r.run_id,r.status,r.fingerprint,tr.source_asset_id,tr.transcript_revision_id
                FROM translation_revisions r JOIN translation_runs tr ON tr.project_id=r.project_id AND tr.id=r.run_id
                WHERE r.id=?""", (revision_id,)).fetchone()
            if revision is None or revision["project_id"] != project_id or revision["status"] != "ACCEPTED":
                raise TTSError("accepted_translation_revision_required")
            state = self.revisions.editor_state(project_id, revision["run_id"])
            accepted = state["accepted"]
            if (not state["run_is_current"] or accepted is None or accepted["id"] != revision_id or
                    accepted["status"] != "ACCEPTED" or not accepted["is_active"]):
                raise TTSError("translation_revision_stale")
            asset = connection.execute("""SELECT m.id,m.sha256,m.file_size,m.status,ps.generation AS source_generation,
                    ps.media_asset_id AS selected_asset FROM media_assets m LEFT JOIN project_processing_sources ps
                    ON ps.project_id=m.project_id WHERE m.project_id=? AND m.id=?""",
                (project_id, revision["source_asset_id"])).fetchone()
            active = connection.execute("SELECT generation FROM project_active_translation_revisions WHERE project_id=? AND revision_id=?",
                (project_id, revision_id)).fetchone()
            if asset is None or asset["status"] != "READY" or asset["selected_asset"] != asset["id"] or active is None:
                raise TTSError("translation_revision_stale")
        units = accepted["units"]
        if not units:
            raise TTSError("translation_coverage_invalid")
        seen_unit, seen_logical, seen_revision = set(), set(), set()
        snapshot_units = []
        for index, unit in enumerate(units):
            unit_id, logical = unit.get("unit_id"), unit.get("logical_unit_key")
            source_ids, revision_ids = unit.get("source_segment_ids"), unit.get("revision_segment_ids")
            text, source_text = unit.get("final_text"), unit.get("source_text")
            start_us, end_us = unit.get("start_us"), unit.get("end_us")
            if (type(unit.get("position")) is not int or unit.get("position") != index or not isinstance(text, str) or not text.strip() or
                    not isinstance(source_text, str) or type(start_us) is not int or type(end_us) is not int or start_us < 0 or end_us <= start_us or
                    not isinstance(unit_id, str) or not unit_id or not isinstance(logical, str) or not logical or
                    not isinstance(source_ids, list) or not source_ids or any(not isinstance(item, str) or not item for item in source_ids) or
                    not isinstance(revision_ids, list) or not revision_ids or any(not isinstance(item, str) or not item for item in revision_ids) or
                    len(set(source_ids)) != len(source_ids) or len(set(revision_ids)) != len(revision_ids) or
                    unit_id in seen_unit or logical in seen_logical or seen_revision.intersection(revision_ids)):
                raise TTSError("translation_coverage_invalid")
            seen_unit.add(unit_id); seen_logical.add(logical); seen_revision.update(revision_ids)
            text_hash = _hash(text.encode("utf-8", errors="strict"))
            synthesis_fingerprint = _hash({"fingerprint_version": TTS_FINGERPRINT_VERSION,
                "logical_unit_key": logical, "final_text_sha256": text_hash, "language": "vi-VN",
                "voice": config["voice"], "rate_percent": config["rate_percent"],
                "pitch_hz": config["pitch_hz"], "volume_percent": config["volume_percent"],
                "output_encoding": TTS_OUTPUT_ENCODING, "provider": "microsoft-edge-tts-service",
                "provider_version": EDGE_TTS_VERSION, "adapter_version": TTS_ADAPTER_VERSION,
                "synthesis_version": TTS_SYNTHESIS_VERSION, "text_policy_version": TTS_TEXT_POLICY_VERSION})
            snapshot_units.append({"unit_id": unit_id, "logical_unit_key": logical, "position": index,
                "source_segment_ids": source_ids, "revision_segment_ids": revision_ids,
                "source_text": source_text, "accepted_text": text, "text_sha256": text_hash,
                "start_us": start_us, "end_us": end_us,
                "source_window_duration_us": end_us - start_us,
                "synthesis_fingerprint": synthesis_fingerprint})
        snapshot = {"schema_version": 1, "kind": "edge_tts_batch", "project_id": project_id,
            "source_asset_id": asset["id"], "source_sha256": asset["sha256"], "source_size": asset["file_size"],
            "source_generation": asset["source_generation"], "translation_run_id": revision["run_id"],
            "translation_revision_id": revision_id, "revision_fingerprint": revision["fingerprint"],
            "translation_revision_generation": active["generation"], "config": config,
            "provider": "microsoft-edge-tts-service", "provider_version": EDGE_TTS_VERSION,
            "adapter_version": TTS_ADAPTER_VERSION, "synthesis_version": TTS_SYNTHESIS_VERSION,
            "text_policy_version": TTS_TEXT_POLICY_VERSION, "fingerprint_version": TTS_FINGERPRINT_VERSION,
            "unit_count": len(snapshot_units), "units_sha256": _hash(snapshot_units)}
        return snapshot, snapshot_units

    def create_batch(self, project_id: str, revision_id: str, config_raw: dict[str, Any], consent: bool,
                     idempotency_key: str) -> dict[str, Any]:
        if consent is not True:
            raise TTSError("edge_tts_consent_required")
        try:
            project_id = str(uuid.UUID(project_id)); revision_id = str(uuid.UUID(revision_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TTSError("invalid_tts_id") from exc
        config = self.validate_config(config_raw)
        self._ensure_voice(config["voice"])
        snapshot, units = self._snapshot(project_id, revision_id, config)
        cached_by_fingerprint: dict[str, dict[str, Any]] = {}
        for unit in units:
            candidate = self._find_cached_artifact(project_id, unit["synthesis_fingerprint"])
            if candidate:
                cached_by_fingerprint[unit["synthesis_fingerprint"]] = candidate
        proposed_run_id = str(uuid.uuid4())
        now = utc_now()

        def create_rows(connection: sqlite3.Connection, job_id: str) -> None:
            job = connection.execute("SELECT expected_input_generation,expected_pointer_generation FROM jobs WHERE id=?", (job_id,)).fetchone()
            connection.execute("""INSERT INTO tts_runs(id,project_id,source_asset_id,translation_run_id,translation_revision_id,
                producer_job_id,input_fingerprint,revision_fingerprint,config_json,provider_version,adapter_version,synthesis_version,
                expected_input_generation,expected_pointer_generation,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?)""", (proposed_run_id,project_id,snapshot["source_asset_id"],
                snapshot["translation_run_id"],revision_id,job_id,_hash(snapshot),snapshot["revision_fingerprint"],
                _canonical(config),EDGE_TTS_VERSION,TTS_ADAPTER_VERSION,TTS_SYNTHESIS_VERSION,
                job["expected_input_generation"],job["expected_pointer_generation"],now))
            for unit in units:
                connection.execute("""INSERT INTO tts_run_units(project_id,tts_run_id,unit_id,logical_unit_key,position,
                    source_segment_ids_json,revision_segment_ids_json,source_text,accepted_text,text_sha256,start_us,end_us,
                    source_window_duration_us,synthesis_fingerprint,status,artifact_id,cache_hit,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?)""", (project_id,proposed_run_id,unit["unit_id"],unit["logical_unit_key"],
                    unit["position"],_canonical(unit["source_segment_ids"]),_canonical(unit["revision_segment_ids"]),
                    unit["source_text"],unit["accepted_text"],unit["text_sha256"],unit["start_us"],unit["end_us"],
                    unit["source_window_duration_us"],unit["synthesis_fingerprint"],
                    "READY" if unit["synthesis_fingerprint"] in cached_by_fingerprint else "QUEUED",
                    cached_by_fingerprint.get(unit["synthesis_fingerprint"],{}).get("id"),
                    int(unit["synthesis_fingerprint"] in cached_by_fingerprint),now))

        try:
            job = self.jobs.create_job(project_id,"edge_tts_batch",snapshot,idempotency_key,
                activation_stage="tts",update_stage_input="tts",creation_hook=create_rows)
        except IdempotencyConflict:
            raise
        except JobConflict:
            raise
        # On idempotent replay JobService does not invoke creation_hook.
        with closing(self.database.connect()) as connection:
            persisted = connection.execute("SELECT id FROM tts_runs WHERE producer_job_id=? AND project_id=?",
                (job["id"], project_id)).fetchone()
        if persisted is None:
            raise TTSError("tts_run_persistence_failed")
        run_id = persisted["id"]
        return {"run_id":run_id,"job":job,"status":self.get_run(project_id,run_id)}

    def _find_cached_artifact(self, project_id: str, synthesis_fingerprint: str) -> dict[str, Any] | None:
        with closing(self.database.connect()) as connection:
            candidates = connection.execute("""SELECT u.artifact_id FROM tts_run_units u
                JOIN artifacts a ON a.project_id=u.project_id AND a.id=u.artifact_id
                WHERE u.project_id=? AND u.synthesis_fingerprint=? AND u.status='READY' AND a.kind='tts_audio' AND a.status='READY'
                ORDER BY a.published_at DESC LIMIT 4""", (project_id,synthesis_fingerprint)).fetchall()
        for candidate in candidates:
            try:
                artifact = self.artifacts.verify_ready(candidate["artifact_id"],project_id)
                if artifact.get("content_fingerprint") == synthesis_fingerprint:
                    return artifact
            except ArtifactError:
                continue
        return None

    def _run_is_current(self, project_id: str, run_id: str) -> bool:
        with closing(self.database.connect()) as connection:
            run = connection.execute("SELECT translation_revision_id,source_asset_id FROM tts_runs WHERE project_id=? AND id=?",(project_id,run_id)).fetchone()
            if run is None:
                return False
            row = connection.execute("""SELECT ar.status AS revision_status,ap.revision_id AS active_revision,
                ps.media_asset_id AS selected_asset,m.status AS media_status,tr.status AS translation_status,
                tr.artifact_id AS translation_artifact,sp.active_artifact_id AS active_translation_artifact,
                tj.status AS translation_job_status,tj.input_fingerprint AS translation_job_fingerprint,
                sp.input_fingerprint AS active_translation_fingerprint
                FROM translation_revisions ar JOIN translation_runs tr ON tr.id=ar.run_id AND tr.project_id=ar.project_id
                JOIN jobs tj ON tj.id=tr.producer_job_id AND tj.project_id=tr.project_id
                LEFT JOIN project_active_translation_revisions ap ON ap.project_id=ar.project_id
                LEFT JOIN project_processing_sources ps ON ps.project_id=ar.project_id
                LEFT JOIN media_assets m ON m.project_id=ar.project_id AND m.id=?
                LEFT JOIN stage_pointers sp ON sp.project_id=ar.project_id AND sp.stage='translation'
                WHERE ar.project_id=? AND ar.id=?""",(run["source_asset_id"],project_id,run["translation_revision_id"])).fetchone()
        return bool(row and row["revision_status"]=="ACCEPTED" and row["active_revision"]==run["translation_revision_id"]
            and row["selected_asset"]==run["source_asset_id"] and row["media_status"]=="READY"
            and row["translation_status"]=="READY" and row["translation_artifact"]==row["active_translation_artifact"]
            and row["translation_job_status"]=="READY" and row["translation_job_fingerprint"]==row["active_translation_fingerprint"])

    def _reconcile_unit_artifacts(self, project_id: str, run_id: str) -> None:
        with closing(self.database.connect()) as connection:
            pending=connection.execute("SELECT unit_id,synthesis_fingerprint FROM tts_run_units WHERE tts_run_id=? AND status='QUEUED' ORDER BY position",(run_id,)).fetchall()
        for unit in pending:
            cached=self._find_cached_artifact(project_id,unit["synthesis_fingerprint"])
            if cached:
                with closing(self.database.connect()) as connection:
                    connection.execute("UPDATE tts_run_units SET status='READY',artifact_id=?,cache_hit=1,retryable=0,error_code=NULL,updated_at=? WHERE tts_run_id=? AND unit_id=? AND status='QUEUED'",
                        (cached["id"],utc_now(),run_id,unit["unit_id"]))

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM tts_runs WHERE project_id=? AND id=?",(project_id,run_id)).fetchone()
            if row is None:
                raise JobNotFound
            units = connection.execute("""SELECT u.*,a.sha256,a.file_size,a.verification_metadata_json,a.status AS artifact_status
                FROM tts_run_units u LEFT JOIN artifacts a ON a.project_id=u.project_id AND a.id=u.artifact_id
                WHERE u.tts_run_id=? ORDER BY u.position""",(run_id,)).fetchall()
            pointer = connection.execute("SELECT active_artifact_id FROM stage_pointers WHERE project_id=? AND stage='tts'",(project_id,)).fetchone()
            unit_values=[]
            ready=reused=running=queued=failed=review=cancelled=0
            for unit in units:
                value=dict(unit)
                value["source_segment_ids"]=json.loads(value.pop("source_segment_ids_json"))
                value["revision_segment_ids"]=json.loads(value.pop("revision_segment_ids_json"))
                value["artifact"] = None
                if value.get("artifact_id") and value.get("artifact_status")=="READY":
                    meta=json.loads(value.pop("verification_metadata_json") or "{}")
                    value["artifact"]={"id":value["artifact_id"],"sha256":value["sha256"],"file_size":value["file_size"],**meta}
                else:
                    value.pop("verification_metadata_json",None)
                value.pop("sha256",None); value.pop("file_size",None); value.pop("artifact_status",None)
                unit_values.append(value)
                ready += value["status"]=="READY"; reused += value["cache_hit"]==1
                running += value["status"]=="RUNNING"; queued += value["status"]=="QUEUED"
                failed += value["status"]=="FAILED"; review += value["status"]=="REVIEW_REQUIRED"
                cancelled += value["status"]=="CANCELLED"
            value=dict(row)
            value["config"]=json.loads(value.pop("config_json"))
            value["units"]=unit_values
            value["progress"]={"total_units":len(unit_values),"ready_units":ready,"reused_units":reused,
                "running_units":running,"queued_units":queued,"failed_units":failed,"review_required_units":review,
                "cancelled_units":cancelled}
            value["is_active"]=bool(pointer and pointer["active_artifact_id"] and pointer["active_artifact_id"]==value["manifest_artifact_id"] and value["status"]=="READY")
            job_detail=self.jobs.get_job(value["producer_job_id"],project_id)
            value["retry_available"]=job_detail["retry_available"]
            value["retry_block_reason"]=job_detail["retry_block_reason"]
            value["job_progress"]=job_detail["progress"]
            return value

    def latest_run(self, project_id: str) -> dict[str, Any] | None:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT id FROM tts_runs WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 1",(project_id,)).fetchone()
        return self.get_run(project_id,row["id"]) if row else None

    def active_preview(self, project_id:str)->dict[str,Any]|None:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT id,status,input_snapshot FROM jobs WHERE project_id=? AND kind='edge_tts_preview' AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED') ORDER BY created_at DESC LIMIT 1",(project_id,)).fetchone()
        if not row:return None
        snapshot=json.loads(row["input_snapshot"])
        return {"job_id":row["id"],"status":row["status"],"config":snapshot.get("config",{})}

    def reconcile_cancelled_job(self, job_id: str) -> None:
        """Close queued TTS units when the shared Job API cancels before claim."""
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT r.id,j.status FROM tts_runs r JOIN jobs j ON j.id=r.producer_job_id WHERE j.id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "CANCELLED":
                return
            connection.execute("UPDATE tts_run_units SET status='CANCELLED',updated_at=? WHERE tts_run_id=? AND status='QUEUED'", (utc_now(), row["id"]))
            connection.execute("UPDATE tts_runs SET status='CANCELLED',completed_at=COALESCE(completed_at,?) WHERE id=?", (utc_now(), row["id"]))

    def audio_artifact(self, project_id: str, run_id: str, unit_id: str) -> dict[str, Any]:
        with closing(self.database.connect()) as connection:
            row=connection.execute("""SELECT u.status,u.artifact_id FROM tts_run_units u JOIN tts_runs r ON r.id=u.tts_run_id
                WHERE r.project_id=? AND r.id=? AND u.unit_id=?""",(project_id,run_id,unit_id)).fetchone()
        if row is None or row["status"]!="READY" or not row["artifact_id"]:
            raise TTSError("tts_audio_not_ready")
        return self.artifacts.verify_ready(row["artifact_id"],project_id)

    def open_audio(self, project_id: str, artifact_id: str):
        artifact=self.artifacts.get_artifact(artifact_id,project_id)
        if artifact["kind"] not in {"tts_audio","tts_preview"} or artifact["status"]!="READY":
            raise TTSError("tts_audio_not_ready")
        with closing(self.database.connect()) as connection:
            linked=connection.execute("""SELECT 1 FROM tts_run_units WHERE project_id=? AND artifact_id=? AND status='READY'
                UNION ALL SELECT 1 FROM tts_preview_cache WHERE project_id=? AND artifact_id=? LIMIT 1""",
                (project_id,artifact_id,project_id,artifact_id)).fetchone()
        if linked is None:
            raise TTSError("tts_audio_not_ready")
        artifact=self.artifacts.verify_ready(artifact_id,project_id)
        path=self.artifacts._path(artifact["managed_relative_path"])
        info=path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_size!=artifact["file_size"]:
            raise TTSError("tts_audio_not_ready")
        handle=path.open("rb")
        opened=os.fstat(handle.fileno())
        if (opened.st_dev,opened.st_ino,opened.st_size)!=(info.st_dev,info.st_ino,info.st_size):
            handle.close()
            raise TTSError("tts_audio_not_ready")
        return handle,artifact["file_size"],artifact["sha256"]

    def preview_result(self, project_id: str, job_id: str) -> dict[str, Any]:
        job=self.jobs.get_job(job_id,project_id)
        artifact=None
        if job["status"]=="READY":
            with closing(self.database.connect()) as connection:
                row=connection.execute("SELECT id FROM artifacts WHERE producer_job_id=? AND project_id=? AND kind='tts_preview' AND status='READY'",(job_id,project_id)).fetchone()
            if row:
                try: artifact=self.artifacts.verify_ready(row["id"],project_id)
                except ArtifactError: artifact=None
        return {"job":job,"artifact_id":artifact["id"] if artifact else None}

    def create_preview(self, project_id: str, text: str, config_raw: dict[str, Any], consent: bool,
                       idempotency_key: str) -> dict[str, Any]:
        if consent is not True:
            raise TTSError("edge_tts_consent_required")
        if not isinstance(text,str) or not text.strip() or len(text)>TTS_MAX_PREVIEW_TEXT:
            raise TTSError("invalid_tts_preview_text")
        config=self.validate_config(config_raw)
        self._ensure_voice(config["voice"])
        preview_fp=_hash({"text":text,"config":config,"provider_version":EDGE_TTS_VERSION,
            "adapter_version":TTS_ADAPTER_VERSION,"synthesis_version":TTS_SYNTHESIS_VERSION,
            "text_policy_version":TTS_TEXT_POLICY_VERSION})
        with closing(self.database.connect()) as connection:
            cached=connection.execute("SELECT artifact_id FROM tts_preview_cache WHERE project_id=? AND fingerprint=?",(project_id,preview_fp)).fetchone()
        if cached:
            try:
                artifact=self.artifacts.verify_ready(cached["artifact_id"],project_id)
                with closing(self.database.connect()) as connection:
                    connection.execute("UPDATE tts_preview_cache SET last_used_at=? WHERE fingerprint=?",(utc_now(),preview_fp))
                return {"cached":True,"artifact_id":artifact["id"],"status":"READY"}
            except ArtifactError:
                pass
        snapshot={"schema_version":1,"kind":"edge_tts_preview","project_id":project_id,
            "text":text,"text_sha256":_hash(text.encode("utf-8")),"config":config,
            "provider_version":EDGE_TTS_VERSION,"adapter_version":TTS_ADAPTER_VERSION,
            "synthesis_version":TTS_SYNTHESIS_VERSION,"preview_fingerprint":preview_fp}
        input_fingerprint = _hash(snapshot)
        with closing(self.database.connect()) as connection:
            active = connection.execute("SELECT id,status FROM jobs WHERE project_id=? AND kind='edge_tts_preview' AND input_fingerprint=? AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED') ORDER BY created_at LIMIT 1",
                (project_id,input_fingerprint)).fetchone()
        if active:
            return {"cached":False,"job":self.jobs.get_job(active["id"],project_id),"status":active["status"]}
        job=self.jobs.create_job(project_id,"edge_tts_preview",snapshot,idempotency_key)
        return {"cached":False,"job":job,"status":job["status"]}

    def prune_preview_cache(self) -> None:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("""SELECT c.fingerprint,c.artifact_id,a.managed_relative_path,a.file_size FROM tts_preview_cache c
                JOIN artifacts a ON a.id=c.artifact_id WHERE c.project_id=(SELECT project_id FROM artifacts WHERE id=c.artifact_id)
                ORDER BY c.last_used_at DESC,c.fingerprint""").fetchall()
        total=sum(row["file_size"] or 0 for row in rows)
        for index,row in reversed(list(enumerate(rows))):
            if index<TTS_PREVIEW_CACHE_COUNT and total<=TTS_PREVIEW_CACHE_BYTES: continue
            with closing(self.database.connect()) as connection:
                refs=connection.execute("SELECT COUNT(*) FROM tts_run_units WHERE artifact_id=?",(row["artifact_id"],)).fetchone()[0]
                artifact=connection.execute("SELECT status,kind FROM artifacts WHERE id=?",(row["artifact_id"],)).fetchone()
            if refs or not artifact or artifact["kind"]!="tts_preview" or artifact["status"] not in {"READY","MISSING","CORRUPT"}:
                continue
            try:
                path=self.artifacts._path(row["managed_relative_path"])
                path.unlink(missing_ok=True)
                with closing(self.database.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM tts_preview_cache WHERE fingerprint=?",(row["fingerprint"],))
                    connection.execute("DELETE FROM artifacts WHERE id=? AND kind='tts_preview'",(row["artifact_id"],))
                    connection.commit()
                total-=row["file_size"] or 0
            except (OSError,sqlite3.Error):
                continue


class TTSManifestValidator:
    version="tts-run-manifest-v1"
    def validate(self,path:Path)->dict[str,Any]:
        try:
            value=json.loads(path.read_text(encoding="utf-8"))
            if (value.get("schema_version")!=1 or not isinstance(value.get("tts_run_id"),str)
                    or not isinstance(value.get("units"),list) or len(value["units"])>10000):
                raise ValueError
            ids=[item.get("unit_id") for item in value["units"]]
            if any(not isinstance(item,str) for item in ids) or len(ids)!=len(set(ids)):
                raise ValueError
            return {"artifact_type":"tts_run_manifest","schema_version":1,
                "tts_run_id":value["tts_run_id"],"unit_count":len(ids),"manifest_fingerprint":value["input_fingerprint"]}
        except (OSError,ValueError,TypeError,AttributeError,json.JSONDecodeError) as exc:
            raise ArtifactError("tts_manifest_invalid") from exc


class TTSWorker:
    def __init__(self, service:TTSService):
        self.service=service; self.database=service.database; self.jobs=service.jobs
        self.artifacts=service.artifacts; self.adapter=service.adapter
        self.stop_event=threading.Event(); self.thread:threading.Thread|None=None
        self.active_lock=threading.Lock(); self.active_job_id:str|None=None

    def start(self)->None:
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear(); self.thread=threading.Thread(target=self._loop,name="autodub-edge-tts",daemon=True); self.thread.start()

    def stop(self)->None:
        self.stop_event.set()
        with self.active_lock: job_id=self.active_job_id
        if job_id:
            try: self.jobs.request_cancel(job_id,f"tts-shutdown-{uuid.uuid4().hex}")
            except Exception: pass
        if self.thread: self.thread.join(timeout=8)

    def _loop(self)->None:
        while not self.stop_event.is_set():
            try: worked=self.run_once()
            except Exception: worked=False
            if not worked: self.stop_event.wait(0.25)

    def _cancelled(self,job_id:str)->bool:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
        return row is None or row["status"]!="RUNNING" or self.stop_event.is_set()

    def run_once(self)->bool:
        job=self.jobs.claim_next_job(kind="edge_tts_batch")
        if job:
            with self.active_lock:self.active_job_id=job["id"]
            try:self._run_batch(job)
            finally:
                with self.active_lock:self.active_job_id=None
            return True
        job=self.jobs.claim_next_job(kind="edge_tts_preview")
        if not job:return False
        with self.active_lock:self.active_job_id=job["id"]
        try:self._run_preview(job)
        finally:
            with self.active_lock:self.active_job_id=None
        return True

    def _run_preview(self,job:dict[str,Any])->None:
        with closing(self.database.connect()) as connection:
            source=connection.execute("SELECT input_snapshot FROM jobs WHERE id=?",(job["id"],)).fetchone()
            attempt=connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'",(job["id"],)).fetchone()
        if not source or not attempt:return
        snapshot=json.loads(source["input_snapshot"]); attempt_id=attempt["id"]
        path=self.database.data_root/"staging"/"tts"/f"preview-{job['id']}-{attempt_id}.mp3"
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            if self._cancelled(job["id"]):raise TTSError("job_cancelled")
            config=snapshot["config"]
            for call_no in range(3):
                _REQUEST_PACER.wait_turn(lambda:self._cancelled(job["id"]))
                call_id=str(uuid.uuid4());call_started=time.monotonic();call_stamp=utc_now()
                with closing(self.database.connect()) as connection:
                    connection.execute("INSERT INTO tts_provider_calls(id,project_id,job_attempt_id,call_kind,round_no,call_no,status,started_at) VALUES(?,?,?,'preview',?,?,'RUNNING',?)",(call_id,job["project_id"],attempt_id,call_no+1,call_no+1,call_stamp))
                try:
                    self.adapter.synthesize(snapshot["text"],path,voice=config["voice"],rate=config["rate_percent"],
                        pitch_hz=config["pitch_hz"],volume=config["volume_percent"],cancelled=lambda:self._cancelled(job["id"]))
                    with closing(self.database.connect()) as connection:
                        connection.execute("UPDATE tts_provider_calls SET status='READY',completed_at=?,elapsed_ms=? WHERE id=?",(utc_now(),int((time.monotonic()-call_started)*1000),call_id))
                    break
                except TTSError as exc:
                    with closing(self.database.connect()) as connection:
                        connection.execute("UPDATE tts_provider_calls SET status=?,completed_at=?,elapsed_ms=?,error_code=?,exception_class=?,http_status=?,retry_after=? WHERE id=?",("CANCELLED" if exc.code=="job_cancelled" else "FAILED",utc_now(),int((time.monotonic()-call_started)*1000),exc.code,type(exc).__name__,exc.http_status,exc.retry_after_at,call_id))
                    if not exc.retryable or call_no>=2: raise
                    wait_seconds=TTS_ROUND_DELAY_SECONDS
                    if exc.retry_after_at:
                        try:wait_seconds=max(wait_seconds,min(3600,(datetime.fromisoformat(exc.retry_after_at.replace("Z","+00:00"))-datetime.now(timezone.utc)).total_seconds()))
                        except (ValueError,TypeError):pass
                    deadline=time.monotonic()+wait_seconds
                    while time.monotonic()<deadline and not self._cancelled(job["id"]): time.sleep(min(0.1,deadline-time.monotonic()))
                    if self._cancelled(job["id"]): raise TTSError("job_cancelled")
            with path.open("rb") as preview_audio:
                artifact=self.artifacts.publish(project_id=job["project_id"],job_id=job["id"],attempt_id=attempt_id,
                    kind="tts_preview",input_fingerprint=job["input_fingerprint"],content_fingerprint=snapshot["preview_fingerprint"],
                    fingerprint_version=TTS_FINGERPRINT_VERSION,source=preview_audio,
                    validator_cancelled=lambda:self._cancelled(job["id"]))
            with closing(self.database.connect()) as connection:
                connection.execute("INSERT OR REPLACE INTO tts_preview_cache(fingerprint,project_id,artifact_id,text_sha256,created_at,last_used_at) VALUES(?,?,?,?,?,?)",
                    (snapshot["preview_fingerprint"],job["project_id"],artifact["id"],snapshot["text_sha256"],utc_now(),utc_now()))
            evidence=self.artifacts.completion_evidence(job["id"],attempt_id,[artifact["id"]])
            self.jobs.complete_attempt(job["id"],attempt_id,evidence)
            self.service.prune_preview_cache()
        except TTSError as exc:
            try:self.jobs.fail_attempt(job["id"],attempt_id,exc.code,retryable=exc.retryable,retry_after_at=exc.retry_after_at)
            except Exception:pass
        except Exception:
            try:self.jobs.fail_attempt(job["id"],attempt_id,"tts_preview_failed",retryable=True)
            except Exception:pass
        finally:
            path.unlink(missing_ok=True)

    def _run_batch(self,job:dict[str,Any],round_no:int=1)->None:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT r.id,j.input_snapshot FROM tts_runs r JOIN jobs j ON j.id=r.producer_job_id WHERE j.id=?",(job["id"],)).fetchone()
            attempt=connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'",(job["id"],)).fetchone()
        if not row or not attempt:return
        run_id=row["id"];attempt_id=attempt["id"];snapshot=json.loads(row["input_snapshot"])
        if not self._validate_compact_snapshot(run_id,snapshot):
            self.jobs.fail_attempt(job["id"],attempt_id,"tts_unit_mapping_invalid",retryable=False)
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_runs SET status='FAILED',completed_at=?,manifest_artifact_id=NULL WHERE id=? AND status NOT IN ('READY','STALE')",(utc_now(),run_id))
            return
        with closing(self.database.connect()) as connection:
            connection.execute("UPDATE tts_runs SET status='RUNNING' WHERE id=?",(run_id,))
            connection.execute("UPDATE tts_run_units SET status='QUEUED',updated_at=? WHERE tts_run_id=? AND (status='RUNNING' OR (status='FAILED' AND retryable=1))",(utc_now(),run_id))
        self.service._reconcile_unit_artifacts(job["project_id"],run_id)
        with closing(self.database.connect()) as connection:
            pending=connection.execute("SELECT * FROM tts_run_units WHERE tts_run_id=? AND status='QUEUED' ORDER BY position",(run_id,)).fetchall()
        if not pending:
            self._finish_batch(job,run_id,attempt_id,snapshot)
            return
        retryable=False;retry_after=None;review=False;cancelled=False;stale=False;throttled=False
        iterator=iter(pending); futures={}; completed=0
        temp_root=self.artifacts._ensure_directory(PurePosixPath("staging/tts"))
        with ThreadPoolExecutor(max_workers=TTS_CONCURRENCY,thread_name_prefix="edge-tts-unit") as pool:
            def submit_one(unit:sqlite3.Row)->None:
                if not self.service._run_is_current(job["project_id"],run_id):
                    raise TTSError("translation_revision_stale")
                with closing(self.database.connect()) as connection:
                    connection.execute("UPDATE tts_run_units SET status='RUNNING',updated_at=? WHERE tts_run_id=? AND unit_id=?",(utc_now(),run_id,unit["unit_id"]))
                futures[pool.submit(self._synthesize_unit,job,attempt_id,run_id,unit, snapshot,temp_root,round_no)]=unit
            for _ in range(min(TTS_CONCURRENCY,len(pending))):
                try:submit_one(next(iterator))
                except StopIteration:break
                except TTSError as exc:
                    stale=exc.code=="translation_revision_stale";review=exc.review_required;retryable=exc.retryable;break
            while futures:
                done,_=wait(tuple(futures),timeout=0.2,return_when=FIRST_COMPLETED)
                if not done:
                    if self._cancelled(job["id"]):cancelled=True
                    continue
                for future in done:
                    unit=futures.pop(future);completed+=1
                    try:future.result()
                    except (TTSError, ArtifactError) as raw_exc:
                        exc = raw_exc if isinstance(raw_exc, TTSError) else TTSError(raw_exc.code,
                            retryable=raw_exc.code in {"tts_audio_decode_timeout", "tts_media_tools_unavailable"})
                        with closing(self.database.connect()) as connection:
                            status="CANCELLED" if exc.code=="job_cancelled" else "REVIEW_REQUIRED" if exc.review_required else "FAILED"
                            connection.execute("UPDATE tts_run_units SET status=?,error_code=?,retryable=?,updated_at=? WHERE tts_run_id=? AND unit_id=?",
                                (status,exc.code,int(exc.retryable),utc_now(),run_id,unit["unit_id"]))
                        retryable=retryable or exc.retryable;retry_after=exc.retry_after_at or retry_after
                        throttled=throttled or exc.code=="tts_provider_throttled"
                        review=review or exc.review_required;cancelled=cancelled or exc.code=="job_cancelled"
                    except Exception as exc:
                        with closing(self.database.connect()) as connection:
                            connection.execute("UPDATE tts_provider_calls SET status='FAILED',completed_at=?,error_code='tts_unit_failed',exception_class=? WHERE tts_run_id=? AND unit_id=? AND status='RUNNING'",(utc_now(),type(exc).__name__,run_id,unit["unit_id"]))
                            connection.execute("UPDATE tts_run_units SET status='FAILED',error_code='tts_unit_failed',retryable=0,updated_at=? WHERE tts_run_id=? AND unit_id=?",(utc_now(),run_id,unit["unit_id"]))
                    ready=self._counts(run_id)["ready_units"]
                    counts_now=self._unit_counts(run_id)
                    self.jobs.update_attempt_progress(job["id"],attempt_id,{"phase":"synthesizing_units","completed_units":ready,"total_units":counts_now["total_units"],"fraction":ready/max(1,counts_now["total_units"]),"current_round":round_no,"round_count":TTS_MAX_ROUNDS,"running_units":counts_now["running_units"],"queued_units":counts_now["queued_units"],"failed_units":counts_now["failed_units"]})
                if self._cancelled(job["id"]):
                    cancelled=True
                    for future in futures: future.cancel()
                    break
                while len(futures)<TTS_CONCURRENCY and not throttled:
                    try:submit_one(next(iterator))
                    except StopIteration:break
                    except TTSError as exc:
                        stale=exc.code=="translation_revision_stale";review=exc.review_required
                        break
        if self._cancelled(job["id"]):cancelled=True
        with closing(self.database.connect()) as connection:
            if cancelled:
                connection.execute("UPDATE tts_run_units SET status='CANCELLED',retryable=0,error_code='job_cancelled',updated_at=? WHERE tts_run_id=? AND status IN ('QUEUED','RUNNING')",(utc_now(),run_id))
                connection.execute("UPDATE tts_runs SET status='CANCELLED',completed_at=? WHERE id=?",(utc_now(),run_id))
            elif stale:
                connection.execute("UPDATE tts_run_units SET status='STALE',error_code='translation_revision_stale',updated_at=? WHERE tts_run_id=? AND status='QUEUED'",(utc_now(),run_id))
                connection.execute("UPDATE tts_runs SET status='STALE',completed_at=? WHERE id=?",(utc_now(),run_id))
            else:
                counts=self._counts(run_id)
                if counts["ready_units"]==counts["total_units"]:
                    pass
                else:
                    status="REVIEW_REQUIRED" if review else "PARTIAL" if counts["ready_units"] else "FAILED"
                    connection.execute("UPDATE tts_runs SET status=?,completed_at=? WHERE id=?",(status,utc_now(),run_id))
        if cancelled or stale:
            self._finish_manifest_only(job,run_id,attempt_id,snapshot, cancelled=cancelled)
            return
        counts=self._counts(run_id)
        if counts["ready_units"]==counts["total_units"]:
            self._finish_batch(job,run_id,attempt_id,snapshot)
        elif retryable and round_no < TTS_MAX_ROUNDS and counts["ready_units"] > 0 and not self._cancelled(job["id"]):
            self.jobs.update_attempt_progress(job["id"],attempt_id,{"phase":"retry_wait","completed_units":counts["ready_units"],"total_units":counts["total_units"],"fraction":counts["ready_units"]/max(1,counts["total_units"]),"current_round":round_no+1,"round_count":TTS_MAX_ROUNDS,"running_units":0,"queued_units":0,"failed_units":counts["total_units"]-counts["ready_units"]})
            wait_seconds=TTS_ROUND_DELAY_SECONDS
            if retry_after:
                try: wait_seconds=max(wait_seconds,min(3600,(datetime.fromisoformat(retry_after.replace("Z","+00:00"))-datetime.now(timezone.utc)).total_seconds()))
                except (ValueError,TypeError): pass
            deadline=time.monotonic()+wait_seconds
            while time.monotonic()<deadline and not self._cancelled(job["id"]): time.sleep(min(0.1,deadline-time.monotonic()))
            if not self._cancelled(job["id"]): self._run_batch(job,round_no+1)
            else: self._finish_manifest_only(job,run_id,attempt_id,snapshot,cancelled=True)
        else:
            self.jobs.fail_attempt(job["id"],attempt_id,"tts_units_incomplete",retryable=retryable,retry_after_at=retry_after)

    def _counts(self,run_id:str)->dict[str,int]:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT COUNT(*) AS total,SUM(status='READY') AS ready FROM tts_run_units WHERE tts_run_id=?",(run_id,)).fetchone()
        return {"total_units":row["total"],"ready_units":row["ready"] or 0}

    def _validate_compact_snapshot(self,run_id:str,snapshot:dict[str,Any])->bool:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT unit_id,logical_unit_key,position,source_segment_ids_json,revision_segment_ids_json,source_text,accepted_text,text_sha256,start_us,end_us,source_window_duration_us,synthesis_fingerprint FROM tts_run_units WHERE tts_run_id=? ORDER BY position",(run_id,)).fetchall()
        values=[{"unit_id":r["unit_id"],"logical_unit_key":r["logical_unit_key"],"position":r["position"],"source_segment_ids":json.loads(r["source_segment_ids_json"]),"revision_segment_ids":json.loads(r["revision_segment_ids_json"]),"source_text":r["source_text"],"accepted_text":r["accepted_text"],"text_sha256":r["text_sha256"],"start_us":r["start_us"],"end_us":r["end_us"],"source_window_duration_us":r["source_window_duration_us"],"synthesis_fingerprint":r["synthesis_fingerprint"]} for r in rows]
        return len(values)==snapshot.get("unit_count") and _hash(values)==snapshot.get("units_sha256") and all(item["position"]==i for i,item in enumerate(values)) and len({item["unit_id"] for item in values})==len(values)

    def _unit_counts(self,run_id:str)->dict[str,int]:
        with closing(self.database.connect()) as connection:
            row=connection.execute("SELECT COUNT(*) total,SUM(status='READY') ready,SUM(status='RUNNING') running,SUM(status='QUEUED') queued,SUM(status='FAILED') failed FROM tts_run_units WHERE tts_run_id=?",(run_id,)).fetchone()
        return {"total_units":row["total"],"ready_units":row["ready"] or 0,"running_units":row["running"] or 0,"queued_units":row["queued"] or 0,"failed_units":row["failed"] or 0}

    def _synthesize_unit(self,job:dict[str,Any],attempt_id:str,run_id:str,unit:sqlite3.Row,snapshot:dict[str,Any],temp_root:Path,round_no:int=1)->None:
        config=snapshot["config"];temp=temp_root/f"{uuid.uuid4()}.mp3"
        call_id=str(uuid.uuid4()); started=time.monotonic(); stamp=utc_now()
        try:
            _REQUEST_PACER.wait_turn(lambda:self._cancelled(job["id"]))
            with closing(self.database.connect()) as connection:
                call_no=connection.execute("UPDATE tts_run_units SET attempt_count=attempt_count+1,updated_at=? WHERE tts_run_id=? AND unit_id=? RETURNING attempt_count",(utc_now(),run_id,unit["unit_id"])).fetchone()[0]
                connection.execute("INSERT INTO tts_provider_calls(id,project_id,tts_run_id,unit_id,job_attempt_id,call_kind,round_no,call_no,status,started_at) VALUES(?,?,?,?,?,'batch_unit',?,?,'RUNNING',?)",(call_id,job["project_id"],run_id,unit["unit_id"],attempt_id,round_no,call_no,stamp))
            self.adapter.synthesize(unit["accepted_text"],temp,voice=config["voice"],rate=config["rate_percent"],
                pitch_hz=config["pitch_hz"],volume=config["volume_percent"],cancelled=lambda:self._cancelled(job["id"]))
            if self._cancelled(job["id"]):raise TTSError("job_cancelled")
            with temp.open("rb") as audio:
                artifact=self.artifacts.publish(project_id=job["project_id"],job_id=job["id"],attempt_id=attempt_id,
                    kind="tts_audio",input_fingerprint=job["input_fingerprint"],content_fingerprint=unit["synthesis_fingerprint"],
                    fingerprint_version=TTS_FINGERPRINT_VERSION,source=audio,
                    validator_cancelled=lambda:self._cancelled(job["id"]))
            stamp_done=utc_now()
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_run_units SET status='READY',artifact_id=?,cache_hit=0,error_code=NULL,retryable=0,updated_at=? WHERE tts_run_id=? AND unit_id=?",(artifact["id"],stamp_done,run_id,unit["unit_id"]))
                connection.execute("UPDATE tts_provider_calls SET status='READY',completed_at=?,elapsed_ms=? WHERE id=?",(stamp_done,int((time.monotonic()-started)*1000),call_id))
        except (TTSError, ArtifactError) as raw_exc:
            exc=raw_exc if isinstance(raw_exc,TTSError) else TTSError(raw_exc.code,retryable=raw_exc.code in {"tts_audio_decode_timeout","tts_media_tools_unavailable"})
            with closing(self.database.connect()) as connection:
                status="CANCELLED" if exc.code=="job_cancelled" else "FAILED"
                connection.execute("UPDATE tts_provider_calls SET status=?,completed_at=?,elapsed_ms=?,error_code=?,exception_class=?,http_status=?,retry_after=? WHERE id=? AND status='RUNNING'",(status,utc_now(),int((time.monotonic()-started)*1000),exc.code,type(raw_exc).__name__,exc.http_status,exc.retry_after_at,call_id))
            raise exc
        except Exception as exc:
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_provider_calls SET status='FAILED',completed_at=?,elapsed_ms=?,error_code='tts_unit_failed',exception_class=? WHERE id=? AND status='RUNNING'",(utc_now(),int((time.monotonic()-started)*1000),type(exc).__name__,call_id))
            raise
        finally:
            temp.unlink(missing_ok=True)

    def _manifest(self,run_id:str,snapshot:dict[str,Any])->bytes:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT unit_id,logical_unit_key,position,synthesis_fingerprint,status,artifact_id,cache_hit FROM tts_run_units WHERE tts_run_id=? ORDER BY position",(run_id,)).fetchall()
        return _canonical({"schema_version":1,"tts_run_id":run_id,"input_fingerprint":_hash(snapshot),
            "provider_version":EDGE_TTS_VERSION,"synthesis_version":TTS_SYNTHESIS_VERSION,"units":[dict(row) for row in rows]}).encode("utf-8")

    def _finish_batch(self,job:dict[str,Any],run_id:str,attempt_id:str,snapshot:dict[str,Any])->None:
        if not self.service._run_is_current(job["project_id"],run_id):
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_runs SET status='STALE',completed_at=? WHERE id=?",(utc_now(),run_id))
            self._finish_manifest_only(job,run_id,attempt_id,snapshot,cancelled=False)
            return
        with closing(self.database.connect()) as connection:
            unit_rows=connection.execute("SELECT unit_id,artifact_id,synthesis_fingerprint FROM tts_run_units WHERE tts_run_id=? AND status='READY'",(run_id,)).fetchall()
        invalid=[]
        for unit in unit_rows:
            try:
                artifact=self.artifacts.verify_ready(unit["artifact_id"],job["project_id"])
                if artifact.get("content_fingerprint")!=unit["synthesis_fingerprint"]: invalid.append(unit["unit_id"])
            except ArtifactError: invalid.append(unit["unit_id"])
        if invalid:
            with closing(self.database.connect()) as connection:
                for unit_id in invalid:
                    connection.execute("UPDATE tts_run_units SET status='FAILED',artifact_id=NULL,retryable=1,error_code='tts_artifact_unavailable',updated_at=? WHERE tts_run_id=? AND unit_id=?",(utc_now(),run_id,unit_id))
                connection.execute("UPDATE tts_runs SET status='PARTIAL' WHERE id=?",(run_id,))
            self.jobs.fail_attempt(job["id"],attempt_id,"tts_artifact_unavailable",retryable=True)
            return
        manifest=self.artifacts.publish(project_id=job["project_id"],job_id=job["id"],attempt_id=attempt_id,
            kind="tts_run_manifest",input_fingerprint=job["input_fingerprint"],fingerprint_version=TTS_FINGERPRINT_VERSION,source=self._manifest(run_id,snapshot))
        evidence=self.artifacts.completion_evidence(job["id"],attempt_id,[manifest["id"]])
        def complete(connection:sqlite3.Connection)->None:
            active=connection.execute("SELECT active_artifact_id,input_fingerprint FROM stage_pointers WHERE project_id=? AND stage='tts'",(job["project_id"],)).fetchone()
            status="READY" if active and active["active_artifact_id"]==manifest["id"] and active["input_fingerprint"]==job["input_fingerprint"] else "STALE"
            connection.execute("UPDATE tts_runs SET status=?,manifest_artifact_id=?,completed_at=? WHERE id=?",
                (status,manifest["id"],utc_now(),run_id))
        self.jobs.complete_attempt(job["id"],attempt_id,evidence,completion_hook=complete)
        if self.jobs.get_job(job["id"])["status"]=="CANCELLED":
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_runs SET status='CANCELLED',manifest_artifact_id=?,completed_at=? WHERE id=?",(manifest["id"],utc_now(),run_id))

    def _finish_manifest_only(self,job:dict[str,Any],run_id:str,attempt_id:str,snapshot:dict[str,Any],*,cancelled:bool)->None:
        try:
            manifest=self.artifacts.publish(project_id=job["project_id"],job_id=job["id"],attempt_id=attempt_id,
                kind="tts_run_manifest",input_fingerprint=job["input_fingerprint"],fingerprint_version=TTS_FINGERPRINT_VERSION,source=self._manifest(run_id,snapshot))
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE tts_runs SET manifest_artifact_id=? WHERE id=?",(manifest["id"],run_id))
            evidence=self.artifacts.completion_evidence(job["id"],attempt_id,[manifest["id"]])
            self.jobs.complete_attempt(job["id"],attempt_id,evidence,completion_hook=lambda conn:conn.execute(
                "UPDATE tts_runs SET manifest_artifact_id=? WHERE id=?",(manifest["id"],run_id)))
        except Exception:
            if not cancelled:
                try:self.jobs.fail_attempt(job["id"],attempt_id,"tts_run_stale",retryable=False)
                except Exception:pass


