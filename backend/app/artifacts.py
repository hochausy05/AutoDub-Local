"""Verified, immutable local artifact publication with a durable DB manifest."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol

from app.database import Database, utc_now


_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_COMPLETION_SEAL = object()
_CHUNK_SIZE = 1024 * 1024
_MAX_COMPLETION_ARTIFACTS = 1000


class ArtifactError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ArtifactNotFound(ArtifactError):
    def __init__(self):
        super().__init__("artifact_not_found")


class ArtifactConflict(ArtifactError):
    pass


class ArtifactValidator(Protocol):
    version: str

    def validate(self, path: Path) -> Mapping[str, Any]: ...


class CompletionEvidence:
    """Sealed proof created only after READY artifacts were revalidated."""

    __slots__ = ("job_id", "attempt_id", "artifact_ids", "input_fingerprint", "_seal")

    def __init__(self, job_id: str, attempt_id: str, artifact_ids: tuple[str, ...], input_fingerprint: str, *, _seal: object):
        if _seal is not _COMPLETION_SEAL:
            raise TypeError("CompletionEvidence is issued by ArtifactPublicationService only.")
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.artifact_ids = artifact_ids
        self.input_fingerprint = input_fingerprint
        self._seal = _seal


class ArtifactPublicationService:
    """Internal service. Validators are registered by future pipeline adapters."""

    def __init__(
        self,
        database: Database,
        validators: Mapping[str, ArtifactValidator] | None = None,
        *,
        id_factory=uuid.uuid4,
        fault_hook=None,
    ):
        self.database = database
        self.data_root = database.data_root
        self.validators = dict(validators or {})
        self.id_factory = id_factory
        self.fault_hook = fault_hook

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ArtifactError(f"invalid_{field}") from exc
        if str(parsed) != value.lower():
            raise ArtifactError(f"invalid_{field}")
        return str(parsed)

    @staticmethod
    def _canonical(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ArtifactError("invalid_verification_metadata") from exc

    def _fault(self, phase: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(phase)

    def _validator(self, kind: str) -> ArtifactValidator:
        validator = self.validators.get(kind)
        if validator is None:
            raise ArtifactError("validator_unavailable")
        version = getattr(validator, "version", None)
        if not isinstance(version, str) or not 1 <= len(version) <= 80 or not re.fullmatch(r"[A-Za-z0-9_.-]+", version):
            raise ArtifactError("validator_version_invalid")
        return validator

    def _safe_metadata(self, value: Mapping[str, Any]) -> tuple[str, str]:
        if not isinstance(value, Mapping) or len(value) > 32:
            raise ArtifactError("validator_metadata_invalid")
        metadata: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _FIELD.fullmatch(key):
                raise ArtifactError("validator_metadata_invalid")
            if isinstance(item, str):
                if len(item) > 128:
                    raise ArtifactError("validator_metadata_invalid")
                metadata[key] = item
            elif item is None or type(item) is bool or type(item) is int:
                metadata[key] = item
            elif type(item) is float and math.isfinite(item):
                metadata[key] = item
            else:
                raise ArtifactError("validator_metadata_invalid")
        encoded = self._canonical(metadata)
        if len(encoded.encode("utf-8")) > 4096:
            raise ArtifactError("validator_metadata_invalid")
        return encoded, self._canonical(metadata)

    def _check_root(self) -> None:
        try:
            root_stat = self.data_root.lstat()
        except FileNotFoundError as exc:
            raise ArtifactError("managed_root_missing") from exc
        reparse = getattr(root_stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode) or reparse:
            raise ArtifactError("unsafe_managed_root")
        if self.data_root.resolve(strict=True) != self.data_root:
            raise ArtifactError("unsafe_managed_root")

    def _ensure_directory(self, relative: PurePosixPath) -> Path:
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} or "\\" in part for part in relative.parts):
            raise ArtifactError("unsafe_managed_path")
        self._check_root()
        current = self.data_root
        for component in relative.parts:
            current = current / component
            try:
                current.mkdir()
            except FileExistsError:
                pass
            try:
                current_stat = current.lstat()
            except OSError as exc:
                raise ArtifactError("managed_directory_unavailable") from exc
            reparse = getattr(current_stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if not stat.S_ISDIR(current_stat.st_mode) or stat.S_ISLNK(current_stat.st_mode) or reparse:
                raise ArtifactError("unsafe_managed_path")
            resolved = current.resolve(strict=True)
            if not resolved.is_relative_to(self.data_root):
                raise ArtifactError("unsafe_managed_path")
        return current

    def _path(self, relative: str, *, ensure_parent: bool = False) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in {".", ".."} or "\\" in part for part in pure.parts):
            raise ArtifactError("unsafe_managed_path")
        parent = PurePosixPath(*pure.parts[:-1])
        if ensure_parent:
            parent_path = self._ensure_directory(parent)
        else:
            self._check_root()
            parent_path = self.data_root.joinpath(*parent.parts)
            resolved_parent = parent_path.resolve(strict=True)
            if not resolved_parent.is_relative_to(self.data_root):
                raise ArtifactError("unsafe_managed_path")
            self._check_directory_components(parent)
        return parent_path / pure.parts[-1]

    def _check_directory_components(self, relative: PurePosixPath) -> None:
        current = self.data_root
        for component in relative.parts:
            current = current / component
            info = current.lstat()
            reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
                raise ArtifactError("unsafe_managed_path")

    @staticmethod
    def _chunks(source: Iterable[bytes] | BinaryIO | bytes):
        if isinstance(source, (bytes, bytearray, memoryview)):
            yield source
            return
        reader = getattr(source, "read", None)
        if callable(reader):
            while True:
                chunk = reader(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
            return
        yield from source

    @staticmethod
    def _write_stream(destination: Path, source: Iterable[bytes] | BinaryIO | bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for chunk in ArtifactPublicationService._chunks(source):
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ArtifactError("stream_chunk_invalid")
                view = memoryview(chunk)
                for offset in range(0, len(view), _CHUNK_SIZE):
                    stream.write(view[offset:offset + _CHUNK_SIZE])
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _validate_file(self, path: Path, validator: ArtifactValidator, cancelled=None) -> tuple[str, str]:
        try:
            if cancelled is not None and "cancelled" in inspect.signature(validator.validate).parameters:
                result = validator.validate(path, cancelled=cancelled)
            else:
                result = validator.validate(path)
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactError("validator_failed") from exc
        metadata_json, _ = self._safe_metadata(result)
        return metadata_json, validator.version

    def _assert_producer(self, project_id: str, job_id: str, attempt_id: str, fingerprint: str) -> None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """SELECT j.status AS job_status,j.input_fingerprint,ja.status AS attempt_status
                   FROM jobs AS j JOIN job_attempts AS ja ON ja.job_id=j.id
                   WHERE j.id=? AND j.project_id=? AND ja.id=?""",
                (job_id, project_id, attempt_id),
            ).fetchone()
        if row is None:
            raise ArtifactNotFound
        if row["job_status"] not in {"RUNNING", "CANCEL_REQUESTED"} or row["attempt_status"] != "RUNNING":
            raise ArtifactConflict("producer_not_running")
        if row["input_fingerprint"] != fingerprint:
            raise ArtifactConflict("input_fingerprint_mismatch")

    def publish(
        self,
        *,
        project_id: str,
        job_id: str,
        attempt_id: str,
        kind: str,
        input_fingerprint: str,
        fingerprint_version: int,
        content_fingerprint: str | None = None,
        validator_cancelled=None,
        source: Iterable[bytes] | BinaryIO | bytes,
    ) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project_id")
        job_id = self._uuid(job_id, "job_id")
        attempt_id = self._uuid(attempt_id, "attempt_id")
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ArtifactError("invalid_artifact_kind")
        if not isinstance(input_fingerprint, str) or not _FINGERPRINT.fullmatch(input_fingerprint):
            raise ArtifactError("invalid_input_fingerprint")
        content_fingerprint = content_fingerprint or input_fingerprint
        if not isinstance(content_fingerprint, str) or not _FINGERPRINT.fullmatch(content_fingerprint):
            raise ArtifactError("invalid_content_fingerprint")
        if type(fingerprint_version) is not int or fingerprint_version < 1:
            raise ArtifactError("invalid_fingerprint_version")
        validator = self._validator(kind)
        self._assert_producer(project_id, job_id, attempt_id, input_fingerprint)

        artifact_id = str(self.id_factory())
        artifact_id = self._uuid(artifact_id, "artifact_id")
        staging_relative = f"staging/artifacts/{artifact_id}.part"
        managed_relative = f"projects/{project_id}/artifacts/{kind}/{artifact_id}.artifact"
        staging_path = self._path(staging_relative, ensure_parent=True)
        managed_path = self._path(managed_relative, ensure_parent=True)
        stamp = utc_now()
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO artifacts(id,project_id,producer_job_id,producer_attempt_id,kind,input_fingerprint,content_fingerprint,
                           fingerprint_version,staging_relative_path,managed_relative_path,status,schema_version,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'STAGING',1,?)""",
                    (artifact_id, project_id, job_id, attempt_id, kind, input_fingerprint, content_fingerprint, fingerprint_version,
                     staging_relative, managed_relative, stamp),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._fault("after_manifest")

        verified = False
        try:
            self._write_stream(staging_path, source)
            self._fault("after_stage_flush")
            sha256, file_size = self._hash_file(staging_path)
            metadata_json, verification_version = self._validate_file(staging_path, validator, validator_cancelled)
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    changed = connection.execute(
                        """UPDATE artifacts SET status='VERIFIED',sha256=?,file_size=?,verification_metadata_json=?,
                               verification_version=?,verified_at=? WHERE id=? AND status='STAGING'""",
                        (sha256, file_size, metadata_json, verification_version, utc_now(), artifact_id),
                    ).rowcount
                    if changed != 1:
                        raise ArtifactConflict("artifact_manifest_state_changed")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            verified = True
            self._fault("after_verified")
            self._publish_no_replace(staging_path, managed_path)
            published_sha256, published_size = self._hash_file(managed_path)
            if published_sha256 != sha256 or published_size != file_size:
                raise ArtifactError("published_integrity_failed")
            self._fault("after_publish")
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    changed = connection.execute(
                        "UPDATE artifacts SET status='READY',published_at=? WHERE id=? AND status='VERIFIED'",
                        (utc_now(), artifact_id),
                    ).rowcount
                    if changed != 1:
                        raise ArtifactConflict("artifact_manifest_state_changed")
                    self._fault("before_ready_commit")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            self._fault("after_ready_commit")
            return self.get_artifact(artifact_id, project_id)
        except Exception as exc:
            if not verified:
                failure_code = exc.code if isinstance(exc, ArtifactError) and _ERROR_CODE.fullmatch(exc.code) else "publication_failed"
                self._mark_failed(artifact_id, failure_code)
            if isinstance(exc, ArtifactError):
                raise
            if isinstance(exc, FileExistsError):
                raise ArtifactConflict("destination_exists") from exc
            if isinstance(exc, OSError) and not verified:
                raise ArtifactError("staging_write_failed") from exc
            raise ArtifactError("publication_commit_failed" if verified else "publication_failed") from exc

    def _publish_no_replace(self, source: Path, destination: Path) -> None:
        if destination.exists():
            raise ArtifactConflict("destination_exists")
        source_device = source.parent.stat().st_dev
        destination_device = destination.parent.stat().st_dev
        if source_device != destination_device:
            raise ArtifactError("cross_volume_not_supported")
        if os.name == "nt":
            # Windows os.rename fails when destination exists; unlike os.replace it never replaces it.
            os.rename(source, destination)
        else:
            # Atomic link creation is exclusive; fail closed if hard links are unavailable.
            os.link(source, destination, follow_symlinks=False)
            os.unlink(source)

    def _mark_failed(self, artifact_id: str, error_code: str) -> None:
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "UPDATE artifacts SET status='FAILED',error_code=? WHERE id=? AND status='STAGING'",
                        (error_code, artifact_id),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except sqlite3.Error:
            # The durable STAGING manifest remains for CORE-01D if SQLite is unavailable.
            return

    def get_artifact(self, artifact_id: str, project_id: str | None = None) -> dict[str, Any]:
        artifact_id = self._uuid(artifact_id, "artifact_id")
        expected_project = self._uuid(project_id, "project_id") if project_id is not None else None
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """SELECT id,project_id,producer_job_id,producer_attempt_id,kind,input_fingerprint,content_fingerprint,fingerprint_version,
                          staging_relative_path,managed_relative_path,sha256,file_size,verification_metadata_json,
                          verification_version,status,error_code,schema_version,created_at,verified_at,published_at
                   FROM artifacts WHERE id=?""",
                (artifact_id,),
            ).fetchone()
        if row is None or (expected_project is not None and row["project_id"] != expected_project):
            raise ArtifactNotFound
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "producer_job_id": row["producer_job_id"],
            "producer_attempt_id": row["producer_attempt_id"],
            "kind": row["kind"],
            "input_fingerprint": row["input_fingerprint"],
            "content_fingerprint": row["content_fingerprint"],
            "fingerprint_version": row["fingerprint_version"],
            "staging_relative_path": row["staging_relative_path"],
            "managed_relative_path": row["managed_relative_path"],
            "sha256": row["sha256"],
            "file_size": row["file_size"],
            "verification_metadata": json.loads(row["verification_metadata_json"]) if row["verification_metadata_json"] else None,
            "verification_version": row["verification_version"],
            "status": row["status"],
            "error_code": row["error_code"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "verified_at": row["verified_at"],
            "published_at": row["published_at"],
        }

    def _mark_integrity_failure(self, artifact_id: str, expected_status: str, status: str, error_code: str) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    "UPDATE artifacts SET status=?,error_code=? WHERE id=? AND status=?",
                    (status, error_code, artifact_id, expected_status),
                ).rowcount
                if changed:
                    connection.execute(
                        """UPDATE stage_pointers SET active_artifact_id=NULL,pointer_generation=pointer_generation+1,updated_at=?
                           WHERE active_artifact_id=? AND project_id=(SELECT project_id FROM artifacts WHERE id=?)""",
                        (utc_now(), artifact_id, artifact_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def verify_ready(self, artifact_id: str, project_id: str | None = None) -> dict[str, Any]:
        artifact = self.get_artifact(artifact_id, project_id)
        if artifact["status"] != "READY":
            raise ArtifactConflict("artifact_not_ready")
        validator = self._validator(artifact["kind"])
        if validator.version != artifact["verification_version"]:
            raise ArtifactConflict("verification_version_unsupported")
        try:
            path = self._path(artifact["managed_relative_path"])
            info = path.lstat()
            reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
                raise ArtifactError("unsafe_managed_file")
            actual_sha256, actual_size = self._hash_file(path)
        except FileNotFoundError as exc:
            self._mark_integrity_failure(artifact["id"], "READY", "MISSING", "artifact_missing")
            raise ArtifactConflict("artifact_missing") from exc
        except ArtifactError as exc:
            self._mark_integrity_failure(artifact["id"], "READY", "CORRUPT", "artifact_path_invalid")
            raise ArtifactConflict("artifact_corrupt") from exc
        if actual_size != artifact["file_size"] or actual_sha256 != artifact["sha256"]:
            self._mark_integrity_failure(artifact["id"], "READY", "CORRUPT", "artifact_integrity_failed")
            raise ArtifactConflict("artifact_corrupt")
        try:
            metadata_json, version = self._validate_file(path, validator)
        except ArtifactError as exc:
            self._mark_integrity_failure(artifact["id"], "READY", "CORRUPT", "artifact_validation_failed")
            raise ArtifactConflict("artifact_corrupt") from exc
        if version != artifact["verification_version"] or metadata_json != self._canonical(artifact["verification_metadata"]):
            self._mark_integrity_failure(artifact["id"], "READY", "CORRUPT", "artifact_verification_changed")
            raise ArtifactConflict("artifact_corrupt")
        return artifact

    def completion_evidence(self, job_id: str, attempt_id: str, artifact_ids: Iterable[str]) -> CompletionEvidence:
        job_id = self._uuid(job_id, "job_id")
        attempt_id = self._uuid(attempt_id, "attempt_id")
        ids = tuple(self._uuid(item, "artifact_id") for item in artifact_ids)
        if not ids or len(ids) > _MAX_COMPLETION_ARTIFACTS or len(set(ids)) != len(ids):
            raise ArtifactError("required_artifacts_invalid")
        with closing(self.database.connect()) as connection:
            producer = connection.execute(
                """SELECT j.project_id,j.input_fingerprint,j.status,ja.status AS attempt_status
                   FROM jobs AS j JOIN job_attempts AS ja ON ja.job_id=j.id
                   WHERE j.id=? AND ja.id=?""",
                (job_id, attempt_id),
            ).fetchone()
        if producer is None:
            raise ArtifactNotFound
        if producer["status"] not in {"RUNNING", "CANCEL_REQUESTED"} or producer["attempt_status"] != "RUNNING":
            raise ArtifactConflict("producer_not_running")
        for artifact_id in ids:
            artifact = self.verify_ready(artifact_id, producer["project_id"])
            if artifact["producer_job_id"] != job_id or artifact["producer_attempt_id"] != attempt_id:
                raise ArtifactConflict("artifact_provenance_mismatch")
            if artifact["input_fingerprint"] != producer["input_fingerprint"]:
                raise ArtifactConflict("artifact_fingerprint_mismatch")
        return CompletionEvidence(job_id, attempt_id, ids, producer["input_fingerprint"], _seal=_COMPLETION_SEAL)

    def validate_completion_evidence(self, evidence: CompletionEvidence, job_id: str, attempt_id: str) -> str:
        if not isinstance(evidence, CompletionEvidence) or evidence._seal is not _COMPLETION_SEAL:
            raise ArtifactConflict("completion_unverified")
        if evidence.job_id != job_id or evidence.attempt_id != attempt_id or not evidence.artifact_ids:
            raise ArtifactConflict("completion_provenance_mismatch")
        for artifact_id in evidence.artifact_ids:
            artifact = self.verify_ready(artifact_id)
            if artifact["producer_job_id"] != job_id or artifact["producer_attempt_id"] != attempt_id:
                raise ArtifactConflict("completion_provenance_mismatch")
            if artifact["input_fingerprint"] != evidence.input_fingerprint:
                raise ArtifactConflict("completion_fingerprint_mismatch")
        summary = {
            "artifact_count": len(evidence.artifact_ids),
            "artifact_ids_digest": hashlib.sha256(self._canonical(sorted(evidence.artifact_ids)).encode("utf-8")).hexdigest(),
            "input_fingerprint": evidence.input_fingerprint,
            "verification_code": "artifacts_verified",
        }
        return self._canonical(summary)

    def _record_recovery_state(self, artifact_id: str, expected: str, status: str, **fields) -> bool:
        allowed = {"sha256", "file_size", "verification_metadata_json", "verification_version", "verified_at", "published_at", "error_code"}
        if set(fields) - allowed:
            raise ArtifactError("invalid_recovery_update")
        assignments = [f"{name}=?" for name in fields]
        values = list(fields.values())
        assignments.insert(0, "status=?")
        values.insert(0, status)
        values.extend([artifact_id, expected])
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    f"UPDATE artifacts SET {','.join(assignments)} WHERE id=? AND status=?", values
                ).rowcount
                connection.commit()
                return changed == 1
            except BaseException:
                connection.rollback()
                raise

    def reconcile_publications(self) -> dict[str, Any]:
        """Reconcile manifest-backed candidates without deleting or overwriting files."""
        with closing(self.database.connect()) as connection:
            rows = connection.execute("SELECT * FROM artifacts ORDER BY created_at,id").fetchall()
        report: dict[str, Any] = {"ready_verified": 0, "recovered": 0, "missing": 0, "corrupt": 0,
                                  "startup_blocked": False, "findings": []}
        known_paths: set[str] = set()
        for source_row in rows:
            artifact = dict(source_row)
            staging_relative = artifact["staging_relative_path"]
            final_relative = artifact["managed_relative_path"]
            known_paths.update((staging_relative, final_relative))
            status = artifact["status"]
            if status == "READY":
                try:
                    self.verify_ready(artifact["id"])
                    report["ready_verified"] += 1
                except ArtifactConflict:
                    refreshed = self.get_artifact(artifact["id"])
                    if refreshed["status"] == "MISSING":
                        report["missing"] += 1
                    elif refreshed["status"] == "CORRUPT":
                        report["corrupt"] += 1
                    else:
                        report["startup_blocked"] = True
                        report["findings"].append((final_relative, "ready_verification_blocked"))
                except ArtifactError as exc:
                    report["startup_blocked"] = True
                    report["findings"].append((final_relative, "ready_verification_blocked"))
                continue
            if status == "STAGING":
                try:
                    staging_path = self._path(staging_relative)
                    info = staging_path.lstat()
                    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
                        raise ArtifactError("unsafe_staging_file")
                    validator = self._validator(artifact["kind"])
                    sha256, file_size = self._hash_file(staging_path)
                    metadata_json, version = self._validate_file(staging_path, validator)
                    changed = self._record_recovery_state(
                        artifact["id"], "STAGING", "VERIFIED", sha256=sha256, file_size=file_size,
                        verification_metadata_json=metadata_json, verification_version=version,
                        verified_at=utc_now(), error_code=None,
                    )
                    if changed:
                        status = "VERIFIED"
                        artifact.update(status=status, sha256=sha256, file_size=file_size,
                                        verification_metadata_json=metadata_json, verification_version=version)
                    else:
                        continue
                except FileNotFoundError:
                    report["findings"].append((staging_relative, "staging_candidate_missing"))
                    continue
                except ArtifactError as exc:
                    report["findings"].append((staging_relative, exc.code))
                    continue
                except OSError:
                    report["findings"].append((staging_relative, "staging_candidate_unavailable"))
                    continue
            if status != "VERIFIED":
                continue
            try:
                try:
                    staging_path = self._path(staging_relative)
                except FileNotFoundError:
                    staging_path = None
                final_path = self._path(final_relative, ensure_parent=True)
                staging_exists = staging_path is not None and staging_path.exists()
                final_exists = final_path.exists()
                if staging_exists and final_exists:
                    report["findings"].append((final_relative, "duplicate_publication_candidates"))
                    continue
                if not staging_exists and not final_exists:
                    self._mark_integrity_failure(artifact["id"], "VERIFIED", "MISSING", "artifact_missing")
                    report["missing"] += 1
                    continue
                candidate = final_path if final_exists else staging_path
                if candidate is None:
                    self._mark_integrity_failure(artifact["id"], "VERIFIED", "MISSING", "artifact_missing")
                    report["missing"] += 1
                    continue
                info = candidate.lstat()
                reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or reparse:
                    raise ArtifactError("unsafe_candidate_file")
                sha256, file_size = self._hash_file(candidate)
                validator = self._validator(artifact["kind"])
                metadata_json, version = self._validate_file(candidate, validator)
                if (sha256 != artifact["sha256"] or file_size != artifact["file_size"] or
                    metadata_json != artifact["verification_metadata_json"] or version != artifact["verification_version"]):
                    self._mark_integrity_failure(artifact["id"], "VERIFIED", "CORRUPT", "artifact_integrity_failed")
                    report["corrupt"] += 1
                    continue
                if not final_exists:
                    assert staging_path is not None
                    self._publish_no_replace(staging_path, final_path)
                    published_hash, published_size = self._hash_file(final_path)
                    published_metadata, published_version = self._validate_file(final_path, validator)
                    if (published_hash != sha256 or published_size != file_size or published_metadata != metadata_json or
                        published_version != version):
                        self._mark_integrity_failure(artifact["id"], "VERIFIED", "CORRUPT", "artifact_integrity_failed")
                        report["corrupt"] += 1
                        continue
                changed = self._record_recovery_state(artifact["id"], "VERIFIED", "READY", published_at=utc_now(), error_code=None)
                if changed:
                    report["recovered"] += 1
            except FileNotFoundError:
                report["findings"].append((final_relative, "candidate_disappeared"))
            except ArtifactError as exc:
                report["findings"].append((final_relative, exc.code))
            except OSError:
                report["findings"].append((final_relative, "candidate_recovery_unavailable"))
        return report
