"""Trusted Windows selection and durable source-media import."""

from __future__ import annotations

import hashlib
import ctypes
import json
import os
import shutil
import stat
import re
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from app.artifacts import ArtifactConflict, ArtifactError, ArtifactPublicationService
from app.database import Database, IdempotencyConflict, utc_now
from app.jobs import JobService, invalidate_translation_input

_STREAM_CHUNK_SIZE = 1024 * 1024


class MediaError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parse_single_byte_range(value: str, size: int) -> tuple[int, int]:
    if not isinstance(value, str) or "," in value:
        raise MediaError("invalid_range")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip(), flags=re.IGNORECASE)
    if match is None or (not match.group(1) and not match.group(2)):
        raise MediaError("invalid_range")
    first, last = match.groups()
    if size <= 0:
        raise MediaError("range_not_satisfiable")
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                raise MediaError("range_not_satisfiable")
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
            if start >= size or end < start:
                raise MediaError("range_not_satisfiable")
    except ValueError as exc:
        raise MediaError("invalid_range") from exc
    return start, end


def _media_content_type(metadata: dict[str, Any]) -> str:
    container = metadata.get("container")
    if not isinstance(container, str):
        raise MediaError("media_type_unsupported")
    formats = {name.strip().lower() for name in container.split(",")}
    if "webm" in formats:
        return "video/webm"
    if "mp4" in formats or "m4v" in formats or "mp4" in container.lower():
        return "video/mp4"
    if "mov" in formats or "quicktime" in formats:
        return "video/quicktime"
    if "matroska" in formats:
        return "video/x-matroska"
    if "avi" in formats:
        return "video/x-msvideo"
    if "mpeg" in formats or "mpegts" in formats:
        return "video/mpeg"
    if "asf" in formats:
        return "video/x-ms-asf"
    raise MediaError("media_type_unsupported")


class OpenMedia:
    def __init__(self, *, asset_id: str, mode: str, handle: BinaryIO, size: int, sha256: str,
                 identity: dict[str, int], content_type: str):
        self.asset_id, self.mode, self.handle = asset_id, mode, handle
        self.size, self.sha256, self.identity, self.content_type = size, sha256, identity, content_type

    @property
    def etag(self) -> str:
        return f'"{self.sha256}"'


def _path_identity(path: Path) -> dict[str, int]:
    raw = str(path)
    if raw.startswith("\\\\"):
        raise MediaError("network_source_unsupported")
    if os.name == "nt" and path.drive:
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(f"{path.drive}\\") == 4:
                raise MediaError("network_source_unsupported")
        except AttributeError:
            raise MediaError("source_identity_unavailable")
        name = path.name
        if ":" in name or name.endswith((".", " ")) or name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise MediaError("unsafe_source")
    cursor = path.absolute()
    for component in (cursor, *cursor.parents):
        try:
            component_info = component.lstat()
        except OSError as exc:
            raise MediaError("source_unavailable") from exc
        if component.is_symlink() or getattr(component_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise MediaError("unsafe_source")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise MediaError("source_unavailable") from exc
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
        raise MediaError("unsafe_source")
    return {"device": int(info.st_dev), "file_id": int(info.st_ino), "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns)}


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(source.fileno())
    except OSError as exc:
        raise MediaError("source_unavailable") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise MediaError("source_changed")
    return digest.hexdigest(), size


class FFprobe:
    def __init__(self, executable: str | None = None, timeout: float = 60):
        self.executable = executable or shutil.which("ffprobe")
        self.timeout = timeout

    def probe(self, path: Path, cancelled=None) -> dict[str, Any]:
        if not self.executable:
            raise MediaError("ffprobe_unavailable")
        try:
            process = subprocess.Popen(
                [self.executable, "-v", "error", "-show_entries",
                 "format=format_name,duration:format_tags=major_brand:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels,channel_layout",
                 "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + self.timeout
            while process.poll() is None:
                if cancelled and cancelled():
                    process.kill()
                    process.communicate()
                    raise MediaError("job_cancelled")
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise MediaError("ffprobe_timeout")
                time.sleep(0.05)
            stdout, stderr = process.communicate()
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            raise MediaError("ffprobe_timeout") from exc
        except OSError as exc:
            raise MediaError("ffprobe_unavailable") from exc
        if result.returncode != 0:
            error_text = result.stderr.decode("utf-8", errors="replace").lower()
            corrupt_markers = ("invalid data found", "moov atom not found", "truncated", "end of file")
            raise MediaError("media_corrupt" if any(marker in error_text for marker in corrupt_markers) else "media_unsupported")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
            streams = value["streams"]
            fmt = value.get("format", {})
            video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
            if video is None:
                raise MediaError("video_stream_missing")
            duration = float(fmt.get("duration", "0"))
            if duration <= 0:
                raise MediaError("media_corrupt")
            return {
                "container": str(fmt.get("format_name", "unknown"))[:128],
                "duration_us": round(duration * 1_000_000),
                "width": int(video.get("width", 0)), "height": int(video.get("height", 0)),
                "video_codec": str(video.get("codec_name", "unknown"))[:80],
                "audio_streams": sum(1 for stream in streams if stream.get("codec_type") == "audio"),
                "streams": [{key: stream.get(key) for key in ("index", "codec_type", "codec_name", "width", "height", "avg_frame_rate", "r_frame_rate", "sample_rate", "channels", "channel_layout")} for stream in streams],
                "frame_rate": video.get("avg_frame_rate") or video.get("r_frame_rate"),
            }
        except MediaError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise MediaError("media_corrupt") from exc


class SourceVideoValidator:
    version = "ffprobe-source-v1"

    def __init__(self, ffprobe: FFprobe):
        self.ffprobe = ffprobe

    def validate(self, path: Path) -> dict[str, Any]:
        metadata = self.ffprobe.probe(path)
        return {"container": metadata["container"], "duration_us": metadata["duration_us"],
                "width": metadata["width"], "height": metadata["height"], "video_codec": metadata["video_codec"]}


class LinkManifestValidator:
    version = "media-link-manifest-v1"

    def validate(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if len(value.get("source_sha256", "")) != 64 or int(value.get("size", -1)) < 0:
                raise ValueError
            return {"manifest_type": "link", "schema_version": 1, "size": int(value["size"])}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MediaError("link_manifest_invalid") from exc


class MediaImportService:
    SELECTION_TTL = 300

    def __init__(self, database: Database, jobs: JobService, artifacts: ArtifactPublicationService,
                 ffprobe: FFprobe | None = None, helper_path: Path | None = None):
        self.database, self.jobs, self.artifacts = database, jobs, artifacts
        self.ffprobe = ffprobe or FFprobe()
        self.helper_path = helper_path or Path(__file__).resolve().parents[2] / "scripts" / "windows" / "picker.ps1"
        self.active_job_id: str | None = None
        self.active_lock = threading.Lock()
        self._stream_verifications: dict[tuple[str, str], tuple[int, int, int, int]] = {}
        self._stream_lock = threading.Lock()

    @staticmethod
    def session_hash(session_token: str) -> str:
        return hashlib.sha256(session_token.encode("utf-8")).hexdigest()

    def choose_source(self, session_token: str, project_id: str, *, purpose: str = "source", asset_id: str | None = None) -> dict[str, str]:
        if purpose not in {"source", "relink"} or (purpose == "source" and asset_id is not None) or (purpose == "relink" and not asset_id):
            raise MediaError("invalid_selection_purpose")
        if os.name != "nt":
            raise MediaError("picker_unavailable")
        nonce = os.urandom(32).hex()
        if not self.helper_path.is_file():
            raise MediaError("picker_unavailable")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            result = subprocess.run(
                [str(powershell), "-NoLogo", "-NoProfile", "-STA", "-File", str(self.helper_path)],
                input=json.dumps({"nonce": nonce, "purpose": purpose, "project_id": project_id, "asset_id": asset_id}),
                text=True, encoding="utf-8", capture_output=True, timeout=180, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError("picker_timeout") from exc
        except OSError as exc:
            raise MediaError("picker_unavailable") from exc
        if result.returncode != 0:
            raise MediaError("picker_failed")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaError("picker_protocol_invalid") from exc
        if not isinstance(response, dict) or response.get("nonce") != nonce:
            raise MediaError("picker_protocol_invalid")
        if response.get("cancelled") is True:
            raise MediaError("selection_cancelled")
        raw_path = response.get("path")
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise MediaError("picker_protocol_invalid")
        path = Path(raw_path)
        identity = _path_identity(path)
        display_name = path.name
        token = os.urandom(32).hex()
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                    raise MediaError("project_not_found")
                if purpose == "relink":
                    asset = connection.execute("SELECT mode,status FROM media_assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
                    if asset is None or asset["mode"] != "LINK" or asset["status"] == "READY":
                        raise MediaError("relink_not_allowed")
                connection.execute(
                    "INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,asset_id,created_at) VALUES(?,?,?,?,?,?,?,?,'ISSUED',?,?)",
                    (token_hash, self.session_hash(session_token), project_id, purpose, str(path), display_name,
                     json.dumps(identity), time.time() + self.SELECTION_TTL, asset_id, utc_now()),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"selection_id": token, "display_name": display_name, "expires_in_seconds": self.SELECTION_TTL}

    def create_import_job(self, session_token: str, project_id: str, selection_id: str, mode: str, key: str) -> dict[str, Any]:
        if mode not in {"COPY", "LINK"}:
            raise MediaError("invalid_import_mode")
        self.jobs.validate_idempotency_key(key)
        token_hash = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
        scope = f"job:create:{project_id}:media_import"
        snapshot: dict[str, Any]
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                selection = connection.execute("SELECT * FROM selections WHERE id_hash=?", (token_hash,)).fetchone()
                if selection is None or selection["session_hash"] != self.session_hash(session_token) or selection["project_id"] != project_id or selection["purpose"] != "source" or selection["asset_id"] is not None:
                    raise MediaError("selection_invalid")
                prior = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
                request = {"project_id": project_id, "selection_hash": token_hash, "mode": mode}
                digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if prior:
                    if prior["request_digest"] != digest:
                        raise IdempotencyConflict
                    job_id = json.loads(prior["response_json"])["job_id"]
                    connection.commit()
                    return self.jobs.get_job(job_id, project_id)
                if selection["state"] != "ISSUED":
                    raise MediaError("selection_consumed")
                if selection["expires_at"] <= time.time():
                    connection.execute("UPDATE selections SET state='EXPIRED' WHERE id_hash=?", (token_hash,))
                    raise MediaError("selection_expired")
                path = Path(selection["private_path"])
                identity = _path_identity(path)
                if identity != json.loads(selection["file_identity_json"]):
                    raise MediaError("source_changed")
                project = connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
                if project is None:
                    raise MediaError("project_not_found")
                connection.execute("INSERT OR IGNORE INTO media_import_state(project_id,desired_generation,active_generation) VALUES(?,0,0)", (project_id,))
                state = connection.execute("SELECT desired_generation FROM media_import_state WHERE project_id=?", (project_id,)).fetchone()
                generation = state["desired_generation"] + 1
                connection.execute("UPDATE media_import_state SET desired_generation=? WHERE project_id=?", (generation, project_id))
                snapshot = {"schema_version": 1, "mode": mode, "selection_ref": token_hash, "display_name": selection["display_name"], "source_identity": identity, "source_generation": generation}
                snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fingerprint = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
                job_id = str(uuid.uuid4())
                stamp = utc_now()
                connection.execute("INSERT INTO jobs(id,project_id,kind,input_snapshot,input_fingerprint,idempotency_key,request_digest,status,created_at,updated_at) VALUES(?,?, 'media_import',?,?,?,?,'QUEUED',?,?)", (job_id, project_id, snapshot_json, fingerprint, key, digest, stamp, stamp))
                connection.execute("INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)", (scope, key, digest, project_id, json.dumps({"job_id": job_id}), stamp))
                connection.execute("UPDATE selections SET state='CONSUMED',consumed_job_id=? WHERE id_hash=? AND state='ISSUED'", (job_id, token_hash))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.jobs.get_job(job_id, project_id)

    def list_assets(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute("SELECT m.id,m.mode,m.status,m.display_name,m.private_source_path,m.source_identity_json,m.managed_artifact_id,m.sha256,m.file_size,m.metadata_json,m.created_at,m.lock_version,a.status AS artifact_status FROM media_assets m LEFT JOIN artifacts a ON a.id=m.managed_artifact_id WHERE m.project_id=? AND m.status<>'PENDING' ORDER BY m.created_at DESC,m.id", (project_id,)).fetchall()
        items = []
        for row in rows:
            status = row["status"]
            if row["mode"] == "LINK":
                try:
                    current_identity = _path_identity(Path(row["private_source_path"]))
                    status = "READY" if current_identity == json.loads(row["source_identity_json"]) else "CHANGED"
                except MediaError:
                    status = "MISSING"
            elif row["artifact_status"] in {"MISSING", "CORRUPT", "FAILED"}:
                status = "MISSING" if row["artifact_status"] == "MISSING" else "CHANGED"
            if status != row["status"]:
                with closing(self.database.connect()) as connection:
                    connection.execute("UPDATE media_assets SET status=?,lock_version=lock_version+1 WHERE id=? AND status<>'PENDING' AND status<>?", (status, row["id"], status))
            lock_version = row["lock_version"] + (1 if status != row["status"] else 0)
            items.append({"id": row["id"], "mode": row["mode"], "status": status, "display_name": row["display_name"], "sha256": row["sha256"], "file_size": row["file_size"], "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"], "lock_version": lock_version})
        return items

    def get_processing_source(self, project_id: str, *, auto_select_single: bool = True) -> dict[str, Any]:
        assets = {item["id"]: item for item in self.list_assets(project_id)}
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT media_asset_id,generation FROM project_processing_sources WHERE project_id=?", (project_id,)).fetchone()
            if row is None and auto_select_single:
                ready = [asset for asset in assets.values() if asset["status"] == "READY"]
                if len(ready) == 1:
                    stamp = utc_now()
                    connection.execute(
                        "INSERT INTO project_processing_sources(project_id,media_asset_id,generation,updated_at) VALUES(?,?,1,?)",
                        (project_id, ready[0]["id"], stamp),
                    )
                    row = connection.execute("SELECT media_asset_id,generation FROM project_processing_sources WHERE project_id=?", (project_id,)).fetchone()
            connection.commit()
        if row is None:
            return {"media_asset_id": None, "generation": 0, "status": "UNSELECTED", "asset": None}
        asset = assets.get(row["media_asset_id"])
        status = asset["status"] if asset else "MISSING"
        return {"media_asset_id": row["media_asset_id"], "generation": row["generation"], "status": status, "asset": asset}

    def select_processing_source(self, project_id: str, asset_id: str, expected_generation: int) -> dict[str, Any]:
        if type(expected_generation) is not int or expected_generation < 0:
            raise MediaError("invalid_source_generation")
        assets = {item["id"]: item for item in self.list_assets(project_id)}
        asset = assets.get(asset_id)
        if asset is None:
            raise MediaError("asset_not_found")
        if asset["status"] != "READY":
            raise MediaError("asset_unavailable" if asset["status"] == "MISSING" else "asset_changed")
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT media_asset_id,generation FROM project_processing_sources WHERE project_id=?", (project_id,)).fetchone()
            current_generation = row["generation"] if row else 0
            if current_generation != expected_generation:
                connection.rollback()
                raise MediaError("source_selection_conflict")
            if row and row["media_asset_id"] == asset_id:
                generation = row["generation"]
            else:
                generation = current_generation + 1
                connection.execute(
                    "INSERT INTO project_processing_sources(project_id,media_asset_id,generation,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(project_id) DO UPDATE SET media_asset_id=excluded.media_asset_id,generation=excluded.generation,updated_at=excluded.updated_at",
                    (project_id, asset_id, generation, utc_now()),
                )
                invalidate_translation_input(connection, project_id, "processing_source_changed", f"{asset_id}:{generation}")
            connection.commit()
        return {"media_asset_id": asset_id, "generation": generation, "status": "READY", "asset": asset}

    def relink_asset(self, session_token: str, project_id: str, asset_id: str, selection_id: str,
                     expected_lock_version: int, idempotency_key: str) -> dict[str, Any]:
        try:
            project_id, asset_id = str(uuid.UUID(project_id)), str(uuid.UUID(asset_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MediaError("asset_not_found") from exc
        if type(expected_lock_version) is not int or expected_lock_version < 1:
            raise MediaError("invalid_lock_version")
        self.jobs.validate_idempotency_key(idempotency_key)
        token_hash = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
        scope = f"media:relink:{asset_id}"
        request_value = {"project_id": project_id, "asset_id": asset_id, "selection_hash": token_hash,
                         "expected_lock_version": expected_lock_version}
        request_digest = hashlib.sha256(json.dumps(request_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        session_digest = self.session_hash(session_token)
        with closing(self.database.connect()) as connection:
            selection = connection.execute("SELECT * FROM selections WHERE id_hash=?", (token_hash,)).fetchone()
            if selection is None or selection["session_hash"] != session_digest or selection["project_id"] != project_id or selection["purpose"] != "relink" or selection["asset_id"] != asset_id:
                raise MediaError("selection_invalid")
            previous = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, idempotency_key)).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise IdempotencyConflict
                return json.loads(previous["response_json"])
            if selection["state"] != "ISSUED":
                raise MediaError("selection_consumed")
            if selection["expires_at"] <= time.time():
                raise MediaError("selection_expired")
            asset = connection.execute("SELECT * FROM media_assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
            if asset is None or asset["mode"] != "LINK":
                raise MediaError("asset_not_found")
            if asset["status"] not in {"MISSING", "CHANGED"}:
                raise MediaError("relink_not_required")
            if asset["lock_version"] != expected_lock_version:
                raise MediaError("asset_revision_conflict")
            selection_snapshot = dict(selection)
            asset_snapshot = dict(asset)

        path = Path(selection_snapshot["private_path"])
        try:
            identity = _path_identity(path)
            if identity != json.loads(selection_snapshot["file_identity_json"]):
                raise MediaError("source_changed")
            digest, size = _hash_file(path)
            metadata = self.ffprobe.probe(path)
            if _path_identity(path) != identity:
                raise MediaError("source_changed")
        except MediaError:
            raise
        except OSError as exc:
            raise MediaError("source_unavailable") from exc

        previous_metadata = json.loads(asset_snapshot["metadata_json"])
        if digest != asset_snapshot["sha256"] or size != asset_snapshot["file_size"] or metadata != previous_metadata:
            with closing(self.database.connect()) as connection:
                connection.execute("UPDATE selections SET state='REVOKED' WHERE id_hash=? AND state='ISSUED'", (token_hash,))
            raise MediaError("relink_content_mismatch")

        stamp = utc_now()
        response = {"asset_id": asset_id, "status": "READY", "lock_version": expected_lock_version + 1}
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, idempotency_key)).fetchone()
                if previous:
                    if previous["request_digest"] != request_digest:
                        raise IdempotencyConflict
                    connection.commit()
                    return json.loads(previous["response_json"])
                selection = connection.execute("SELECT * FROM selections WHERE id_hash=?", (token_hash,)).fetchone()
                asset = connection.execute("SELECT * FROM media_assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
                if selection is None or selection["session_hash"] != session_digest or selection["purpose"] != "relink" or selection["asset_id"] != asset_id or selection["state"] != "ISSUED":
                    raise MediaError("selection_consumed")
                if selection["expires_at"] <= time.time():
                    raise MediaError("selection_expired")
                if asset is None or asset["lock_version"] != expected_lock_version or asset["status"] not in {"MISSING", "CHANGED"}:
                    raise MediaError("asset_revision_conflict")
                connection.execute(
                    "INSERT INTO media_relink_history(id,asset_id,selection_hash,previous_private_source_path,previous_identity_json,previous_sha256,new_private_source_path,new_identity_json,relinked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), asset_id, token_hash, asset["private_source_path"], asset["source_identity_json"], asset["sha256"], selection["private_path"], json.dumps(identity), stamp),
                )
                changed = connection.execute(
                    "UPDATE media_assets SET private_source_path=?,source_identity_json=?,status='READY',lock_version=lock_version+1 WHERE id=? AND project_id=? AND mode='LINK' AND status IN ('MISSING','CHANGED') AND lock_version=?",
                    (selection["private_path"], json.dumps(identity), asset_id, project_id, expected_lock_version),
                ).rowcount
                if changed != 1:
                    raise MediaError("asset_revision_conflict")
                connection.execute("UPDATE selections SET state='CONSUMED' WHERE id_hash=? AND state='ISSUED'", (token_hash,))
                connection.execute("INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)", (scope, idempotency_key, request_digest, project_id, json.dumps(response), stamp))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        with self._stream_lock:
            self._stream_verifications.pop((asset_id, "LINK"), None)
        return response

    @staticmethod
    def _identity_tuple(identity: dict[str, int]) -> tuple[int, int, int, int]:
        return identity["device"], identity["file_id"], identity["size"], identity["mtime_ns"]

    @staticmethod
    def _hash_handle(handle: BinaryIO) -> tuple[str, int, tuple[int, int, int, int]]:
        handle.seek(0)
        before = os.fstat(handle.fileno())
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(_STREAM_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(handle.fileno())
        before_id = (int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns))
        after_id = (int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns))
        if before_id != after_id or size != int(after.st_size):
            raise MediaError("source_changed")
        handle.seek(0)
        return digest.hexdigest(), size, after_id

    def _mark_media_status(self, asset: dict[str, Any], status: str) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("UPDATE media_assets SET status=?,lock_version=lock_version+1 WHERE id=? AND status<>?", (status, asset["id"], status))
        if asset["mode"] == "COPY" and status in {"MISSING", "CHANGED"}:
            target = "MISSING" if status == "MISSING" else "CORRUPT"
            try:
                artifact = self.artifacts.get_artifact(asset["managed_artifact_id"])
                if artifact["status"] == "READY":
                    self.artifacts._mark_integrity_failure(artifact["id"], "READY", target, "artifact_integrity_failed")
            except (ArtifactError, OSError):
                pass

    def open_media_stream(self, project_id: str, asset_id: str) -> OpenMedia:
        try:
            project_id = str(uuid.UUID(project_id))
            asset_id = str(uuid.UUID(asset_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MediaError("asset_not_found") from exc
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM media_assets WHERE id=? AND project_id=?",
                (asset_id, project_id),
            ).fetchone()
        if row is None or row["status"] == "PENDING":
            raise MediaError("asset_not_found")
        if row["status"] != "READY":
            raise MediaError("asset_unavailable")
        asset = dict(row)
        metadata = json.loads(row["metadata_json"])
        content_type = _media_content_type(metadata)
        if row["mode"] == "COPY":
            try:
                artifact = self.artifacts.get_artifact(row["managed_artifact_id"], project_id)
            except ArtifactError as exc:
                raise MediaError("asset_unavailable") from exc
            if artifact["kind"] != "source_video" or artifact["sha256"] != row["sha256"] or artifact["file_size"] != row["file_size"]:
                self._mark_media_status(asset, "CHANGED")
                raise MediaError("asset_changed")
            try:
                path = self.artifacts._path(artifact["managed_relative_path"])
                identity = _path_identity(path)
            except (ArtifactError, MediaError, OSError) as exc:
                self._mark_media_status(asset, "MISSING" if isinstance(exc, FileNotFoundError) else "CHANGED")
                raise MediaError("asset_unavailable") from exc
        elif row["mode"] == "LINK":
            path = Path(row["private_source_path"])
            try:
                identity = _path_identity(path)
            except MediaError as exc:
                self._mark_media_status(asset, "MISSING" if exc.code == "source_unavailable" else "CHANGED")
                raise MediaError("asset_unavailable" if exc.code == "source_unavailable" else "asset_changed") from exc
            if identity != json.loads(row["source_identity_json"]):
                self._mark_media_status(asset, "CHANGED")
                raise MediaError("asset_changed")
        else:
            raise MediaError("asset_unavailable")

        try:
            handle = path.open("rb")
        except OSError as exc:
            self._mark_media_status(asset, "MISSING")
            raise MediaError("asset_unavailable") from exc
        try:
            opened = os.fstat(handle.fileno())
            opened_identity = (int(opened.st_dev), int(opened.st_ino), int(opened.st_size), int(opened.st_mtime_ns))
            expected_identity = self._identity_tuple(identity)
            if opened_identity != expected_identity:
                raise MediaError("asset_changed")
            cache_key = (asset_id, row["mode"])
            with self._stream_lock:
                verified_identity = self._stream_verifications.get(cache_key)
            if verified_identity != opened_identity:
                if row["mode"] == "COPY":
                    self.artifacts.verify_ready(row["managed_artifact_id"], project_id)
                sha256, size, hashed_identity = self._hash_handle(handle)
                if size != row["file_size"] or sha256 != row["sha256"] or hashed_identity != opened_identity:
                    raise MediaError("asset_changed")
                with self._stream_lock:
                    self._stream_verifications[cache_key] = opened_identity
            return OpenMedia(asset_id=asset_id, mode=row["mode"], handle=handle, size=row["file_size"], sha256=row["sha256"], identity=identity, content_type=content_type)
        except ArtifactConflict as exc:
            handle.close()
            status = "MISSING" if exc.code == "artifact_missing" else "CHANGED"
            self._mark_media_status(asset, status)
            raise MediaError("asset_unavailable" if status == "MISSING" else "asset_changed") from exc
        except ArtifactError as exc:
            handle.close()
            status = "MISSING" if exc.code == "artifact_missing" else "CHANGED"
            self._mark_media_status(asset, status)
            raise MediaError("asset_unavailable" if status == "MISSING" else "asset_changed") from exc
        except MediaError as exc:
            handle.close()
            status = "MISSING" if exc.code == "source_unavailable" else "CHANGED"
            self._mark_media_status(asset, status)
            raise MediaError("asset_unavailable" if status == "MISSING" else "asset_changed") from exc
        except OSError as exc:
            handle.close()
            self._mark_media_status(asset, "CHANGED")
            raise MediaError("asset_changed") from exc
        except BaseException:
            handle.close()
            raise

    def get_transcription_source(self, project_id: str, asset_id: str) -> dict[str, Any]:
        """Return an internal verified path snapshot for the supervised ASR worker only."""
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM media_assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
        if row is None or row["status"] != "READY":
            raise MediaError("asset_unavailable")
        opened = self.open_media_stream(project_id, asset_id)
        opened.handle.close()
        if row["mode"] == "COPY":
            artifact = self.artifacts.get_artifact(row["managed_artifact_id"], project_id)
            path = self.artifacts._path(artifact["managed_relative_path"])
        elif row["mode"] == "LINK":
            path = Path(row["private_source_path"])
        else:
            raise MediaError("asset_unavailable")
        identity = _path_identity(path)
        if identity["size"] != row["file_size"] or _hash_file(path) != (row["sha256"], row["file_size"]):
            self._mark_media_status(dict(row), "CHANGED")
            raise MediaError("asset_changed")
        return {
            "asset_id": asset_id, "project_id": project_id, "mode": row["mode"], "path": path,
            "sha256": row["sha256"], "file_size": row["file_size"],
            "duration_us": json.loads(row["metadata_json"])["duration_us"], "identity": self._identity_tuple(identity),
        }

    def verify_transcription_source(self, snapshot: dict[str, Any]) -> bool:
        path = snapshot["path"]
        try:
            identity = _path_identity(path)
            unchanged = (
                self._identity_tuple(identity) == snapshot["identity"] and
                identity["size"] == snapshot["file_size"] and
                _hash_file(path) == (snapshot["sha256"], snapshot["file_size"])
            )
        except (MediaError, OSError):
            unchanged = False
        if not unchanged:
            with closing(self.database.connect()) as connection:
                row = connection.execute("SELECT * FROM media_assets WHERE id=? AND project_id=?", (snapshot["asset_id"], snapshot["project_id"])).fetchone()
            if row is not None:
                self._mark_media_status(dict(row), "CHANGED")
        return unchanged

    def stream_chunks(self, opened: OpenMedia, start: int, length: int) -> Iterator[bytes]:
        remaining = length
        digest = hashlib.sha256()
        try:
            opened.handle.seek(start)
            while remaining:
                chunk = opened.handle.read(min(_STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    self._mark_stream_changed(opened)
                    raise MediaError("asset_changed")
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            info = os.fstat(opened.handle.fileno())
            current = (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))
            if current != self._identity_tuple(opened.identity) or (start == 0 and length == opened.size and digest.hexdigest() != opened.sha256):
                self._mark_stream_changed(opened)
                raise MediaError("asset_changed")
        except (OSError, ValueError):
            self._mark_stream_changed(opened)
            raise
        finally:
            opened.handle.close()

    def _mark_stream_changed(self, opened: OpenMedia) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("UPDATE media_assets SET status='CHANGED',lock_version=lock_version+1 WHERE id=? AND status='READY'", (opened.asset_id,))
        with self._stream_lock:
            self._stream_verifications.pop((opened.asset_id, opened.mode), None)

    def reconcile_pending_assets(self) -> int:
        with closing(self.database.connect()) as connection:
            rows = connection.execute("SELECT a.*,j.status AS job_status FROM media_assets a JOIN jobs j ON j.id=a.producer_job_id WHERE a.status='PENDING'").fetchall()
        promoted = 0
        for row in rows:
            if row["job_status"] != "READY":
                continue
            if row["mode"] == "LINK":
                try:
                    path = Path(row["private_source_path"])
                    if _path_identity(path) != json.loads(row["source_identity_json"]) or _hash_file(path) != (row["sha256"], row["file_size"]):
                        continue
                except (MediaError, OSError, ValueError):
                    continue
            try:
                self.artifacts.verify_ready(row["managed_artifact_id"], row["project_id"])
            except ArtifactError:
                continue
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    state = connection.execute("SELECT desired_generation,active_generation FROM media_import_state WHERE project_id=?", (row["project_id"],)).fetchone()
                    changed = connection.execute("UPDATE media_assets SET status='READY' WHERE id=? AND status='PENDING' AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='READY')", (row["id"], row["producer_job_id"])).rowcount
                    if changed and state and state["desired_generation"] == row["generation"] and row["generation"] > state["active_generation"]:
                        connection.execute("UPDATE media_import_state SET active_generation=?,active_asset_id=? WHERE project_id=? AND desired_generation=?", (row["generation"], row["id"], row["project_id"], row["generation"]))
                    connection.commit()
                    promoted += changed
                except BaseException:
                    connection.rollback()
                    raise
        return promoted

    def import_once(self) -> bool:
        job = self.jobs.claim_next_job(kind="media_import")
        if job is None:
            return False
        with self.active_lock:
            self.active_job_id = job["id"]
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT j.input_snapshot,s.private_path FROM jobs j JOIN selections s ON s.consumed_job_id=j.id WHERE j.id=?", (job["id"],)).fetchone()
            attempt = connection.execute("SELECT id FROM job_attempts WHERE job_id=? AND status='RUNNING'", (job["id"],)).fetchone()
        if row is None or attempt is None:
            if attempt is not None:
                self.jobs.fail_attempt(job["id"], attempt["id"], "authorized_source_missing", retryable=False)
            with self.active_lock:
                self.active_job_id = None
            return True
        snapshot = json.loads(row["input_snapshot"])
        path = Path(row["private_path"])
        def cancelled() -> bool:
            with closing(self.database.connect()) as connection:
                status = connection.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            return status is None or status["status"] == "CANCEL_REQUESTED"

        try:
            if cancelled():
                self.jobs.fail_attempt(job["id"], attempt["id"], "job_cancelled", retryable=False)
                with self.active_lock:
                    self.active_job_id = None
                return True
            if _path_identity(path) != snapshot["source_identity"]:
                raise MediaError("source_changed")
            free = shutil.disk_usage(self.database.data_root).free
            mode = snapshot["mode"]
            before_hash, size = _hash_file(path)
            if size != snapshot["source_identity"]["size"]:
                raise MediaError("source_changed")
            if mode == "COPY" and free < size + max(16 * 1024 * 1024, size // 20):
                raise MediaError("insufficient_disk_space")
            metadata = self.ffprobe.probe(path, cancelled)
            if mode == "COPY":
                with path.open("rb") as source:
                    def chunks():
                        while True:
                            if cancelled():
                                raise MediaError("job_cancelled")
                            block = source.read(1024 * 1024)
                            if not block:
                                return
                            yield block
                    artifact = self.artifacts.publish(project_id=job["project_id"], job_id=job["id"], attempt_id=attempt["id"], kind="source_video", input_fingerprint=job["input_fingerprint"], fingerprint_version=1, source=chunks())
                if artifact["sha256"] != before_hash or artifact["file_size"] != size:
                    raise MediaError("source_changed")
                content_artifact_id = artifact["id"]
            else:
                content_artifact_id = None
                # A small verified manifest satisfies CORE-01 completion evidence; the LINK source itself is never copied.
                manifest = json.dumps({"source_sha256": before_hash, "size": size, "metadata": metadata}, ensure_ascii=False).encode("utf-8")
                artifact = self.artifacts.publish(project_id=job["project_id"], job_id=job["id"], attempt_id=attempt["id"], kind="media_link_manifest", input_fingerprint=job["input_fingerprint"], fingerprint_version=1, source=manifest)
            if _path_identity(path) != snapshot["source_identity"] or _hash_file(path) != (before_hash, size):
                raise MediaError("source_changed")
            asset_id = str(uuid.uuid4())
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    previous_candidate = connection.execute("SELECT id FROM media_assets WHERE producer_job_id=? AND generation=? AND status='PENDING'", (job["id"], snapshot["source_generation"])).fetchone()
                    if previous_candidate:
                        asset_id = previous_candidate["id"]
                        connection.execute("UPDATE media_assets SET mode=?,display_name=?,private_source_path=?,managed_artifact_id=?,sha256=?,file_size=?,metadata_json=?,source_identity_json=? WHERE id=? AND status='PENDING'", (mode, snapshot["display_name"], str(path) if mode == "LINK" else None, content_artifact_id or artifact["id"], before_hash, size, json.dumps(metadata, ensure_ascii=False), json.dumps(snapshot["source_identity"]), asset_id))
                    else:
                        connection.execute("INSERT INTO media_assets(id,project_id,mode,status,display_name,private_source_path,managed_artifact_id,sha256,file_size,metadata_json,source_identity_json,producer_job_id,generation,created_at) VALUES(?,?,?,'PENDING',?,?,?,?,?,?,?,?,?,?)", (asset_id, job["project_id"], mode, snapshot["display_name"], str(path) if mode == "LINK" else None, content_artifact_id or artifact["id"], before_hash, size, json.dumps(metadata, ensure_ascii=False), json.dumps(snapshot["source_identity"]), job["id"], snapshot["source_generation"], utc_now()))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            evidence = self.artifacts.completion_evidence(job["id"], attempt["id"], [artifact["id"]])
            def publish_media_asset(connection):
                changed = connection.execute(
                    "UPDATE media_assets SET status='READY' WHERE id=? AND status='PENDING' AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='RUNNING')",
                    (asset_id, job["id"]),
                ).rowcount
                if changed != 1:
                    raise MediaError("media_asset_publish_failed")
                state = connection.execute("SELECT desired_generation,active_generation FROM media_import_state WHERE project_id=?", (job["project_id"],)).fetchone()
                if state and state["desired_generation"] == snapshot["source_generation"] and snapshot["source_generation"] > state["active_generation"]:
                    connection.execute("UPDATE media_import_state SET active_generation=?,active_asset_id=? WHERE project_id=? AND desired_generation=?", (snapshot["source_generation"], asset_id, job["project_id"], snapshot["source_generation"]))
            result = self.jobs.complete_attempt(job["id"], attempt["id"], evidence, completion_hook=publish_media_asset)
            if result["status"] != "READY":
                with self.active_lock:
                    self.active_job_id = None
                return True
        except MediaError as exc:
            self.jobs.fail_attempt(job["id"], attempt["id"], exc.code, retryable=exc.code in {"ffprobe_timeout", "source_unavailable"})
        except Exception:
            try:
                self.jobs.fail_attempt(job["id"], attempt["id"], "media_import_failed", retryable=False)
            except Exception:
                pass
        with self.active_lock:
            self.active_job_id = None
        return True


class MediaImportWorker:
    def __init__(self, service: MediaImportService):
        self.service = service
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="autodub-media-import", daemon=True)
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
                worked = self.service.import_once()
            except Exception:
                worked = False
            if not worked:
                self.stop_event.wait(0.25)
