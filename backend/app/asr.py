"""Local Faster-Whisper transcription with durable Jobs and immutable Runs."""

from __future__ import annotations

import hashlib
import errno
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.artifacts import ArtifactError, ArtifactPublicationService
from app.database import Database, utc_now
from app.jobs import ActivationStage, IdempotencyConflict, JobConflict, JobInputError, JobNotFound, JobService
from app.media import MediaError, MediaImportService, _hash_file, _path_identity


ASR_ADAPTER_VERSION = "faster-whisper-local-v1"
EXTRACTOR_VERSION = "ffmpeg-mono-pcm16k-v1"
MODEL_SPECS = {
    "base": {
        "repo_id": "Systran/faster-whisper-base",
        "revision": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "approximate_bytes": 145_000_000,
    },
    "small": {
        "repo_id": "Systran/faster-whisper-small",
        "revision": "536b0662742c02347bc0e980a01041f333bce120",
        "approximate_bytes": 484_000_000,
    },
}
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$", re.IGNORECASE)
_CUDA_ERROR_MARKERS = ("cuda", "cudnn", "cublas", "nvrtc", "gpu device", "device-side")


class ASRError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AudioExtraction:
    duration_us: int
    source_offset_us: int
    source_video_start_us: int
    audio_stream_index: int
    sample_rate: int = 16_000


@dataclass(frozen=True)
class ASRConfig:
    model: str = "base"
    language: str | None = None
    device: str = "auto"
    vad_enabled: bool = True
    beam_size: int = 3
    allow_cpu_fallback: bool = True
    allow_model_download: bool = False


def validate_config(config: ASRConfig) -> ASRConfig:
    if config.model not in MODEL_SPECS:
        raise ASRError("invalid_model")
    if config.language is not None and (not isinstance(config.language, str) or not _LANGUAGE.fullmatch(config.language)):
        raise ASRError("invalid_language")
    if config.device not in {"auto", "cpu", "cuda"}:
        raise ASRError("invalid_device")
    if type(config.vad_enabled) is not bool or type(config.allow_cpu_fallback) is not bool or type(config.allow_model_download) is not bool:
        raise ASRError("invalid_asr_config")
    if type(config.beam_size) is not int or not 1 <= config.beam_size <= 5:
        raise ASRError("invalid_beam_size")
    return config


class HardwareProbe:
    """Reports CTranslate2 capabilities; it does not claim inference succeeded."""

    @staticmethod
    def inspect() -> dict[str, Any]:
        try:
            import ctranslate2
        except ImportError:
            return {"cuda_device_count": 0, "cuda_compute_types": [], "cpu_compute_types": [], "probe_error": "ctranslate2_unavailable"}
        try:
            count = int(ctranslate2.get_cuda_device_count())
            cpu = sorted(ctranslate2.get_supported_compute_types("cpu"))
            cuda = sorted(ctranslate2.get_supported_compute_types("cuda")) if count else []
            return {"cuda_device_count": count, "cuda_compute_types": cuda, "cpu_compute_types": cpu, "probe_error": None}
        except Exception as exc:
            return {"cuda_device_count": 0, "cuda_compute_types": [], "cpu_compute_types": [], "probe_error": _safe_error_class(exc)}


def _safe_error_class(exc: BaseException) -> str:
    # Never persist raw provider/runtime messages: they can contain machine paths.
    name = type(exc).__name__.lower()
    return re.sub(r"[^a-z0-9_]+", "_", name)[:80] or "runtime_error"


def _safe_diagnostic_summary(exc: BaseException) -> str:
    # Arbitrary runtime messages may include paths, secrets, or media content.
    if isinstance(exc, TypeError):
        match = re.search(r"unexpected keyword argument ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", str(exc))
        if match:
            return f"unexpected keyword argument: {match.group(1)}"
    if isinstance(exc, OSError):
        return f"OS error number: {exc.errno}" if exc.errno is not None else "OS operation failed"
    return ""


class FFmpegAudioExtractor:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None, timeout_seconds: int = 180):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or shutil.which("ffprobe")
        self.timeout_seconds = timeout_seconds

    def _probe_input(self, source: Path) -> dict[str, Any]:
        if not self.ffprobe:
            raise ASRError("ffprobe_unavailable")
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
            capture_output=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise ASRError("media_probe_failed")
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ASRError("media_probe_failed") from exc

    def extract(self, source: Path, destination: Path, cancelled: Callable[[], bool]) -> AudioExtraction:
        if not self.ffmpeg:
            raise ASRError("ffmpeg_unavailable")
        probe = self._probe_input(source)
        audio_streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"]
        if not audio_streams:
            raise ASRError("no_audio_stream")
        stream = audio_streams[0]
        try:
            video_origin = float(probe.get("format", {}).get("start_time", 0) or 0)
            audio_start = float(stream.get("start_time", video_origin) or video_origin)
            stream_index = int(stream.get("index", 0))
        except (TypeError, ValueError) as exc:
            raise ASRError("media_timing_invalid") from exc
        if not math.isfinite(video_origin) or not math.isfinite(audio_start):
            raise ASRError("media_timing_invalid")
        offset_us = round((audio_start - video_origin) * 1_000_000)
        if offset_us < 0:
            # Source timeline starts at the container origin; earlier audio is clipped at zero.
            offset_us = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-n", "-i", str(source),
            "-map", f"0:{stream_index}", "-vn", "-af", "asetpts=PTS-STARTPTS", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", str(destination),
        ]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as exc:
            raise ASRError("ffmpeg_unavailable") from exc
        deadline = time.monotonic() + self.timeout_seconds
        while process.poll() is None:
            if cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                raise ASRError("job_cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                process.wait(timeout=3)
                raise ASRError("ffmpeg_timeout")
            time.sleep(0.05)
        _stdout, stderr = process.communicate()
        if process.returncode != 0:
            # Keep FFmpeg diagnostics out of API, logs and Job error strings.
            raise ASRError("ffmpeg_extraction_failed")
        try:
            with wave.open(str(destination), "rb") as audio:
                if audio.getnchannels() != 1 or audio.getframerate() != 16_000 or audio.getsampwidth() != 2:
                    raise ASRError("audio_format_invalid")
                frames = audio.getnframes()
                if frames <= 0:
                    raise ASRError("audio_empty")
                duration_us = round(frames * 1_000_000 / audio.getframerate())
        except (OSError, wave.Error) as exc:
            raise ASRError("audio_decode_failed") from exc
        return AudioExtraction(duration_us, offset_us, round(video_origin * 1_000_000), stream_index)


class FasterWhisperAdapter:
    def __init__(self, model_root: Path, probe: HardwareProbe | None = None):
        self.model_root = model_root
        self.probe = probe or HardwareProbe()

    @staticmethod
    def model_info(model_name: str) -> dict[str, Any]:
        spec = MODEL_SPECS.get(model_name)
        if spec is None:
            raise ASRError("invalid_model")
        return {"model": model_name, "repo_id": spec["repo_id"], "revision": spec["revision"], "approximate_bytes": spec["approximate_bytes"]}

    def is_cached(self, model_name: str) -> bool:
        spec = MODEL_SPECS.get(model_name)
        if spec is None:
            raise ASRError("invalid_model")
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=spec["repo_id"], revision=spec["revision"], cache_dir=str(self.model_root), local_files_only=True)
            return True
        except Exception:
            return False

    def run(
        self, audio_path: Path, config: ASRConfig, *, source_duration_us: int,
        source_offset_us: int, cancelled: Callable[[], bool], progress: Callable[[str], None],
    ) -> dict[str, Any]:
        validate_config(config)
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
            from faster_whisper import __version__ as fw_version
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ASRError("faster_whisper_unavailable") from exc

        spec = MODEL_SPECS[config.model]
        info = self.probe.inspect()
        cuda_available = info["cuda_device_count"] > 0
        requested = config.device
        use_cuda = requested == "cuda" or (requested == "auto" and cuda_available)
        fallback_reason = None
        if use_cuda and not cuda_available:
            if not config.allow_cpu_fallback:
                raise ASRError("cuda_unavailable")
            use_cuda = False
            fallback_reason = "cuda_unavailable"
        if requested == "cpu":
            use_cuda = False
        progress("loading_model")
        model_started = time.perf_counter()
        try:
            model_path = snapshot_download(
                repo_id=spec["repo_id"], revision=spec["revision"], cache_dir=str(self.model_root),
                local_files_only=not config.allow_model_download,
            )
        except Exception as exc:
            if not config.allow_model_download:
                raise ASRError("model_download_consent_required") from exc
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise ASRError("insufficient_model_disk_space") from exc
            raise ASRError("model_download_failed") from exc
        model_load_seconds = time.perf_counter() - model_started

        def instantiate(device: str):
            types = ctranslate2.get_supported_compute_types(device)
            if device == "cuda":
                compute_type = "float16" if "float16" in types else "int8_float16" if "int8_float16" in types else "float32"
            else:
                compute_type = "int8" if "int8" in types else "float32"
            threads = max(1, min(8, (os.cpu_count() or 2) - 2)) if device == "cpu" else 0
            model = WhisperModel(model_path, device=device, compute_type=compute_type, cpu_threads=threads)
            return model, compute_type

        actual_device = "cuda" if use_cuda else "cpu"
        try:
            model, compute_type = instantiate(actual_device)
        except Exception as exc:
            if actual_device != "cuda" or not config.allow_cpu_fallback or not _is_cuda_failure(exc):
                raise ASRError("cuda_initialization_failed" if actual_device == "cuda" else "model_load_failed") from exc
            actual_device, fallback_reason = "cpu", "cuda_initialization_failed"
            model, compute_type = instantiate("cpu")

        if cancelled():
            raise ASRError("job_cancelled")
        progress("transcribing")
        inference_started = time.perf_counter()

        def consume(current_model):
            segments, info_value = current_model.transcribe(
                str(audio_path), language=config.language, task="transcribe", beam_size=config.beam_size,
                vad_filter=config.vad_enabled,
            )
            values = []
            for segment in segments:  # Faster-Whisper does inference lazily while iterating.
                if cancelled():
                    raise ASRError("job_cancelled")
                start = round(float(segment.start) * 1_000_000) + source_offset_us
                end = round(float(segment.end) * 1_000_000) + source_offset_us
                text = segment.text
                if not isinstance(text, str) or not text.strip():
                    raise ASRError("asr_segment_text_empty")
                if not (0 <= start < end) or end > source_duration_us + 40_000:
                    raise ASRError("asr_timestamp_out_of_bounds")
                if end > source_duration_us:
                    end = source_duration_us
                flags = []
                no_speech = _optional_finite(getattr(segment, "no_speech_prob", None))
                avg_logprob = _optional_finite(getattr(segment, "avg_logprob", None))
                compression = _optional_finite(getattr(segment, "compression_ratio", None))
                if no_speech is not None and no_speech >= 0.6:
                    flags.append("high_no_speech_probability")
                if avg_logprob is not None and avg_logprob < -1.0:
                    flags.append("low_log_probability")
                if compression is not None and compression > 2.4:
                    flags.append("high_compression_ratio")
                values.append({
                    "start_us": start, "end_us": end, "source_text": text,
                    "avg_logprob": avg_logprob, "no_speech_prob": no_speech,
                    "compression_ratio": compression, "flags": flags,
                })
            language = getattr(info_value, "language", None)
            probability = _optional_finite(getattr(info_value, "language_probability", None))
            return values, language, probability

        try:
            segments, language, language_probability = consume(model)
        except ASRError:
            raise
        except Exception as exc:
            if actual_device != "cuda" or not config.allow_cpu_fallback or not _is_cuda_failure(exc):
                raise ASRError("asr_inference_failed") from exc
            # Discard the partial CUDA result and retry the entire input once on CPU.
            actual_device, fallback_reason = "cpu", "cuda_inference_failed"
            model, compute_type = instantiate("cpu")
            segments, language, language_probability = consume(model)
        inference_seconds = time.perf_counter() - inference_started
        review = any(segment["flags"] for segment in segments)
        run_status = "NO_SPEECH" if not segments else "REVIEW_REQUIRED" if review else "READY"
        return {
            "segments": segments, "language": language or config.language,
            "language_probability": language_probability, "actual_device": actual_device,
            "compute_type": compute_type, "fallback_reason": fallback_reason,
            "model_id": spec["repo_id"], "model_revision": spec["revision"],
            "faster_whisper_version": fw_version, "ctranslate2_version": ctranslate2.__version__,
            "model_load_seconds": model_load_seconds, "inference_seconds": inference_seconds,
            "run_status": run_status,
        }


def _optional_finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_cuda_failure(exc: BaseException) -> bool:
    message = f"{type(exc).__name__} {exc}".lower()
    return any(marker in message for marker in _CUDA_ERROR_MARKERS)


class TranscriptArtifactValidator:
    version = "asr-transcript-json-v1"

    def validate(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            segments = value["segments"]
            if value.get("schema_version") != 1 or not isinstance(segments, list):
                raise ValueError
            previous_start = -1
            for index, item in enumerate(segments):
                start, end = item["start_us"], item["end_us"]
                if type(start) is not int or type(end) is not int or not 0 <= start < end or item["position"] != index:
                    raise ValueError
                if start < previous_start or not isinstance(item.get("source_text"), str) or not item["source_text"].strip():
                    raise ValueError
                previous_start = start
            status = value["status"]
            if status not in {"READY", "NO_SPEECH", "REVIEW_REQUIRED"} or (status == "NO_SPEECH") != (len(segments) == 0):
                raise ValueError
            return {"schema_version": 1, "segment_count": len(segments), "status": status}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ASRError("transcript_artifact_invalid") from exc


class ASRService:
    _diagnostic_lock = threading.Lock()
    def __init__(
        self, database: Database, jobs: JobService, artifacts: ArtifactPublicationService,
        media: MediaImportService, *, adapter: FasterWhisperAdapter | Any | None = None,
        extractor: FFmpegAudioExtractor | None = None,
    ):
        self.database, self.jobs, self.artifacts, self.media = database, jobs, artifacts, media
        self.adapter = adapter or FasterWhisperAdapter(database.data_root / "models" / "faster-whisper")
        self.extractor = extractor or FFmpegAudioExtractor()
        self.active_job_id: str | None = None
        self.active_lock = threading.Lock()
        self.source_lock = threading.RLock()

    def models(self) -> dict[str, Any]:
        values = []
        for name, spec in MODEL_SPECS.items():
            values.append({"id": name, "approximate_bytes": spec["approximate_bytes"], "cached": self.adapter.is_cached(name)})
        return {"items": values, "hardware": HardwareProbe.inspect()}

    def get_processing_source(self, project_id: str) -> dict[str, Any]:
        with self.source_lock:
            return self.media.get_processing_source(project_id)

    def select_processing_source(self, project_id: str, asset_id: str, expected_generation: int) -> dict[str, Any]:
        with self.source_lock:
            previous = self.media.get_processing_source(project_id)
            result = self.media.select_processing_source(project_id, asset_id, expected_generation)
            if previous["media_asset_id"] != asset_id or previous["generation"] != result["generation"]:
                asset = result["asset"]
                source_snapshot = {
                    "schema_version": 1, "source_asset_id": asset_id,
                    "source_generation": result["generation"], "source_sha256": asset["sha256"],
                    "source_size": asset["file_size"],
                }
                self.jobs.set_stage_input(project_id, "asr", source_snapshot)
            return result

    def _write_diagnostic(self, job_id: str, attempt_id: str, phase: str, code: str, exc: BaseException) -> None:
        # Developer diagnostics are local-only and deliberately exclude traceback and media text.
        root_cause = exc
        while root_cause.__cause__ is not None:
            root_cause = root_cause.__cause__
        item = {
            "timestamp": utc_now(), "job_id": job_id, "attempt_id": attempt_id,
            "phase": phase, "error_code": code, "exception_type": _safe_error_class(root_cause),
            "summary": _safe_diagnostic_summary(root_cause),
        }
        path = self.database.data_root / "logs" / "asr-diagnostics.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n"
            with self._diagnostic_lock, path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
        except OSError:
            # Logging must never mask the durable Job failure state.
            pass

    def create_job(self, project_id: str, config: ASRConfig, idempotency_key: str) -> dict[str, Any]:
        validate_config(config)
        self.jobs.validate_idempotency_key(idempotency_key)
        with self.source_lock:
            selected = self.media.get_processing_source(project_id)
            asset_id = selected["media_asset_id"]
            if not asset_id:
                raise ASRError("processing_source_unselected")
            if selected["status"] != "READY":
                raise ASRError("asset_unavailable" if selected["status"] == "MISSING" else "asset_changed")
            try:
                source = self.media.get_transcription_source(project_id, asset_id)
            except MediaError as exc:
                raise ASRError(exc.code) from exc
            if not self.adapter.is_cached(config.model) and not config.allow_model_download:
                raise ASRError("model_download_consent_required")
            spec = MODEL_SPECS[config.model]
            snapshot = {
                "schema_version": 1, "source_asset_id": asset_id,
                "source_generation": selected["generation"],
                "source_sha256": source["sha256"], "source_size": source["file_size"],
                "source_duration_us": source["duration_us"], "source_mode": source["mode"],
                "model_id": spec["repo_id"], "model_revision": spec["revision"],
                "language": config.language, "device": config.device,
                "vad_enabled": config.vad_enabled, "beam_size": config.beam_size,
                "allow_cpu_fallback": config.allow_cpu_fallback,
                "allow_model_download": config.allow_model_download,
                "adapter_version": ASR_ADAPTER_VERSION, "extractor_version": EXTRACTOR_VERSION,
            }
            self.jobs.set_stage_input(project_id, "asr", snapshot)
            return self.jobs.create_job(project_id, "asr_transcription", snapshot, idempotency_key, activation_stage=ActivationStage.ASR)

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """SELECT r.id,r.project_id,r.source_asset_id,r.model_id,r.language,r.actual_device,r.compute_type,r.status,
                          r.input_fingerprint,r.duration_us,r.created_at,r.completed_at,
                          (p.active_artifact_id=r.artifact_id AND s.media_asset_id=r.source_asset_id) AS is_active
                   FROM transcription_runs r LEFT JOIN stage_pointers p ON p.project_id=r.project_id AND p.stage='asr'
                   LEFT JOIN project_processing_sources s ON s.project_id=r.project_id
                   WHERE r.project_id=? ORDER BY r.created_at DESC,r.id DESC""", (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM transcription_runs WHERE id=? AND project_id=?", (run_id, project_id)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["timing"] = json.loads(result.pop("timing_json"))
            result["provenance"] = json.loads(result.pop("provenance_json"))
            result["segments"] = [
                {**dict(segment), "flags": json.loads(segment["flags_json"])}
                for segment in connection.execute("SELECT * FROM segments WHERE run_id=? ORDER BY position", (run_id,)).fetchall()
            ]
            return result

    def _cancelled(self, job_id: str) -> bool:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row is None or row["status"] != "RUNNING"

    def run_once(self) -> bool:
        job = self.jobs.claim_next_job(kind="asr_transcription")
        if job is None:
            return False
        with self.active_lock:
            self.active_job_id = job["id"]
        attempt_id = "unknown"
        phase = "loading_input"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (job["id"],)).fetchone()
                attempt = connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'", (job["id"],)).fetchone()
            if row is None or attempt is None:
                if attempt:
                    self.jobs.fail_attempt(job["id"], attempt["id"], "asr_input_missing", retryable=False)
                return True
            attempt_id = attempt["id"]
            snapshot = json.loads(row["input_snapshot"])
            cancelled = lambda: self._cancelled(job["id"])
            if cancelled():
                self.jobs.fail_attempt(job["id"], attempt["id"], "job_cancelled", retryable=False)
                return True
            source = self.media.get_transcription_source(job["project_id"], snapshot["source_asset_id"])
            phase = "validating_source"
            if source["sha256"] != snapshot["source_sha256"] or source["file_size"] != snapshot["source_size"]:
                raise ASRError("source_changed")
            def progress(current_phase: str) -> None:
                nonlocal phase
                phase = current_phase
                self.jobs.update_attempt_progress(job["id"], attempt["id"], {"phase": current_phase})
            progress("extracting_audio")
            phase = "extracting_audio"
            scratch = self.database.data_root / "staging" / "asr" / f"{job['id']}-{attempt['id']}.wav"
            try:
                extraction = self.extractor.extract(source["path"], scratch, cancelled)
                if self.media.verify_transcription_source(source) is False:
                    raise ASRError("source_changed")
                if cancelled():
                    raise ASRError("job_cancelled")
                config = ASRConfig(
                    model=next(name for name, spec in MODEL_SPECS.items() if spec["repo_id"] == snapshot["model_id"]),
                    language=snapshot["language"], device=snapshot["device"], vad_enabled=snapshot["vad_enabled"],
                    beam_size=snapshot["beam_size"], allow_cpu_fallback=snapshot["allow_cpu_fallback"],
                    allow_model_download=snapshot["allow_model_download"],
                )
                phase = "loading_model"
                result = self.adapter.run(
                    scratch, config, source_duration_us=snapshot["source_duration_us"],
                    source_offset_us=extraction.source_offset_us, cancelled=cancelled, progress=progress,
                )
                phase = "validating_result"
                if cancelled():
                    raise ASRError("job_cancelled")
                if not self.media.verify_transcription_source(source):
                    raise ASRError("source_changed")
                run_id = str(uuid.uuid4())
                segment_rows = []
                for position, segment in enumerate(result["segments"]):
                    segment_rows.append({"id": str(uuid.uuid4()), "position": position, **segment})
                status = result["run_status"]
                if status == "NO_SPEECH" and segment_rows:
                    raise ASRError("asr_result_invalid")
                artifact_value = {
                    "schema_version": 1, "run_id": run_id, "source_asset_id": snapshot["source_asset_id"],
                    "input_fingerprint": job["input_fingerprint"], "status": status,
                    "segments": [{**segment, "flags": segment["flags"]} for segment in segment_rows],
                }
                artifact_bytes = json.dumps(artifact_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                artifact = self.artifacts.publish(
                    project_id=job["project_id"], job_id=job["id"], attempt_id=attempt["id"],
                    kind="transcript_json", input_fingerprint=job["input_fingerprint"], fingerprint_version=1,
                    source=artifact_bytes,
                )
                evidence = self.artifacts.completion_evidence(job["id"], attempt["id"], [artifact["id"]])
                stamp = utc_now()
                timing = {
                    "source_duration_us": snapshot["source_duration_us"], "audio_duration_us": extraction.duration_us,
                    "audio_start_offset_us": extraction.source_offset_us,
                    "source_video_start_us": extraction.source_video_start_us,
                    "audio_stream_index": extraction.audio_stream_index,
                    "model_load_seconds": result["model_load_seconds"], "inference_seconds": result["inference_seconds"],
                }
                provenance = {
                    "source_sha256": snapshot["source_sha256"], "source_size": snapshot["source_size"],
                    "source_mode": snapshot["source_mode"], "config": {key: snapshot[key] for key in (
                        "language", "device", "vad_enabled", "beam_size", "allow_cpu_fallback")},
                    "requested_device": snapshot["device"], "actual_device": result["actual_device"],
                    "compute_type": result["compute_type"], "fallback_reason": result["fallback_reason"],
                    "model_id": result["model_id"], "model_revision": result["model_revision"],
                    "faster_whisper_version": result["faster_whisper_version"],
                    "ctranslate2_version": result["ctranslate2_version"],
                    "language_probability": result["language_probability"],
                    "extractor_version": EXTRACTOR_VERSION,
                }

                def persist_run(connection):
                    connection.execute(
                        """INSERT INTO transcription_runs(id,project_id,source_asset_id,artifact_id,input_fingerprint,model_id,model_revision,
                           faster_whisper_version,ctranslate2_version,language,language_probability,vad_enabled,requested_device,actual_device,
                           compute_type,status,duration_us,audio_start_offset_us,timing_json,provenance_json,created_at,completed_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, job["project_id"], snapshot["source_asset_id"], artifact["id"], job["input_fingerprint"],
                         result["model_id"], result["model_revision"], result["faster_whisper_version"], result["ctranslate2_version"],
                         result["language"], result["language_probability"], int(snapshot["vad_enabled"]), snapshot["device"],
                         result["actual_device"], result["compute_type"], status, snapshot["source_duration_us"],
                         extraction.source_offset_us, json.dumps(timing, allow_nan=False), json.dumps(provenance, ensure_ascii=False, allow_nan=False), stamp, stamp),
                    )
                    connection.executemany(
                        """INSERT INTO segments(id,project_id,run_id,position,start_us,end_us,source_text,avg_logprob,no_speech_prob,
                           compression_ratio,flags_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        [(segment["id"], job["project_id"], run_id, segment["position"], segment["start_us"], segment["end_us"],
                          segment["source_text"], segment["avg_logprob"], segment["no_speech_prob"], segment["compression_ratio"],
                          json.dumps(segment["flags"], separators=(",", ":"))) for segment in segment_rows],
                    )

                progress("publishing_transcript")
                phase = "publishing_transcript"
                completed = self.jobs.complete_attempt(job["id"], attempt["id"], evidence, completion_hook=persist_run)
                return completed["status"] == "READY"
            finally:
                try:
                    scratch.unlink(missing_ok=True)
                except OSError:
                    pass
        except (ASRError, MediaError, ArtifactError, JobInputError, JobConflict) as exc:
            code = exc.code if isinstance(exc, (ASRError, MediaError, ArtifactError, JobConflict)) else str(exc)
            cause = exc.__cause__ or exc
            self._write_diagnostic(job["id"], attempt_id, phase, code, cause)
            try:
                with closing(self.database.connect()) as connection:
                    attempt = connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'", (job["id"],)).fetchone()
                if attempt:
                    retryable = code in {"ffmpeg_timeout", "model_download_failed", "asr_inference_failed", "source_unavailable"}
                    self.jobs.fail_attempt(job["id"], attempt["id"], code, retryable=retryable)
            except Exception:
                pass
            return True
        except Exception as exc:
            self._write_diagnostic(job["id"], attempt_id, phase, "asr_worker_failed", exc)
            try:
                with closing(self.database.connect()) as connection:
                    attempt = connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'", (job["id"],)).fetchone()
                if attempt:
                    self.jobs.fail_attempt(job["id"], attempt["id"], "asr_worker_failed", retryable=False)
            except Exception:
                pass
            return True
        finally:
            with self.active_lock:
                self.active_job_id = None


class ASRWorker:
    def __init__(self, service: ASRService):
        self.service = service
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="autodub-asr-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.service.active_lock:
            active_job_id = self.service.active_job_id
        if active_job_id:
            try:
                self.service.jobs.request_cancel(active_job_id, f"shutdown-{uuid.uuid4().hex}")
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=8)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                worked = self.service.run_once()
            except Exception:
                worked = False
            if not worked:
                self.stop_event.wait(0.25)

