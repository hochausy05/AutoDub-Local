"""Small SQLite persistence layer for local Projects."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any


SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000


class IdempotencyConflict(Exception):
    pass


def default_data_root() -> Path:
    override = os.environ.get("AUTODUB_DATA_ROOT")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise RuntimeError("LOCALAPPDATA is not configured; set AUTODUB_DATA_ROOT to a local folder.")
        return Path(base) / "AutoDub-Local"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "share") / "AutoDub-Local"


def _reject_network_root(root: Path) -> None:
    raw = str(root)
    win = PureWindowsPath(raw)
    if raw.startswith("\\\\") or win.drive.startswith("\\\\"):
        raise ValueError("SQLite WAL requires a local data folder; UNC paths are not supported.")
    if os.name == "nt":
        drive = root.drive or Path.cwd().drive
        if drive:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
            if drive_type == 4:  # DRIVE_REMOTE, including mapped network drives
                raise ValueError("SQLite WAL requires a local data folder; network drives are not supported.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Database:
    def __init__(self, data_root: Path | str | None = None):
        raw_root = (Path(data_root) if data_root is not None else default_data_root()).expanduser()
        _reject_network_root(raw_root)
        self.data_root = raw_root.resolve()
        _reject_network_root(self.data_root)
        source_root = Path(__file__).resolve().parents[2]
        if self.data_root.resolve().is_relative_to(source_root):
            raise ValueError("AUTODUB_DATA_ROOT must be outside the application source tree.")
        self.path = self.data_root / "autodub.sqlite3"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def initialize(self, target_version: int = SCHEMA_VERSION) -> None:
        if not 1 <= target_version <= SCHEMA_VERSION:
            raise ValueError("Unsupported migration target version.")
        self.data_root.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("SQLite could not enable WAL for the local database.")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise RuntimeError(f"Database schema version {current} is newer than this application supports.")
                if current < 1:
                    statements = (
                        """CREATE TABLE projects (
                            id TEXT PRIMARY KEY NOT NULL,
                            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
                            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status = 'ACTIVE'),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            lock_version INTEGER NOT NULL DEFAULT 1 CHECK(lock_version >= 1),
                            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version >= 1)
                        )""",
                        """CREATE TABLE idempotency_records (
                            scope TEXT NOT NULL,
                            idempotency_key TEXT NOT NULL,
                            request_digest TEXT NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            response_json TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(scope, idempotency_key)
                        )""",
                        "CREATE INDEX projects_created_at_idx ON projects(created_at DESC, id DESC)",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (1, utc_now())
                    )
                    current = 1
                if current < 2 and target_version >= 2:
                    statements = (
                        """CREATE TABLE jobs (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 64),
                            input_snapshot TEXT NOT NULL CHECK(json_valid(input_snapshot)),
                            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint) = 64),
                            idempotency_key TEXT NOT NULL,
                            request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
                            status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','CANCEL_REQUESTED','CANCELLED','READY','FAILED','INTERRUPTED')),
                            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                            lock_version INTEGER NOT NULL DEFAULT 1 CHECK(lock_version >= 1),
                            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version >= 1),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(project_id, kind, idempotency_key),
                            CHECK((status IN ('CANCEL_REQUESTED','CANCELLED') AND cancel_requested=1) OR (status NOT IN ('CANCEL_REQUESTED','CANCELLED') AND cancel_requested=0))
                        )""",
                        """CREATE TABLE job_attempts (
                            id TEXT PRIMARY KEY NOT NULL,
                            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
                            attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
                            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version >= 1),
                            status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')),
                            started_at TEXT NOT NULL,
                            ended_at TEXT,
                            error_code TEXT CHECK(error_code IS NULL OR (length(error_code) BETWEEN 1 AND 80 AND substr(error_code,1,1) GLOB '[a-z]' AND error_code NOT GLOB '*[^a-z0-9_]*')),
                            retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
                            progress_json TEXT CHECK(progress_json IS NULL OR (length(progress_json) <= 4096 AND json_valid(progress_json))),
                            completion_evidence_json TEXT CHECK(completion_evidence_json IS NULL OR (length(completion_evidence_json) <= 1024 AND json_valid(completion_evidence_json))),
                            UNIQUE(job_id, attempt_no),
                            CHECK((status='RUNNING' AND ended_at IS NULL) OR (status<>'RUNNING' AND ended_at IS NOT NULL))
                        )""",
                        "CREATE UNIQUE INDEX job_attempts_one_active_idx ON job_attempts(job_id) WHERE status='RUNNING'",
                        "CREATE INDEX jobs_queue_idx ON jobs(status, created_at, id)",
                        """CREATE TRIGGER jobs_input_immutable BEFORE UPDATE OF id,project_id,kind,input_snapshot,input_fingerprint,idempotency_key,request_digest,created_at ON jobs
                            BEGIN SELECT RAISE(ABORT, 'job input snapshot is immutable'); END""",
                        """CREATE TRIGGER jobs_status_transition_guard BEFORE UPDATE OF status ON jobs
                            WHEN OLD.status <> NEW.status AND NOT (
                                (OLD.status='QUEUED' AND NEW.status IN ('RUNNING','CANCELLED')) OR
                                (OLD.status='RUNNING' AND NEW.status IN ('CANCEL_REQUESTED','READY','FAILED','INTERRUPTED')) OR
                                (OLD.status='CANCEL_REQUESTED' AND NEW.status='CANCELLED') OR
                                (OLD.status IN ('FAILED','INTERRUPTED') AND NEW.status='QUEUED')
                            ) BEGIN SELECT RAISE(ABORT, 'invalid job state transition'); END""",
                        """CREATE TRIGGER job_attempt_status_transition_guard BEFORE UPDATE OF status ON job_attempts
                            WHEN OLD.status <> NEW.status AND NOT (
                                OLD.status='RUNNING' AND NEW.status IN ('SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')
                            ) BEGIN SELECT RAISE(ABORT, 'invalid attempt state transition'); END""",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (2, utc_now())
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def create_project(self, name: str, key: str) -> tuple[dict[str, Any], bool]:
        digest = hashlib.sha256(json.dumps({"name": name}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_digest, response_json FROM idempotency_records WHERE scope='project:create' AND idempotency_key=?",
                (key,),
            ).fetchone()
            if prior:
                if prior["request_digest"] != digest:
                    raise IdempotencyConflict
                result = json.loads(prior["response_json"])
                connection.commit()
                return result, True
            project_id = str(uuid.uuid4())
            stamp = utc_now()
            connection.execute(
                "INSERT INTO projects(id,name,status,created_at,updated_at,lock_version,schema_version) VALUES(?,?, 'ACTIVE',?,?,1,1)",
                (project_id, name, stamp, stamp),
            )
            result = {"id": project_id, "lock_version": 1}
            connection.execute(
                "INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES('project:create',?,?,?,?,?)",
                (key, digest, project_id, json.dumps(result, separators=(",", ":")), stamp),
            )
            connection.commit()
            return result, False
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("id", "name", "status", "created_at", "updated_at", "lock_version", "schema_version")}

    def list_projects(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id,name,status,created_at,updated_at,lock_version,schema_version FROM projects ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id,name,status,created_at,updated_at,lock_version,schema_version FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
        return self._project(row) if row else None
