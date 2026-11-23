"""Durable single-machine Job and Attempt state transitions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterator

from app.database import Database, IdempotencyConflict, utc_now
from app.artifacts import ArtifactError, ArtifactPublicationService, CompletionEvidence


_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PHASE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_STAGES = frozenset({"asr", "translation", "tts", "sync", "render"})
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_ATTEMPTS = 3


class ActivationStage(StrEnum):
    ASR = "asr"
    TRANSLATION = "translation"
    TTS = "tts"
    SYNC = "sync"
    RENDER = "render"


class JobNotFound(Exception):
    pass


class JobInputError(Exception):
    pass


class JobConflict(Exception):
    def __init__(self, code: str = "invalid_job_transition"):
        self.code = code
        super().__init__(code)


class JobService:
    """Internal service; there is intentionally no public Job creation endpoint."""

    def __init__(self, database: Database, artifact_service: ArtifactPublicationService):
        self.database = database
        self.artifact_service = artifact_service
        self._recovery_complete = False

    def _allow_claims_after_recovery(self) -> None:
        self._recovery_complete = True

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise JobInputError(f"invalid_{field}") from exc
        if str(parsed) != value.lower():
            raise JobInputError(f"invalid_{field}")
        return str(parsed)

    @staticmethod
    def _canonical(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise JobInputError("invalid_json_snapshot") from exc

    @staticmethod
    def validate_idempotency_key(key: str) -> str:
        if not 16 <= len(key) <= 255 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise JobInputError("invalid_idempotency_key")
        return key

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(JobService._canonical(value).encode("utf-8")).hexdigest()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self.database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _progress_value(raw: str | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        return json.loads(raw)

    def _job_detail(self, connection: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT j.id,j.project_id,j.kind,j.input_fingerprint,j.status,j.cancel_requested,
                      j.lock_version,j.schema_version,j.created_at,j.updated_at,j.activation_stage,
                      j.expected_input_generation,j.expected_pointer_generation
               FROM jobs AS j JOIN projects AS p ON p.id=j.project_id WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound
        attempts = connection.execute(
            """SELECT id,attempt_no,schema_version,status,started_at,ended_at,error_code,retryable,progress_json,completion_evidence_json,activation_outcome
               FROM job_attempts WHERE job_id=? ORDER BY attempt_no""",
            (job_id,),
        ).fetchall()
        values = [
            {
                "id": attempt["id"],
                "attempt_no": attempt["attempt_no"],
                "schema_version": attempt["schema_version"],
                "status": attempt["status"],
                "started_at": attempt["started_at"],
                "ended_at": attempt["ended_at"],
                "error_code": attempt["error_code"],
                "retryable": bool(attempt["retryable"]),
                "progress": self._progress_value(attempt["progress_json"]),
                "activation_outcome": attempt["activation_outcome"],
            }
            for attempt in attempts
        ]
        latest = values[-1] if values else None
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "input_fingerprint": row["input_fingerprint"],
            "status": row["status"],
            "cancel_requested": bool(row["cancel_requested"]),
            "lock_version": row["lock_version"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "attempt": latest,
            "attempts": values,
            "progress": latest["progress"] if latest else None,
            "error_code": latest["error_code"] if latest else None,
            "activation_stage": row["activation_stage"],
            "expected_input_generation": row["expected_input_generation"],
            "expected_pointer_generation": row["expected_pointer_generation"],
        }

    def set_stage_input(self, project_id: str, stage: str, input_snapshot: dict[str, Any]) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project_id")
        if not isinstance(stage, str) or not _STAGE.fullmatch(stage) or stage not in _STAGES:
            raise JobInputError("invalid_activation_stage")
        if not isinstance(input_snapshot, dict) or type(input_snapshot.get("schema_version")) is not int or input_snapshot["schema_version"] < 1:
            raise JobInputError("invalid_input_snapshot")
        snapshot_json = self._canonical(input_snapshot)
        if len(snapshot_json.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise JobInputError("input_snapshot_too_large")
        fingerprint = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise JobNotFound
            row = connection.execute(
                "SELECT input_fingerprint,input_generation,pointer_generation,active_artifact_id FROM stage_pointers WHERE project_id=? AND stage=?",
                (project_id, stage),
            ).fetchone()
            stamp = utc_now()
            if row is None:
                connection.execute(
                    "INSERT INTO stage_pointers(project_id,stage,input_fingerprint,input_generation,pointer_generation,updated_at) VALUES(?,?,?,1,0,?)",
                    (project_id, stage, fingerprint, stamp),
                )
                return {"stage": stage, "input_fingerprint": fingerprint, "input_generation": 1, "pointer_generation": 0, "active_artifact_id": None}
            generation = row["input_generation"] + int(row["input_fingerprint"] != fingerprint)
            if generation != row["input_generation"]:
                connection.execute(
                    "UPDATE stage_pointers SET input_fingerprint=?,input_generation=?,updated_at=? WHERE project_id=? AND stage=?",
                    (fingerprint, generation, stamp, project_id, stage),
                )
            return {"stage": stage, "input_fingerprint": fingerprint, "input_generation": generation,
                    "pointer_generation": row["pointer_generation"], "active_artifact_id": row["active_artifact_id"]}

    def create_job(
        self, project_id: str, kind: str, input_snapshot: dict[str, Any], idempotency_key: str,
        *, activation_stage: ActivationStage | str | None = None,
    ) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project_id")
        self.validate_idempotency_key(idempotency_key)
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise JobInputError("invalid_job_kind")
        if not isinstance(input_snapshot, dict) or type(input_snapshot.get("schema_version")) is not int or input_snapshot["schema_version"] < 1:
            raise JobInputError("invalid_input_snapshot")
        if activation_stage is not None:
            if isinstance(activation_stage, ActivationStage):
                activation_stage = activation_stage.value
            if not isinstance(activation_stage, str) or not _STAGE.fullmatch(activation_stage) or activation_stage not in _STAGES:
                raise JobInputError("invalid_activation_stage")
        snapshot_json = self._canonical(input_snapshot)
        if len(snapshot_json.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise JobInputError("input_snapshot_too_large")
        request_digest = self._digest({"project_id": project_id, "kind": kind, "input_snapshot": input_snapshot, "activation_stage": activation_stage})
        input_fingerprint = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        scope = f"job:create:{project_id}:{kind}"
        with self._write() as connection:
            project = connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise JobNotFound
            previous = connection.execute(
                "SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise IdempotencyConflict
                prior_job_id = json.loads(previous["response_json"])["job_id"]
                return self._job_detail(connection, prior_job_id)

            expected_input_generation = None
            expected_pointer_generation = None
            if activation_stage is not None:
                stage = connection.execute(
                    "SELECT input_fingerprint,input_generation,pointer_generation FROM stage_pointers WHERE project_id=? AND stage=?",
                    (project_id, activation_stage),
                ).fetchone()
                if stage is None or stage["input_fingerprint"] != input_fingerprint:
                    raise JobConflict("stage_input_not_current")
                expected_input_generation = stage["input_generation"]
                expected_pointer_generation = stage["pointer_generation"]

            job_id = str(uuid.uuid4())
            stamp = utc_now()
            connection.execute(
                """INSERT INTO jobs(id,project_id,kind,input_snapshot,input_fingerprint,idempotency_key,request_digest,
                                     status,cancel_requested,lock_version,created_at,updated_at,activation_stage,
                                     expected_input_generation,expected_pointer_generation)
                   VALUES(?,?,?,?,?,?,?,'QUEUED',0,1,?,?,?,?,?)""",
                (job_id, project_id, kind, snapshot_json, input_fingerprint, idempotency_key, request_digest,
                 stamp, stamp, activation_stage, expected_input_generation, expected_pointer_generation),
            )
            connection.execute(
                """INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (scope, idempotency_key, request_digest, project_id, json.dumps({"job_id": job_id}), stamp),
            )
            return self._job_detail(connection, job_id)

    def get_job(self, job_id: str, project_id: str | None = None) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        expected_project = self._uuid(project_id, "project_id") if project_id is not None else None
        with closing(self.database.connect()) as connection:
            detail = self._job_detail(connection, job_id)
        if expected_project is not None and detail["project_id"] != expected_project:
            raise JobNotFound
        return detail

    def claim_next_job(self, kind: str | None = None) -> dict[str, Any] | None:
        if not self._recovery_complete:
            raise JobConflict("startup_recovery_required")
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE status IN ('RUNNING','CANCEL_REQUESTED') LIMIT 1").fetchone():
                return None
            if kind is None:
                row = connection.execute("SELECT id FROM jobs WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1").fetchone()
            else:
                row = connection.execute("SELECT id FROM jobs WHERE status='QUEUED' AND kind=? ORDER BY created_at,id LIMIT 1", (kind,)).fetchone()
            if row is None:
                return None
            job_id = row["id"]
            stamp = utc_now()
            attempt_no = connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM job_attempts WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            changed = connection.execute(
                "UPDATE jobs SET status='RUNNING',lock_version=lock_version+1,updated_at=? WHERE id=? AND status='QUEUED'",
                (stamp, job_id),
            ).rowcount
            if changed != 1:
                raise JobConflict("job_already_claimed")
            connection.execute(
                "INSERT INTO job_attempts(id,job_id,attempt_no,status,started_at) VALUES(?,?,?,'RUNNING',?)",
                (str(uuid.uuid4()), job_id, attempt_no, stamp),
            )
            return self._job_detail(connection, job_id)

    def request_cancel(self, job_id: str, idempotency_key: str) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        self.validate_idempotency_key(idempotency_key)
        scope = f"job:cancel:{job_id}"
        digest = self._digest({"job_id": job_id, "action": "cancel"})
        with self._write() as connection:
            job = connection.execute("SELECT project_id,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise JobNotFound
            previous = connection.execute(
                "SELECT request_digest FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if previous and previous["request_digest"] != digest:
                raise IdempotencyConflict
            if not previous:
                stamp = utc_now()
                if job["status"] == "QUEUED":
                    connection.execute(
                        "UPDATE jobs SET status='CANCELLED',cancel_requested=1,lock_version=lock_version+1,updated_at=? WHERE id=? AND status='QUEUED'",
                        (stamp, job_id),
                    )
                elif job["status"] == "RUNNING":
                    connection.execute(
                        "UPDATE jobs SET status='CANCEL_REQUESTED',cancel_requested=1,lock_version=lock_version+1,updated_at=? WHERE id=? AND status='RUNNING'",
                        (stamp, job_id),
                    )
                elif job["status"] not in {"CANCEL_REQUESTED", "CANCELLED", "READY"}:
                    raise JobConflict("job_not_cancellable")
                connection.execute(
                    """INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (scope, idempotency_key, digest, job["project_id"], json.dumps({"job_id": job_id}), stamp),
                )
            return self._job_detail(connection, job_id)

    def retry_job(self, job_id: str, expected_status: str, idempotency_key: str) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        self.validate_idempotency_key(idempotency_key)
        if expected_status not in {"FAILED", "INTERRUPTED"}:
            raise JobInputError("invalid_expected_status")
        scope = f"job:retry:{job_id}"
        digest = self._digest({"job_id": job_id, "action": "retry", "expected_status": expected_status})
        with self._write() as connection:
            job = connection.execute(
                "SELECT project_id,status,activation_stage,expected_input_generation,expected_pointer_generation,input_fingerprint FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise JobNotFound
            previous = connection.execute(
                "SELECT request_digest FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_digest"] != digest:
                    raise IdempotencyConflict
                return self._job_detail(connection, job_id)
            if job["status"] != expected_status:
                raise JobConflict("job_status_conflict")
            latest_attempt = connection.execute(
                "SELECT retryable FROM job_attempts WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1", (job_id,)
            ).fetchone()
            if latest_attempt is None or not latest_attempt["retryable"]:
                raise JobConflict("job_not_retryable")
            attempt_count = connection.execute("SELECT COUNT(*) FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()[0]
            if attempt_count >= MAX_ATTEMPTS:
                raise JobConflict("retry_limit_reached")
            if job["activation_stage"] is not None:
                pointer = connection.execute(
                    "SELECT input_fingerprint,input_generation,pointer_generation FROM stage_pointers WHERE project_id=? AND stage=?",
                    (job["project_id"], job["activation_stage"]),
                ).fetchone()
                if (pointer is None or pointer["input_fingerprint"] != job["input_fingerprint"] or
                    pointer["input_generation"] != job["expected_input_generation"] or
                    pointer["pointer_generation"] != job["expected_pointer_generation"]):
                    raise JobConflict("job_stale")
            stamp = utc_now()
            connection.execute(
                "UPDATE jobs SET status='QUEUED',cancel_requested=0,lock_version=lock_version+1,updated_at=? WHERE id=? AND status=?",
                (stamp, job_id, expected_status),
            )
            connection.execute(
                """INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (scope, idempotency_key, digest, job["project_id"], json.dumps({"job_id": job_id}), stamp),
            )
            return self._job_detail(connection, job_id)

    def update_attempt_progress(self, job_id: str, attempt_id: str, progress: dict[str, Any]) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        attempt_id = self._uuid(attempt_id, "attempt_id")
        if not isinstance(progress, dict) or set(progress) - {"phase", "completed_units", "total_units", "fraction"}:
            raise JobInputError("invalid_progress")
        phase = progress.get("phase")
        if phase is not None and (not isinstance(phase, str) or not _PHASE.fullmatch(phase)):
            raise JobInputError("invalid_progress")
        completed = progress.get("completed_units")
        total = progress.get("total_units")
        if (completed is None) != (total is None):
            raise JobInputError("invalid_progress")
        if completed is not None and (type(completed) is not int or type(total) is not int or completed < 0 or total < completed):
            raise JobInputError("invalid_progress")
        fraction = progress.get("fraction")
        if fraction is not None and (isinstance(fraction, bool) or not isinstance(fraction, (float, int)) or not math.isfinite(fraction) or not 0 <= fraction <= 1):
            raise JobInputError("invalid_progress")
        value = self._canonical(progress)
        if len(value.encode("utf-8")) > 4096:
            raise JobInputError("invalid_progress")
        with self._write() as connection:
            job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempt = connection.execute("SELECT status FROM job_attempts WHERE id=? AND job_id=?", (attempt_id, job_id)).fetchone()
            if job is None or attempt is None:
                raise JobNotFound
            if job["status"] != "RUNNING" or attempt["status"] != "RUNNING":
                raise JobConflict("attempt_not_running")
            connection.execute("UPDATE job_attempts SET progress_json=? WHERE id=?", (value, attempt_id))
            connection.execute("UPDATE jobs SET updated_at=? WHERE id=?", (utc_now(), job_id))
            return self._job_detail(connection, job_id)

    def complete_attempt(
        self, job_id: str, attempt_id: str, evidence: CompletionEvidence,
        *, completion_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        attempt_id = self._uuid(attempt_id, "attempt_id")
        with closing(self.database.connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM job_attempts WHERE id=? AND job_id=?", (attempt_id, job_id)
            ).fetchone()
        if exists is None:
            raise JobNotFound
        try:
            evidence_json = self.artifact_service.validate_completion_evidence(evidence, job_id, attempt_id)
        except ArtifactError as exc:
            raise JobConflict(exc.code) from exc
        with self._write() as connection:
            job = connection.execute(
                "SELECT status,project_id,input_fingerprint,activation_stage,expected_input_generation,expected_pointer_generation FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            attempt = connection.execute("SELECT status FROM job_attempts WHERE id=? AND job_id=?", (attempt_id, job_id)).fetchone()
            if job is None or attempt is None:
                raise JobNotFound
            if attempt["status"] != "RUNNING":
                raise JobConflict("attempt_not_running")
            stamp = utc_now()
            if job["status"] == "CANCEL_REQUESTED":
                connection.execute(
                    "UPDATE job_attempts SET status='CANCELLED',ended_at=?,completion_evidence_json=?,activation_outcome=? WHERE id=?",
                    (stamp, evidence_json, "CANCELLED" if job["activation_stage"] is not None else "NOT_APPLICABLE", attempt_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='CANCELLED',lock_version=lock_version+1,updated_at=? WHERE id=? AND status='CANCEL_REQUESTED'",
                    (stamp, job_id),
                )
            elif job["status"] == "RUNNING":
                activation_outcome = "NOT_APPLICABLE"
                if job["activation_stage"] is not None:
                    if len(evidence.artifact_ids) != 1:
                        raise JobConflict("stage_activation_requires_one_artifact")
                    artifact_id = evidence.artifact_ids[0]
                    pointer = connection.execute(
                        "SELECT input_fingerprint,input_generation,pointer_generation,active_artifact_id FROM stage_pointers WHERE project_id=? AND stage=?",
                        (job["project_id"], job["activation_stage"]),
                    ).fetchone()
                    snapshot_matches = (
                        pointer is not None and pointer["input_fingerprint"] == job["input_fingerprint"] and
                        pointer["input_generation"] == job["expected_input_generation"] and
                        pointer["pointer_generation"] == job["expected_pointer_generation"]
                    )
                    if snapshot_matches:
                        if pointer["active_artifact_id"] == artifact_id:
                            activation_outcome = "ACTIVE"
                        else:
                            changed = connection.execute(
                                """UPDATE stage_pointers SET active_artifact_id=?,pointer_generation=pointer_generation+1,updated_at=?
                                   WHERE project_id=? AND stage=? AND input_fingerprint=? AND input_generation=? AND pointer_generation=?
                                     AND EXISTS(SELECT 1 FROM artifacts a WHERE a.id=? AND a.project_id=stage_pointers.project_id
                                       AND a.status='READY' AND a.input_fingerprint=? AND a.producer_job_id=? AND a.producer_attempt_id=?)""",
                                (artifact_id, stamp, job["project_id"], job["activation_stage"], job["input_fingerprint"],
                                 job["expected_input_generation"], job["expected_pointer_generation"], artifact_id,
                                 job["input_fingerprint"], job_id, attempt_id),
                            ).rowcount
                            activation_outcome = "ACTIVE" if changed == 1 else "STALE"
                    else:
                        activation_outcome = "STALE"
                if completion_hook is not None:
                    completion_hook(connection)
                connection.execute(
                    "UPDATE job_attempts SET status='SUCCEEDED',ended_at=?,completion_evidence_json=?,activation_outcome=? WHERE id=?",
                    (stamp, evidence_json, activation_outcome, attempt_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='READY',lock_version=lock_version+1,updated_at=? WHERE id=? AND status='RUNNING'",
                    (stamp, job_id),
                )
            else:
                raise JobConflict("invalid_job_transition")
            return self._job_detail(connection, job_id)

    def fail_attempt(self, job_id: str, attempt_id: str, error_code: str, *, retryable: bool = False) -> dict[str, Any]:
        job_id = self._uuid(job_id, "job_id")
        attempt_id = self._uuid(attempt_id, "attempt_id")
        if type(retryable) is not bool:
            raise JobInputError("invalid_retryable")
        safe_code = error_code if isinstance(error_code, str) and _CODE.fullmatch(error_code) else "worker_failed"
        with self._write() as connection:
            job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempt = connection.execute("SELECT status FROM job_attempts WHERE id=? AND job_id=?", (attempt_id, job_id)).fetchone()
            if job is None or attempt is None:
                raise JobNotFound
            if attempt["status"] != "RUNNING" or job["status"] not in {"RUNNING", "CANCEL_REQUESTED"}:
                raise JobConflict("attempt_not_running")
            stamp = utc_now()
            if job["status"] == "CANCEL_REQUESTED":
                connection.execute(
                    "UPDATE job_attempts SET status='CANCELLED',ended_at=?,error_code=? WHERE id=?",
                    (stamp, safe_code, attempt_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='CANCELLED',lock_version=lock_version+1,updated_at=? WHERE id=? AND status='CANCEL_REQUESTED'",
                    (stamp, job_id),
                )
            else:
                connection.execute(
                    "UPDATE job_attempts SET status='FAILED',ended_at=?,error_code=?,retryable=? WHERE id=?",
                    (stamp, safe_code, int(retryable), attempt_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='FAILED',lock_version=lock_version+1,updated_at=? WHERE id=? AND status='RUNNING'",
                    (stamp, job_id),
                )
            return self._job_detail(connection, job_id)
