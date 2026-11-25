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


SCHEMA_VERSION = 25
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
                    current = 2
                if current < 3 and target_version >= 3:
                    statements = (
                        "CREATE UNIQUE INDEX jobs_project_id_id_unique_idx ON jobs(project_id,id)",
                        "CREATE UNIQUE INDEX job_attempts_job_id_id_unique_idx ON job_attempts(job_id,id)",
                        """CREATE TABLE artifacts (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            producer_job_id TEXT NOT NULL,
                            producer_attempt_id TEXT NOT NULL,
                            kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 64),
                            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
                            fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version >= 1),
                            staging_relative_path TEXT NOT NULL UNIQUE,
                            managed_relative_path TEXT NOT NULL UNIQUE,
                            sha256 TEXT CHECK(sha256 IS NULL OR (length(sha256)=64 AND sha256 NOT GLOB '*[^a-f0-9]*')),
                            file_size INTEGER CHECK(file_size IS NULL OR file_size >= 0),
                            verification_metadata_json TEXT CHECK(verification_metadata_json IS NULL OR (length(verification_metadata_json)<=4096 AND json_valid(verification_metadata_json))),
                            verification_version TEXT,
                            status TEXT NOT NULL CHECK(status IN ('STAGING','VERIFIED','READY','FAILED','MISSING','CORRUPT')),
                            error_code TEXT CHECK(error_code IS NULL OR (length(error_code) BETWEEN 1 AND 80 AND substr(error_code,1,1) GLOB '[a-z]' AND error_code NOT GLOB '*[^a-z0-9_]*')),
                            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version >= 1),
                            created_at TEXT NOT NULL,
                            verified_at TEXT,
                            published_at TEXT,
                            UNIQUE(project_id,id),
                            FOREIGN KEY(project_id,producer_job_id) REFERENCES jobs(project_id,id) ON DELETE RESTRICT,
                            FOREIGN KEY(producer_job_id,producer_attempt_id) REFERENCES job_attempts(job_id,id) ON DELETE RESTRICT,
                            CHECK(status NOT IN ('VERIFIED','READY','MISSING','CORRUPT') OR (sha256 IS NOT NULL AND file_size IS NOT NULL AND verification_metadata_json IS NOT NULL AND verification_version IS NOT NULL AND verified_at IS NOT NULL)),
                            CHECK(status <> 'READY' OR published_at IS NOT NULL),
                            CHECK(status <> 'STAGING' OR (sha256 IS NULL AND file_size IS NULL AND verification_metadata_json IS NULL AND verified_at IS NULL AND published_at IS NULL))
                        )""",
                        "CREATE INDEX artifacts_job_attempt_idx ON artifacts(project_id,producer_job_id,producer_attempt_id)",
                        """CREATE TRIGGER artifacts_status_transition_guard BEFORE UPDATE OF status ON artifacts
                            WHEN OLD.status <> NEW.status AND NOT (
                                (OLD.status='STAGING' AND NEW.status IN ('VERIFIED','FAILED')) OR
                                (OLD.status='VERIFIED' AND NEW.status IN ('READY','FAILED')) OR
                                (OLD.status='READY' AND NEW.status IN ('MISSING','CORRUPT'))
                            ) BEGIN SELECT RAISE(ABORT, 'invalid artifact state transition'); END""",
                        """CREATE TRIGGER artifacts_manifest_immutable BEFORE UPDATE OF id,project_id,producer_job_id,producer_attempt_id,kind,input_fingerprint,fingerprint_version,staging_relative_path,managed_relative_path,created_at ON artifacts
                            BEGIN SELECT RAISE(ABORT, 'artifact publication manifest is immutable'); END""",
                        """CREATE TRIGGER ready_artifact_content_immutable BEFORE UPDATE OF sha256,file_size,verification_metadata_json,verification_version,verified_at,published_at ON artifacts
                            WHEN OLD.status IN ('READY','MISSING','CORRUPT')
                            BEGIN SELECT RAISE(ABORT, 'verified artifact content is immutable'); END""",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (3, utc_now())
                    )
                    current = 3
                if current < 4 and target_version >= 4:
                    statements = (
                        "ALTER TABLE jobs ADD COLUMN activation_stage TEXT CHECK(activation_stage IS NULL OR activation_stage IN ('asr','translation','tts','sync','render'))",
                        "ALTER TABLE jobs ADD COLUMN expected_input_generation INTEGER CHECK(expected_input_generation IS NULL OR expected_input_generation >= 1)",
                        "ALTER TABLE jobs ADD COLUMN expected_pointer_generation INTEGER CHECK(expected_pointer_generation IS NULL OR expected_pointer_generation >= 0)",
                        "ALTER TABLE job_attempts ADD COLUMN activation_outcome TEXT NOT NULL DEFAULT 'NOT_APPLICABLE' CHECK(activation_outcome IN ('NOT_APPLICABLE','ACTIVE','STALE','CANCELLED'))",
                        "DROP TRIGGER jobs_input_immutable",
                        "DROP TRIGGER jobs_status_transition_guard",
                        """CREATE TRIGGER jobs_input_immutable BEFORE UPDATE OF id,project_id,kind,input_snapshot,input_fingerprint,idempotency_key,request_digest,activation_stage,expected_input_generation,expected_pointer_generation,created_at ON jobs
                            BEGIN SELECT RAISE(ABORT, 'job input snapshot is immutable'); END""",
                        """CREATE TRIGGER jobs_status_transition_guard BEFORE UPDATE OF status ON jobs
                            WHEN OLD.status <> NEW.status AND NOT (
                                (OLD.status='QUEUED' AND NEW.status IN ('RUNNING','CANCELLED','INTERRUPTED')) OR
                                (OLD.status='RUNNING' AND NEW.status IN ('CANCEL_REQUESTED','READY','FAILED','INTERRUPTED')) OR
                                (OLD.status='CANCEL_REQUESTED' AND NEW.status IN ('CANCELLED','INTERRUPTED')) OR
                                (OLD.status IN ('CANCELLED','READY','FAILED') AND NEW.status='INTERRUPTED') OR
                                (OLD.status IN ('FAILED','INTERRUPTED') AND NEW.status='QUEUED')
                            ) BEGIN SELECT RAISE(ABORT, 'invalid job state transition'); END""",
                        """CREATE TRIGGER jobs_activation_snapshot_guard BEFORE INSERT ON jobs
                            WHEN NOT ((NEW.activation_stage IS NULL AND NEW.expected_input_generation IS NULL AND NEW.expected_pointer_generation IS NULL) OR
                                      (NEW.activation_stage IN ('asr','translation','tts','sync','render') AND NEW.expected_input_generation IS NOT NULL AND NEW.expected_pointer_generation IS NOT NULL))
                            BEGIN SELECT RAISE(ABORT, 'invalid activation snapshot'); END""",
                        """CREATE TABLE stage_pointers (
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            stage TEXT NOT NULL CHECK(stage IN ('asr','translation','tts','sync','render')),
                            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64 AND input_fingerprint NOT GLOB '*[^a-f0-9]*'),
                            input_generation INTEGER NOT NULL CHECK(input_generation >= 1),
                            pointer_generation INTEGER NOT NULL DEFAULT 0 CHECK(pointer_generation >= 0),
                            active_artifact_id TEXT,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(project_id,stage),
                            FOREIGN KEY(project_id,active_artifact_id) REFERENCES artifacts(project_id,id) ON DELETE RESTRICT
                        )""",
                        """CREATE TRIGGER stage_pointer_generation_guard BEFORE UPDATE OF input_fingerprint,input_generation,pointer_generation,active_artifact_id ON stage_pointers
                            WHEN (OLD.input_fingerprint=NEW.input_fingerprint AND OLD.input_generation<>NEW.input_generation) OR
                                 (OLD.input_fingerprint<>NEW.input_fingerprint AND NEW.input_generation<>OLD.input_generation+1) OR
                                 (OLD.active_artifact_id IS NEW.active_artifact_id AND OLD.pointer_generation<>NEW.pointer_generation) OR
                                 (OLD.active_artifact_id IS NOT NEW.active_artifact_id AND NEW.pointer_generation<>OLD.pointer_generation+1)
                            BEGIN SELECT RAISE(ABORT, 'invalid stage pointer generation'); END""",
                        """CREATE TABLE recovery_findings (
                            id TEXT PRIMARY KEY NOT NULL,
                            relative_path TEXT NOT NULL CHECK(length(relative_path) BETWEEN 1 AND 4096 AND substr(relative_path,1,1)<>'/' AND instr(relative_path,'\\')=0 AND instr('/'||relative_path||'/','/../')=0),
                            finding_code TEXT NOT NULL CHECK(length(finding_code) BETWEEN 1 AND 80 AND finding_code NOT GLOB '*[^a-z0-9_]*'),
                            created_at TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','RESOLVED')),
                            UNIQUE(relative_path,finding_code)
                        )""",
                        "CREATE INDEX recovery_findings_open_idx ON recovery_findings(status,created_at)",
                        """CREATE TRIGGER attempt_activation_outcome_guard BEFORE UPDATE OF activation_outcome ON job_attempts
                            WHEN OLD.status<>'RUNNING' AND OLD.activation_outcome<>NEW.activation_outcome
                            BEGIN SELECT RAISE(ABORT, 'terminal activation outcome is immutable'); END""",
                        """CREATE TRIGGER artifacts_status_transition_guard_v4 BEFORE UPDATE OF status ON artifacts
                            WHEN OLD.status <> NEW.status AND NOT (
                                (OLD.status='STAGING' AND NEW.status IN ('VERIFIED','FAILED')) OR
                                (OLD.status='VERIFIED' AND NEW.status IN ('READY','FAILED','MISSING','CORRUPT')) OR
                                (OLD.status='READY' AND NEW.status IN ('MISSING','CORRUPT'))
                            ) BEGIN SELECT RAISE(ABORT, 'invalid artifact state transition'); END""",
                        "DROP TRIGGER artifacts_status_transition_guard",
                    )
                    for statement in statements[:-1]:
                        connection.execute(statement)
                    connection.execute(statements[-1])
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (4, utc_now())
                    )
                    current = 4
                if current < 5 and target_version >= 5:
                    statements = (
                        "CREATE TABLE selections (id_hash TEXT PRIMARY KEY NOT NULL, session_hash TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, purpose TEXT NOT NULL CHECK(purpose='source'), private_path TEXT NOT NULL, display_name TEXT NOT NULL, file_identity_json TEXT NOT NULL CHECK(json_valid(file_identity_json)), expires_at REAL NOT NULL, state TEXT NOT NULL CHECK(state IN ('ISSUED','CONSUMED','EXPIRED','REVOKED')), consumed_job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT, created_at TEXT NOT NULL)",
                        "CREATE INDEX selections_expiry_idx ON selections(state,expires_at)",
                        "CREATE TRIGGER selection_source_immutable BEFORE UPDATE OF id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,created_at ON selections BEGIN SELECT RAISE(ABORT, 'selection source snapshot is immutable'); END",
                        "CREATE TABLE media_assets (id TEXT PRIMARY KEY NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, mode TEXT NOT NULL CHECK(mode IN ('COPY','LINK')), status TEXT NOT NULL CHECK(status IN ('PENDING','READY','MISSING','CHANGED')), display_name TEXT NOT NULL, private_source_path TEXT, managed_artifact_id TEXT REFERENCES artifacts(id) ON DELETE RESTRICT, sha256 TEXT NOT NULL CHECK(length(sha256)=64), file_size INTEGER NOT NULL CHECK(file_size>=0), metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), source_identity_json TEXT NOT NULL CHECK(json_valid(source_identity_json)), producer_job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT, generation INTEGER NOT NULL CHECK(generation>=1), created_at TEXT NOT NULL, UNIQUE(project_id,generation), UNIQUE(project_id,id), CHECK((mode='COPY' AND private_source_path IS NULL AND managed_artifact_id IS NOT NULL) OR (mode='LINK' AND private_source_path IS NOT NULL)))",
                        "CREATE INDEX media_assets_project_idx ON media_assets(project_id,created_at DESC)",
                        "CREATE TABLE media_import_state (project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, desired_generation INTEGER NOT NULL CHECK(desired_generation>=0), active_generation INTEGER NOT NULL CHECK(active_generation>=0), active_asset_id TEXT, FOREIGN KEY(project_id,active_asset_id) REFERENCES media_assets(project_id,id) ON DELETE RESTRICT)",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (5, utc_now()))
                    current = 5
                if current < 6 and target_version >= 6:
                    statements = (
                        "DROP INDEX selections_expiry_idx",
                        "DROP TRIGGER selection_source_immutable",
                        "ALTER TABLE selections RENAME TO selections_v5",
                        "CREATE TABLE selections (id_hash TEXT PRIMARY KEY NOT NULL, session_hash TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT, purpose TEXT NOT NULL CHECK(purpose IN ('source','relink')), private_path TEXT NOT NULL, display_name TEXT NOT NULL, file_identity_json TEXT NOT NULL CHECK(json_valid(file_identity_json)), expires_at REAL NOT NULL, state TEXT NOT NULL CHECK(state IN ('ISSUED','CONSUMED','EXPIRED','REVOKED')), consumed_job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT, asset_id TEXT REFERENCES media_assets(id) ON DELETE RESTRICT, created_at TEXT NOT NULL, CHECK((purpose='source' AND asset_id IS NULL) OR (purpose='relink' AND asset_id IS NOT NULL)))",
                        "INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,consumed_job_id,created_at) SELECT id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,consumed_job_id,created_at FROM selections_v5",
                        "DROP TABLE selections_v5",
                        "CREATE INDEX selections_expiry_idx ON selections(state,expires_at)",
                        "CREATE TRIGGER selection_source_immutable BEFORE UPDATE OF id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,asset_id,created_at ON selections BEGIN SELECT RAISE(ABORT, 'selection source snapshot is immutable'); END",
                        "ALTER TABLE media_assets ADD COLUMN lock_version INTEGER NOT NULL DEFAULT 1 CHECK(lock_version>=1)",
                        "CREATE TABLE media_relink_history (id TEXT PRIMARY KEY NOT NULL, asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT, selection_hash TEXT NOT NULL, previous_private_source_path TEXT NOT NULL, previous_identity_json TEXT NOT NULL CHECK(json_valid(previous_identity_json)), previous_sha256 TEXT NOT NULL CHECK(length(previous_sha256)=64), new_private_source_path TEXT NOT NULL, new_identity_json TEXT NOT NULL CHECK(json_valid(new_identity_json)), relinked_at TEXT NOT NULL, UNIQUE(asset_id,selection_hash))",
                        "CREATE INDEX media_relink_history_asset_idx ON media_relink_history(asset_id,relinked_at DESC)",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (6, utc_now()))
                    current = 6
                if current < 7 and target_version >= 7:
                    statements = (
                        """CREATE TABLE transcription_runs (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            source_asset_id TEXT NOT NULL,
                            artifact_id TEXT NOT NULL,
                            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64 AND input_fingerprint NOT GLOB '*[^a-f0-9]*'),
                            model_id TEXT NOT NULL,
                            model_revision TEXT NOT NULL,
                            faster_whisper_version TEXT NOT NULL,
                            ctranslate2_version TEXT NOT NULL,
                            language TEXT,
                            language_probability REAL CHECK(language_probability IS NULL OR (language_probability>=0 AND language_probability<=1)),
                            vad_enabled INTEGER NOT NULL CHECK(vad_enabled IN (0,1)),
                            requested_device TEXT NOT NULL CHECK(requested_device IN ('auto','cpu','cuda')),
                            actual_device TEXT NOT NULL CHECK(actual_device IN ('cpu','cuda')),
                            compute_type TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN ('READY','NO_SPEECH','REVIEW_REQUIRED')),
                            duration_us INTEGER NOT NULL CHECK(duration_us>0),
                            audio_start_offset_us INTEGER NOT NULL,
                            timing_json TEXT NOT NULL CHECK(json_valid(timing_json)),
                            provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
                            created_at TEXT NOT NULL,
                            completed_at TEXT NOT NULL,
                            UNIQUE(project_id,id),
                            UNIQUE(project_id,artifact_id),
                            FOREIGN KEY(project_id,source_asset_id) REFERENCES media_assets(project_id,id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) ON DELETE RESTRICT
                        )""",
                        "CREATE INDEX transcription_runs_project_idx ON transcription_runs(project_id,created_at DESC,id)",
                        """CREATE TABLE segments (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL,
                            run_id TEXT NOT NULL,
                            position INTEGER NOT NULL CHECK(position>=0),
                            start_us INTEGER NOT NULL CHECK(start_us>=0),
                            end_us INTEGER NOT NULL CHECK(end_us>start_us),
                            source_text TEXT NOT NULL CHECK(length(source_text)>0),
                            avg_logprob REAL,
                            no_speech_prob REAL CHECK(no_speech_prob IS NULL OR (no_speech_prob>=0 AND no_speech_prob<=1)),
                            compression_ratio REAL CHECK(compression_ratio IS NULL OR compression_ratio>=0),
                            flags_json TEXT NOT NULL CHECK(json_valid(flags_json)),
                            UNIQUE(run_id,position),
                            UNIQUE(project_id,run_id,id),
                            FOREIGN KEY(project_id,run_id) REFERENCES transcription_runs(project_id,id) ON DELETE RESTRICT
                        )""",
                        "CREATE INDEX segments_run_order_idx ON segments(run_id,position)",
                        "CREATE TRIGGER transcription_runs_immutable_update BEFORE UPDATE ON transcription_runs BEGIN SELECT RAISE(ABORT, 'transcription runs are immutable'); END",
                        "CREATE TRIGGER transcription_runs_immutable_delete BEFORE DELETE ON transcription_runs BEGIN SELECT RAISE(ABORT, 'transcription runs are immutable'); END",
                        "CREATE TRIGGER segments_immutable_update BEFORE UPDATE ON segments BEGIN SELECT RAISE(ABORT, 'ASR segments are immutable'); END",
                        "CREATE TRIGGER segments_immutable_delete BEFORE DELETE ON segments BEGIN SELECT RAISE(ABORT, 'ASR segments are immutable'); END",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (7, utc_now()))
                    current = 7
                if current < 8 and target_version >= 8:
                    connection.execute(
                        """CREATE TABLE project_processing_sources (
                            project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            media_asset_id TEXT NOT NULL,
                            generation INTEGER NOT NULL CHECK(generation>=1),
                            updated_at TEXT NOT NULL,
                            FOREIGN KEY(project_id,media_asset_id) REFERENCES media_assets(project_id,id) ON DELETE RESTRICT
                        )"""
                    )
                    connection.execute("CREATE INDEX project_processing_source_asset_idx ON project_processing_sources(project_id,media_asset_id)")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (8, utc_now()))
                    current = 8
                if current < 9 and target_version >= 9:
                    statements = (
                        """CREATE TABLE transcript_revisions (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL,
                            run_id TEXT NOT NULL,
                            parent_revision_id TEXT,
                            revision_no INTEGER NOT NULL CHECK(revision_no>=1),
                            status TEXT NOT NULL CHECK(status IN ('DRAFT','ACCEPTED')),
                            lock_version INTEGER NOT NULL DEFAULT 1 CHECK(lock_version>=1),
                            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version>=1),
                            created_at TEXT NOT NULL,
                            accepted_at TEXT,
                            UNIQUE(project_id,id),
                            UNIQUE(run_id,revision_no),
                            FOREIGN KEY(project_id,run_id) REFERENCES transcription_runs(project_id,id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,parent_revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT,
                            CHECK((status='DRAFT' AND accepted_at IS NULL) OR (status='ACCEPTED' AND accepted_at IS NOT NULL))
                        )""",
                        "CREATE UNIQUE INDEX transcript_one_draft_per_run_idx ON transcript_revisions(project_id,run_id) WHERE status='DRAFT'",
                        "CREATE INDEX transcript_revisions_history_idx ON transcript_revisions(project_id,run_id,revision_no DESC)",
                        """CREATE TABLE revision_segments (
                            row_id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL,
                            revision_id TEXT NOT NULL,
                            stable_segment_key TEXT NOT NULL,
                            position INTEGER NOT NULL CHECK(position>=0),
                            text TEXT NOT NULL CHECK(length(text)>0),
                            start_us INTEGER NOT NULL CHECK(start_us>=0),
                            end_us INTEGER NOT NULL CHECK(end_us>start_us),
                            UNIQUE(project_id,revision_id,row_id),
                            UNIQUE(revision_id,stable_segment_key),
                            UNIQUE(revision_id,position),
                            FOREIGN KEY(project_id,revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT
                        )""",
                        "CREATE INDEX revision_segments_order_idx ON revision_segments(revision_id,position)",
                        "CREATE UNIQUE INDEX segments_project_id_unique_v9 ON segments(project_id,id)",
                        """CREATE TABLE revision_segment_parents (
                            project_id TEXT NOT NULL,
                            child_revision_id TEXT NOT NULL,
                            child_row_id TEXT NOT NULL,
                            parent_revision_id TEXT NOT NULL,
                            parent_stable_segment_key TEXT NOT NULL,
                            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
                            PRIMARY KEY(child_revision_id,child_row_id,ordinal),
                            FOREIGN KEY(project_id,child_revision_id,child_row_id) REFERENCES revision_segments(project_id,revision_id,row_id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,parent_revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT,
                            CHECK(child_revision_id<>parent_revision_id OR length(parent_stable_segment_key)>0)
                        )""",
                        """CREATE TABLE coverage_groups (
                            group_id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL,
                            revision_id TEXT NOT NULL,
                            review_status TEXT NOT NULL CHECK(review_status IN ('PENDING','REVIEWED')),
                            disposition TEXT NOT NULL CHECK(disposition IN ('INCLUDED','SKIPPED')),
                            skip_reason TEXT,
                            UNIQUE(project_id,revision_id,group_id),
                            FOREIGN KEY(project_id,revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT,
                            CHECK((disposition='SKIPPED' AND skip_reason IS NOT NULL AND length(trim(skip_reason))>0) OR (disposition='INCLUDED' AND skip_reason IS NULL))
                        )""",
                        """CREATE TABLE coverage_group_sources (
                            project_id TEXT NOT NULL,
                            revision_id TEXT NOT NULL,
                            group_id TEXT NOT NULL,
                            source_id TEXT NOT NULL,
                            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
                            PRIMARY KEY(revision_id,source_id),
                            UNIQUE(group_id,ordinal),
                            FOREIGN KEY(project_id,revision_id,group_id) REFERENCES coverage_groups(project_id,revision_id,group_id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,source_id) REFERENCES segments(project_id,id) ON DELETE RESTRICT
                        )""",
                        """CREATE TABLE coverage_group_members (
                            project_id TEXT NOT NULL,
                            revision_id TEXT NOT NULL,
                            group_id TEXT NOT NULL,
                            row_id TEXT NOT NULL,
                            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
                            PRIMARY KEY(revision_id,row_id),
                            UNIQUE(group_id,ordinal),
                            FOREIGN KEY(project_id,revision_id,group_id) REFERENCES coverage_groups(project_id,revision_id,group_id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,revision_id,row_id) REFERENCES revision_segments(project_id,revision_id,row_id) ON DELETE RESTRICT
                        )""",
                        """CREATE TABLE project_active_transcript_revisions (
                            project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            revision_id TEXT NOT NULL,
                            generation INTEGER NOT NULL CHECK(generation>=1),
                            updated_at TEXT NOT NULL,
                            FOREIGN KEY(project_id,revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT
                        )""",
                        """CREATE TRIGGER accepted_transcript_revision_immutable BEFORE UPDATE ON transcript_revisions
                            WHEN OLD.status='ACCEPTED' BEGIN SELECT RAISE(ABORT, 'accepted transcript revision is immutable'); END""",
                        """CREATE TRIGGER accepted_transcript_revision_delete_guard BEFORE DELETE ON transcript_revisions
                            WHEN OLD.status='ACCEPTED' BEGIN SELECT RAISE(ABORT, 'accepted transcript revision cannot be deleted'); END""",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    for table in ("revision_segments", "revision_segment_parents", "coverage_groups", "coverage_group_sources", "coverage_group_members"):
                        revision_column = "child_revision_id" if table == "revision_segment_parents" else "revision_id"
                        connection.execute(
                            f"""CREATE TRIGGER {table}_draft_insert_guard BEFORE INSERT ON {table}
                                WHEN (SELECT status FROM transcript_revisions WHERE id=NEW.{revision_column}) <> 'DRAFT'
                                BEGIN SELECT RAISE(ABORT, 'transcript revision is immutable'); END"""
                        )
                        connection.execute(
                            f"""CREATE TRIGGER {table}_draft_update_guard BEFORE UPDATE ON {table}
                                WHEN (SELECT status FROM transcript_revisions WHERE id=OLD.{revision_column}) <> 'DRAFT'
                                  OR (SELECT status FROM transcript_revisions WHERE id=NEW.{revision_column}) <> 'DRAFT'
                                BEGIN SELECT RAISE(ABORT, 'transcript revision is immutable'); END"""
                        )
                        connection.execute(
                            f"""CREATE TRIGGER {table}_draft_delete_guard BEFORE DELETE ON {table}
                                WHEN (SELECT status FROM transcript_revisions WHERE id=OLD.{revision_column}) <> 'DRAFT'
                                BEGIN SELECT RAISE(ABORT, 'transcript revision is immutable'); END"""
                        )
                    connection.execute("CREATE TRIGGER active_transcript_revision_accepted_insert BEFORE INSERT ON project_active_transcript_revisions WHEN (SELECT status FROM transcript_revisions WHERE id=NEW.revision_id)<>'ACCEPTED' BEGIN SELECT RAISE(ABORT, 'active transcript revision must be accepted'); END")
                    connection.execute("CREATE TRIGGER active_transcript_revision_accepted_update BEFORE UPDATE ON project_active_transcript_revisions WHEN (SELECT status FROM transcript_revisions WHERE id=NEW.revision_id)<>'ACCEPTED' BEGIN SELECT RAISE(ABORT, 'active transcript revision must be accepted'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (9, utc_now()))
                    current = 9
                if current < 10 and target_version >= 10:
                    statements = (
                        """CREATE TABLE translation_runs (
                            id TEXT PRIMARY KEY NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                            producer_job_id TEXT NOT NULL UNIQUE,
                            transcript_revision_id TEXT NOT NULL,
                            source_asset_id TEXT NOT NULL,
                            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
                            provider TEXT NOT NULL CHECK(provider='gemini'),
                            model_id TEXT NOT NULL,
                            provider_sdk_version TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','PARTIAL','REVIEW_REQUIRED','WAITING_FOR_QUOTA','FAILED','READY','STALE','CANCELLED')),
                            artifact_id TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
                            created_at TEXT NOT NULL,
                            completed_at TEXT,
                            error_code TEXT,
                            UNIQUE(project_id,id),
                            FOREIGN KEY(project_id,producer_job_id) REFERENCES jobs(project_id,id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,transcript_revision_id) REFERENCES transcript_revisions(project_id,id) ON DELETE RESTRICT,
                            FOREIGN KEY(project_id,source_asset_id) REFERENCES media_assets(project_id,id) ON DELETE RESTRICT
                        )""",
                        "CREATE INDEX translation_runs_project_idx ON translation_runs(project_id,created_at DESC,id)",
                        """CREATE TABLE translation_chunks (
                            run_id TEXT NOT NULL REFERENCES translation_runs(id) ON DELETE RESTRICT,
                            chunk_no INTEGER NOT NULL CHECK(chunk_no>=0),
                            chunk_id TEXT NOT NULL,
                            unit_ids_json TEXT NOT NULL CHECK(json_valid(unit_ids_json)),
                            status TEXT NOT NULL CHECK(status IN ('PENDING','PASS1_READY','READY','FAILED','REVIEW_REQUIRED')),
                            pass1_attempts INTEGER NOT NULL DEFAULT 0 CHECK(pass1_attempts>=0),
                            pass2_attempts INTEGER NOT NULL DEFAULT 0 CHECK(pass2_attempts>=0),
                            error_code TEXT,
                            PRIMARY KEY(run_id,chunk_no),
                            UNIQUE(run_id,chunk_id)
                        )""",
                        """CREATE TABLE translation_units (
                            run_id TEXT NOT NULL REFERENCES translation_runs(id) ON DELETE RESTRICT,
                            project_id TEXT NOT NULL,
                            unit_id TEXT NOT NULL,
                            logical_unit_key TEXT NOT NULL,
                            position INTEGER NOT NULL CHECK(position>=0),
                            chunk_no INTEGER NOT NULL CHECK(chunk_no>=0),
                            revision_segment_ids_json TEXT NOT NULL CHECK(json_valid(revision_segment_ids_json)),
                            source_segment_ids_json TEXT NOT NULL CHECK(json_valid(source_segment_ids_json)),
                            source_text TEXT NOT NULL,
                            start_us INTEGER NOT NULL CHECK(start_us>=0),
                            end_us INTEGER NOT NULL CHECK(end_us>start_us),
                            pass1_text TEXT,
                            pass2_text TEXT,
                            pass1_notes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(pass1_notes_json)),
                            review_flags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(review_flags_json)),
                            status TEXT NOT NULL CHECK(status IN ('PENDING','PASS1_READY','READY','REVIEW_REQUIRED','FAILED')),
                            error_code TEXT,
                            PRIMARY KEY(run_id,unit_id),
                            UNIQUE(run_id,logical_unit_key),
                            UNIQUE(run_id,position),
                            FOREIGN KEY(project_id,run_id) REFERENCES translation_runs(project_id,id) ON DELETE RESTRICT
                        )""",
                        "CREATE INDEX translation_units_order_idx ON translation_units(run_id,position)",
                        """CREATE TRIGGER translation_run_input_immutable BEFORE UPDATE OF id,project_id,producer_job_id,transcript_revision_id,source_asset_id,input_fingerprint,provider,model_id,provider_sdk_version,prompt_version,created_at ON translation_runs BEGIN SELECT RAISE(ABORT, 'translation run input is immutable'); END""",
                        """CREATE TRIGGER translation_run_artifact_immutable BEFORE UPDATE OF artifact_id ON translation_runs WHEN OLD.artifact_id IS NOT NULL AND OLD.artifact_id IS NOT NEW.artifact_id BEGIN SELECT RAISE(ABORT, 'translation artifact identity is immutable'); END""",
                        """CREATE TRIGGER translation_unit_mapping_immutable BEFORE UPDATE OF run_id,project_id,unit_id,logical_unit_key,position,chunk_no,revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us ON translation_units BEGIN SELECT RAISE(ABORT, 'translation unit mapping is immutable'); END""",
                        """CREATE TRIGGER translation_unit_result_immutable BEFORE UPDATE OF pass1_text,pass2_text,pass1_notes_json,review_flags_json ON translation_units WHEN (OLD.pass1_text IS NOT NULL AND (OLD.pass1_text IS NOT NEW.pass1_text OR OLD.pass1_notes_json IS NOT NEW.pass1_notes_json)) OR (OLD.pass2_text IS NOT NULL AND (OLD.pass2_text IS NOT NEW.pass2_text OR OLD.review_flags_json IS NOT NEW.review_flags_json)) BEGIN SELECT RAISE(ABORT, 'verified translation output is immutable'); END""",
                    )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (10, utc_now()))
                    current = 10
                if current < 11 and target_version >= 11:
                    connection.execute("ALTER TABLE job_attempts ADD COLUMN retry_after_at TEXT")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (11, utc_now()))
                    current = 11
                if current < 12 and target_version >= 12:
                    connection.execute("""CREATE TABLE translation_credential_states (
                        alias TEXT PRIMARY KEY NOT NULL,
                        quota_group TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('AVAILABLE','AUTH_FAILED','DISABLED','RATE_LIMITED','QUOTA_EXHAUSTED','COOLDOWN','UNKNOWN')),
                        cooldown_until TEXT,
                        failure_code TEXT,
                        updated_at TEXT NOT NULL
                    )""")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (12, utc_now()))
                    current = 12
                if current < 13 and target_version >= 13:
                    connection.execute("""CREATE TABLE translation_provider_calls (
                        call_id TEXT PRIMARY KEY NOT NULL,
                        run_id TEXT NOT NULL REFERENCES translation_runs(id) ON DELETE RESTRICT,
                        job_attempt_id TEXT NOT NULL REFERENCES job_attempts(id) ON DELETE RESTRICT,
                        chunk_id TEXT NOT NULL,
                        pass_number INTEGER NOT NULL CHECK(pass_number IN (1,2)),
                        model_id TEXT NOT NULL,
                        api_type TEXT NOT NULL,
                        api_version TEXT NOT NULL,
                        sdk_version TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        wire_protocol_version TEXT NOT NULL,
                        credential_alias TEXT,
                        quota_group_alias TEXT,
                        sdk_call_count INTEGER NOT NULL CHECK(sdk_call_count>=0),
                        physical_http_request_count INTEGER NOT NULL CHECK(physical_http_request_count>=0),
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        requested_unit_count INTEGER NOT NULL CHECK(requested_unit_count>=0),
                        received_unit_count INTEGER,
                        latency_ms INTEGER NOT NULL CHECK(latency_ms>=0),
                        finish_reason TEXT,
                        provider_request_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('COMPLETED','INVALID','FAILED')),
                        error_code TEXT,
                        created_at TEXT NOT NULL
                    )""")
                    connection.execute("CREATE INDEX translation_provider_calls_run_idx ON translation_provider_calls(run_id,chunk_id,pass_number,created_at)")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (13, utc_now()))
                    current = 13
                if current < 14 and target_version >= 14:
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN api_type TEXT NOT NULL DEFAULT 'interactions'")
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN api_version TEXT NOT NULL DEFAULT 'v1beta'")
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN wire_protocol_version TEXT NOT NULL DEFAULT 'translation-wire-v1'")
                    connection.execute("""CREATE TRIGGER translation_run_route_immutable BEFORE UPDATE OF model_id,api_type,api_version,wire_protocol_version ON translation_runs
                        BEGIN SELECT RAISE(ABORT, 'translation run route is immutable'); END""")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (14, utc_now()))
                    current = 14
                if current < 15 and target_version >= 15:
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN http_status INTEGER")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN request_characters INTEGER")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN estimated_input_tokens INTEGER")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (15, utc_now()))
                    current = 15
                if current < 16 and target_version >= 16:
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN pass_strategy TEXT NOT NULL DEFAULT 'TWO_PASS' CHECK(pass_strategy IN ('TWO_PASS','SINGLE_PASS'))")
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN normalization_policy_version TEXT NOT NULL DEFAULT 'terminal-punctuation-v1'")
                    connection.execute("ALTER TABLE translation_units ADD COLUMN final_text TEXT")
                    connection.execute("ALTER TABLE translation_units ADD COLUMN final_notes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(final_notes_json))")
                    connection.execute("CREATE TRIGGER translation_unit_final_result_immutable BEFORE UPDATE OF final_text,final_notes_json ON translation_units WHEN OLD.final_text IS NOT NULL AND (OLD.final_text IS NOT NEW.final_text OR OLD.final_notes_json IS NOT NEW.final_notes_json) BEGIN SELECT RAISE(ABORT, 'verified translation output is immutable'); END")
                    connection.execute("CREATE TRIGGER translation_run_strategy_immutable BEFORE UPDATE OF pass_strategy,normalization_policy_version ON translation_runs BEGIN SELECT RAISE(ABORT, 'translation run strategy is immutable'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (16, utc_now()))
                    current = 16
                if current < 17 and target_version >= 17:
                    connection.execute("CREATE UNIQUE INDEX translation_units_project_lookup_idx ON translation_units(project_id,run_id,unit_id)")
                    connection.execute("""CREATE TABLE translation_revisions (
                        id TEXT PRIMARY KEY NOT NULL,
                        project_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        parent_revision_id TEXT,
                        source_revision_id TEXT,
                        source_kind TEXT NOT NULL CHECK(source_kind IN ('PROVIDER_OUTPUT','ACCEPTED_REVISION')),
                        revision_no INTEGER NOT NULL CHECK(revision_no>=1),
                        status TEXT NOT NULL CHECK(status IN ('DRAFT','ACCEPTED')),
                        lock_version INTEGER NOT NULL DEFAULT 1 CHECK(lock_version>=1),
                        schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version>=1),
                        fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
                        fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version>=1),
                        provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
                        created_at TEXT NOT NULL,
                        accepted_at TEXT,
                        UNIQUE(project_id,id),
                        UNIQUE(run_id,revision_no),
                        FOREIGN KEY(project_id,run_id) REFERENCES translation_runs(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,parent_revision_id) REFERENCES translation_revisions(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,source_revision_id) REFERENCES translation_revisions(project_id,id) ON DELETE RESTRICT,
                        CHECK((status='DRAFT' AND accepted_at IS NULL) OR (status='ACCEPTED' AND accepted_at IS NOT NULL))
                    )""")
                    connection.execute("CREATE UNIQUE INDEX translation_revisions_one_draft_idx ON translation_revisions(run_id) WHERE status='DRAFT'")
                    connection.execute("CREATE INDEX translation_revisions_latest_idx ON translation_revisions(project_id,run_id,status,revision_no DESC)")
                    connection.execute("""CREATE TABLE translation_revision_units (
                        project_id TEXT NOT NULL,
                        revision_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        unit_id TEXT NOT NULL,
                        logical_unit_key TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK(position>=0),
                        revision_segment_ids_json TEXT NOT NULL CHECK(json_valid(revision_segment_ids_json)),
                        source_segment_ids_json TEXT NOT NULL CHECK(json_valid(source_segment_ids_json)),
                        source_text TEXT NOT NULL,
                        start_us INTEGER NOT NULL CHECK(start_us>=0),
                        end_us INTEGER NOT NULL CHECK(end_us>start_us),
                        final_text TEXT NOT NULL CHECK(length(trim(final_text))>0),
                        unit_fingerprint TEXT NOT NULL CHECK(length(unit_fingerprint)=64),
                        text_origin TEXT NOT NULL CHECK(text_origin IN ('PROVIDER_OUTPUT','USER_EDIT')),
                        PRIMARY KEY(revision_id,unit_id),
                        UNIQUE(revision_id,position),
                        FOREIGN KEY(project_id,revision_id) REFERENCES translation_revisions(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,run_id,unit_id) REFERENCES translation_units(project_id,run_id,unit_id) ON DELETE RESTRICT
                    )""")
                    connection.execute("""CREATE TABLE project_active_translation_revisions (
                        project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                        revision_id TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK(generation>=1),
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(project_id,revision_id) REFERENCES translation_revisions(project_id,id) ON DELETE RESTRICT
                    )""")
                    connection.execute("""CREATE TRIGGER accepted_translation_revision_immutable BEFORE UPDATE ON translation_revisions
                        WHEN OLD.status='ACCEPTED' BEGIN SELECT RAISE(ABORT, 'accepted translation revision is immutable'); END""")
                    connection.execute("""CREATE TRIGGER accepted_translation_revision_delete_guard BEFORE DELETE ON translation_revisions
                        WHEN OLD.status='ACCEPTED' BEGIN SELECT RAISE(ABORT, 'accepted translation revision cannot be deleted'); END""")
                    connection.execute("""CREATE TRIGGER translation_revision_status_guard BEFORE UPDATE OF status ON translation_revisions
                        WHEN NOT (OLD.status='DRAFT' AND NEW.status='ACCEPTED') BEGIN SELECT RAISE(ABORT, 'invalid translation revision transition'); END""")
                    connection.execute("""CREATE TRIGGER translation_revision_units_mapping_immutable BEFORE UPDATE OF project_id,revision_id,run_id,unit_id,logical_unit_key,position,revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us ON translation_revision_units
                        BEGIN SELECT RAISE(ABORT, 'translation revision unit mapping is immutable'); END""")
                    connection.execute("""CREATE TRIGGER translation_revision_units_draft_insert BEFORE INSERT ON translation_revision_units
                        WHEN (SELECT status FROM translation_revisions WHERE id=NEW.revision_id)<>'DRAFT'
                        BEGIN SELECT RAISE(ABORT, 'translation revision units can only be inserted into a draft'); END""")
                    connection.execute("""CREATE TRIGGER accepted_translation_revision_units_immutable BEFORE UPDATE ON translation_revision_units
                        WHEN (SELECT status FROM translation_revisions WHERE id=OLD.revision_id)='ACCEPTED'
                        BEGIN SELECT RAISE(ABORT, 'accepted translation revision unit is immutable'); END""")
                    connection.execute("""CREATE TRIGGER accepted_translation_revision_units_delete_guard BEFORE DELETE ON translation_revision_units
                        WHEN (SELECT status FROM translation_revisions WHERE id=OLD.revision_id)='ACCEPTED'
                        BEGIN SELECT RAISE(ABORT, 'accepted translation revision unit cannot be deleted'); END""")
                    connection.execute("CREATE TRIGGER active_translation_revision_accepted_insert BEFORE INSERT ON project_active_translation_revisions WHEN (SELECT status FROM translation_revisions WHERE id=NEW.revision_id)<>'ACCEPTED' BEGIN SELECT RAISE(ABORT, 'active translation revision must be accepted'); END")
                    connection.execute("CREATE TRIGGER active_translation_revision_accepted_update BEFORE UPDATE ON project_active_translation_revisions WHEN (SELECT status FROM translation_revisions WHERE id=NEW.revision_id)<>'ACCEPTED' BEGIN SELECT RAISE(ABORT, 'active translation revision must be accepted'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (17, utc_now()))
                    current = 17
                if current < 18 and target_version >= 18:
                    connection.execute("""CREATE TABLE tts_runs (
                        id TEXT PRIMARY KEY NOT NULL,
                        project_id TEXT NOT NULL,
                        source_asset_id TEXT NOT NULL,
                        translation_run_id TEXT NOT NULL,
                        translation_revision_id TEXT NOT NULL,
                        producer_job_id TEXT NOT NULL UNIQUE,
                        input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
                        revision_fingerprint TEXT NOT NULL CHECK(length(revision_fingerprint)=64),
                        config_json TEXT NOT NULL CHECK(json_valid(config_json)),
                        provider_version TEXT NOT NULL,
                        adapter_version TEXT NOT NULL,
                        synthesis_version TEXT NOT NULL,
                        expected_input_generation INTEGER NOT NULL CHECK(expected_input_generation>=1),
                        expected_pointer_generation INTEGER NOT NULL CHECK(expected_pointer_generation>=0),
                        status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','PARTIAL','READY','FAILED','CANCELLED','INTERRUPTED','STALE')),
                        manifest_artifact_id TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        UNIQUE(project_id,id),
                        FOREIGN KEY(project_id,source_asset_id) REFERENCES media_assets(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,translation_revision_id) REFERENCES translation_revisions(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,translation_run_id) REFERENCES translation_runs(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,producer_job_id) REFERENCES jobs(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,manifest_artifact_id) REFERENCES artifacts(project_id,id) ON DELETE RESTRICT
                    )""")
                    connection.execute("CREATE INDEX tts_runs_project_created_idx ON tts_runs(project_id,created_at DESC,id)")
                    connection.execute("""CREATE TABLE tts_run_units (
                        project_id TEXT NOT NULL,
                        tts_run_id TEXT NOT NULL,
                        unit_id TEXT NOT NULL,
                        logical_unit_key TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK(position>=0),
                        source_segment_ids_json TEXT NOT NULL CHECK(json_valid(source_segment_ids_json)),
                        revision_segment_ids_json TEXT NOT NULL CHECK(json_valid(revision_segment_ids_json)),
                        source_text TEXT NOT NULL,
                        accepted_text TEXT NOT NULL CHECK(length(trim(accepted_text))>0),
                        text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
                        start_us INTEGER NOT NULL CHECK(start_us>=0),
                        end_us INTEGER NOT NULL CHECK(end_us>start_us),
                        source_window_duration_us INTEGER NOT NULL CHECK(source_window_duration_us=end_us-start_us),
                        synthesis_fingerprint TEXT NOT NULL CHECK(length(synthesis_fingerprint)=64),
                        status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','READY','FAILED','REVIEW_REQUIRED','CANCELLED','STALE')),
                        artifact_id TEXT,
                        cache_hit INTEGER NOT NULL DEFAULT 0 CHECK(cache_hit IN (0,1)),
                        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
                        retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
                        error_code TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(tts_run_id,unit_id),
                        UNIQUE(tts_run_id,logical_unit_key),
                        UNIQUE(tts_run_id,position),
                        FOREIGN KEY(project_id,tts_run_id) REFERENCES tts_runs(project_id,id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) ON DELETE RESTRICT
                    )""")
                    connection.execute("CREATE INDEX tts_run_units_status_idx ON tts_run_units(tts_run_id,status,position)")
                    connection.execute("CREATE INDEX tts_run_units_fingerprint_idx ON tts_run_units(project_id,synthesis_fingerprint,status)")
                    connection.execute("""CREATE TABLE tts_preview_cache (
                        fingerprint TEXT PRIMARY KEY NOT NULL CHECK(length(fingerprint)=64),
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                        artifact_id TEXT NOT NULL UNIQUE,
                        text_sha256 TEXT NOT NULL CHECK(length(text_sha256)=64),
                        created_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL,
                        FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) ON DELETE RESTRICT
                    )""")
                    connection.execute("""CREATE TRIGGER tts_run_input_immutable BEFORE UPDATE OF id,project_id,source_asset_id,translation_run_id,translation_revision_id,producer_job_id,input_fingerprint,revision_fingerprint,config_json,provider_version,adapter_version,synthesis_version,expected_input_generation,expected_pointer_generation,created_at ON tts_runs
                        BEGIN SELECT RAISE(ABORT, 'TTS run input snapshot is immutable'); END""")
                    connection.execute("""CREATE TRIGGER tts_run_unit_input_immutable BEFORE UPDATE OF project_id,tts_run_id,unit_id,logical_unit_key,position,source_segment_ids_json,revision_segment_ids_json,source_text,accepted_text,text_sha256,start_us,end_us,source_window_duration_us,synthesis_fingerprint ON tts_run_units
                        BEGIN SELECT RAISE(ABORT, 'TTS unit input snapshot is immutable'); END""")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (18, utc_now()))
                    current = 18
                if current < 19 and target_version >= 19:
                    connection.execute("ALTER TABLE artifacts ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'")
                    connection.execute("UPDATE artifacts SET content_fingerprint=input_fingerprint")
                    connection.execute("CREATE TRIGGER artifact_content_fingerprint_immutable BEFORE UPDATE OF content_fingerprint ON artifacts BEGIN SELECT RAISE(ABORT, 'artifact content fingerprint is immutable'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (19, utc_now()))
                    current = 19
                if current < 20 and target_version >= 20:
                    # SQLite cannot drop a CHECK/NOT NULL constraint in-place. Relax only
                    # the provider discriminator and SDK provenance columns, preserving
                    # every existing row and all child-table relationships.
                    old_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
                    connection.execute("PRAGMA writable_schema=ON")
                    try:
                        changed = connection.execute("UPDATE sqlite_schema SET sql=replace(sql,\"CHECK(provider='gemini')\",\"CHECK(provider IN ('gemini','cloudflare'))\") WHERE type='table' AND name='translation_runs'").rowcount
                        if changed != 1:
                            raise RuntimeError("Could not safely widen translation provider constraint.")
                        changed = connection.execute("UPDATE sqlite_schema SET sql=replace(sql,\"provider_sdk_version TEXT NOT NULL\",\"provider_sdk_version TEXT\") WHERE type='table' AND name='translation_runs'").rowcount
                        if changed != 1:
                            raise RuntimeError("Could not safely make provider SDK provenance optional.")
                        changed = connection.execute("UPDATE sqlite_schema SET sql=replace(sql,\"sdk_version TEXT NOT NULL\",\"sdk_version TEXT\") WHERE type='table' AND name='translation_provider_calls'").rowcount
                        if changed != 1:
                            raise RuntimeError("Could not safely make provider-call SDK provenance optional.")
                        connection.execute(f"PRAGMA schema_version={old_schema_version + 1}")
                    finally:
                        connection.execute("PRAGMA writable_schema=OFF")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (20, utc_now()))
                    current = 20
                if current < 21 and target_version >= 21:
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN adapter_version TEXT NOT NULL DEFAULT 'google-genai/3.6.1'")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN adapter_version TEXT NOT NULL DEFAULT 'google-genai/3.6.1'")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (21, utc_now()))
                    current = 21
                if current < 22 and target_version >= 22:
                    connection.execute("CREATE TRIGGER translation_run_adapter_immutable BEFORE UPDATE OF adapter_version ON translation_runs BEGIN SELECT RAISE(ABORT, 'translation adapter version is immutable'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (22, utc_now()))
                    current = 22
                if current < 23 and target_version >= 23:
                    connection.execute("ALTER TABLE translation_runs ADD COLUMN provider_strategy_version TEXT NOT NULL DEFAULT 'legacy-shared-v1'")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN provider_strategy_version TEXT NOT NULL DEFAULT 'legacy-shared-v1'")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN exception_class TEXT")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN timeout_phase TEXT")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN request_body_bytes INTEGER CHECK(request_body_bytes IS NULL OR request_body_bytes >= 0)")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN output_token_budget INTEGER CHECK(output_token_budget IS NULL OR output_token_budget >= 0)")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN attempt_elapsed_ms INTEGER CHECK(attempt_elapsed_ms IS NULL OR attempt_elapsed_ms >= 0)")
                    connection.execute("ALTER TABLE translation_provider_calls ADD COLUMN provider_operation_elapsed_ms INTEGER CHECK(provider_operation_elapsed_ms IS NULL OR provider_operation_elapsed_ms >= 0)")
                    connection.execute("CREATE TRIGGER translation_provider_strategy_immutable BEFORE UPDATE OF provider_strategy_version ON translation_runs BEGIN SELECT RAISE(ABORT, 'translation provider strategy is immutable'); END")
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (23, utc_now()))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        if target_version >= 24:
            self._migrate_v24()
        if target_version >= 25:
            self._migrate_v25()

    def _migrate_v25(self) -> None:
        """Add private, content-free per-call TTS diagnostics."""
        with closing(self.connect()) as connection:
            current = connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            if current >= 25:
                return
            if current != 24:
                raise RuntimeError(f"Migration v25 requires schema v24, found {current}.")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("""CREATE TABLE tts_provider_calls (
                    id TEXT PRIMARY KEY NOT NULL,
                    project_id TEXT NOT NULL,
                    tts_run_id TEXT,
                    unit_id TEXT,
                    job_attempt_id TEXT NOT NULL REFERENCES job_attempts(id) ON DELETE RESTRICT,
                    call_kind TEXT NOT NULL CHECK(call_kind IN ('batch_unit','preview')),
                    round_no INTEGER NOT NULL CHECK(round_no BETWEEN 1 AND 3),
                    call_no INTEGER NOT NULL CHECK(call_no>=1),
                    status TEXT NOT NULL CHECK(status IN ('RUNNING','READY','FAILED','CANCELLED','INTERRUPTED')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    elapsed_ms INTEGER CHECK(elapsed_ms IS NULL OR elapsed_ms>=0),
                    error_code TEXT,
                    exception_class TEXT,
                    http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
                    retry_after TEXT,
                    CHECK((tts_run_id IS NULL)=(unit_id IS NULL)),
                    FOREIGN KEY(project_id,tts_run_id) REFERENCES tts_runs(project_id,id) ON DELETE RESTRICT,
                    FOREIGN KEY(tts_run_id,unit_id) REFERENCES tts_run_units(tts_run_id,unit_id) ON DELETE RESTRICT
                )""")
                connection.execute("CREATE INDEX tts_provider_calls_unit_idx ON tts_provider_calls(tts_run_id,unit_id,call_no)")
                connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(25,?)", (utc_now(),))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migrate_v24(self) -> None:
        """Rebuild only Translation tables with FK checks around the atomic swap."""
        with closing(self.connect()) as connection:
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if current >= 24:
                return
            if current != 23:
                raise RuntimeError(f"Migration v24 requires schema v23, found {current}.")
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                sql_by_table = {}
                for table in ("translation_runs", "translation_units"):
                    row = connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if row is None or not isinstance(row["sql"], str):
                        raise RuntimeError(f"Migration v24 cannot find {table} schema.")
                    sql_by_table[table] = row["sql"]
                objects = connection.execute(
                    "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name IN ('translation_runs','translation_units') "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
                ).fetchall()

                run_sql = sql_by_table["translation_runs"]
                run_sql = run_sql.replace("CREATE TABLE translation_runs", "CREATE TABLE translation_runs_v24", 1)
                old_provider = "CHECK(provider IN ('gemini','cloudflare'))"
                if old_provider not in run_sql:
                    raise RuntimeError("Migration v24 found an unexpected Translation provider constraint.")
                run_sql = run_sql.replace(old_provider, "CHECK(provider IN ('gemini','cloudflare','local'))", 1)
                old_sdk = "provider_sdk_version TEXT NOT NULL"
                if old_sdk in run_sql:
                    run_sql = run_sql.replace(old_sdk, "provider_sdk_version TEXT", 1)

                unit_sql = sql_by_table["translation_units"]
                unit_sql = unit_sql.replace("CREATE TABLE translation_units", "CREATE TABLE translation_units_v24", 1)
                old_chunk = "chunk_no INTEGER NOT NULL CHECK(chunk_no>=0)"
                if old_chunk not in unit_sql:
                    raise RuntimeError("Migration v24 found an unexpected Translation Unit chunk constraint.")
                unit_sql = unit_sql.replace(old_chunk, "chunk_no INTEGER CHECK(chunk_no IS NULL OR chunk_no>=0)", 1)

                connection.execute(run_sql)
                connection.execute(unit_sql)
                connection.execute(
                    "INSERT INTO translation_runs_v24 SELECT * FROM translation_runs"
                )
                connection.execute(
                    "INSERT INTO translation_units_v24 SELECT * FROM translation_units"
                )
                connection.execute("DROP TABLE translation_units")
                connection.execute("DROP TABLE translation_runs")
                connection.execute("ALTER TABLE translation_runs_v24 RENAME TO translation_runs")
                connection.execute("ALTER TABLE translation_units_v24 RENAME TO translation_units")
                for item in objects:
                    connection.execute(item["sql"])

                connection.execute("""CREATE TABLE local_translation_state (
                    run_id TEXT PRIMARY KEY NOT NULL REFERENCES translation_runs(id) ON DELETE RESTRICT,
                    next_position INTEGER NOT NULL DEFAULT 0 CHECK(next_position>=0),
                    batch_limit INTEGER NOT NULL CHECK(batch_limit IN (80,60)),
                    timeout_restart_used INTEGER NOT NULL DEFAULT 0 CHECK(timeout_restart_used IN (0,1)),
                    updated_at TEXT NOT NULL
                )""")
                connection.execute("""CREATE TABLE local_translation_requests (
                    request_id TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL REFERENCES translation_runs(id) ON DELETE RESTRICT,
                    job_attempt_id TEXT NOT NULL REFERENCES job_attempts(id) ON DELETE RESTRICT,
                    request_no INTEGER NOT NULL CHECK(request_no>=1),
                    start_position INTEGER NOT NULL CHECK(start_position>=0),
                    unit_count INTEGER NOT NULL CHECK(unit_count BETWEEN 1 AND 80),
                    status TEXT NOT NULL CHECK(status IN ('RUNNING','READY','TIMEOUT','CONTEXT_LIMIT','INVALID','FAILED','CANCELLED','INTERRUPTED')),
                    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
                    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
                    finish_reason TEXT,
                    elapsed_ms INTEGER CHECK(elapsed_ms IS NULL OR elapsed_ms>=0),
                    error_code TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(run_id,request_no)
                )""")
                connection.execute("CREATE UNIQUE INDEX local_translation_one_running_idx ON local_translation_requests(run_id) WHERE status='RUNNING'")
                connection.execute("CREATE INDEX local_translation_requests_attempt_idx ON local_translation_requests(job_attempt_id,request_no)")
                connection.execute("""CREATE TRIGGER local_translation_state_insert_guard
                    BEFORE INSERT ON local_translation_state
                    WHEN (SELECT provider FROM translation_runs WHERE id=NEW.run_id)<>'local'
                      OR NEW.next_position>(SELECT COUNT(*) FROM translation_units WHERE run_id=NEW.run_id)
                    BEGIN SELECT RAISE(ABORT,'invalid local translation state'); END""")
                connection.execute("""CREATE TRIGGER local_translation_state_update_guard
                    BEFORE UPDATE OF run_id,next_position,batch_limit,timeout_restart_used ON local_translation_state
                    WHEN NEW.run_id<>OLD.run_id
                      OR NEW.next_position>(SELECT COUNT(*) FROM translation_units WHERE run_id=NEW.run_id)
                    BEGIN SELECT RAISE(ABORT,'invalid local translation state update'); END""")
                connection.execute("""CREATE TRIGGER local_translation_request_insert_guard
                    BEFORE INSERT ON local_translation_requests
                    WHEN NEW.status<>'RUNNING'
                      OR (SELECT producer_job_id FROM translation_runs WHERE id=NEW.run_id)<>
                         (SELECT job_id FROM job_attempts WHERE id=NEW.job_attempt_id)
                      OR (SELECT provider FROM translation_runs WHERE id=NEW.run_id)<>'local'
                      OR (SELECT COUNT(*) FROM translation_units WHERE run_id=NEW.run_id
                            AND position>=NEW.start_position AND position<NEW.start_position+NEW.unit_count)<>NEW.unit_count
                    BEGIN SELECT RAISE(ABORT,'invalid local translation request'); END""")
                connection.execute("""CREATE TRIGGER local_translation_request_identity_immutable
                    BEFORE UPDATE OF request_id,run_id,job_attempt_id,request_no,start_position,unit_count,started_at
                    ON local_translation_requests
                    BEGIN SELECT RAISE(ABORT,'local request identity is immutable'); END""")
                connection.execute("""CREATE TRIGGER local_translation_request_terminal_immutable
                    BEFORE UPDATE ON local_translation_requests WHEN OLD.status<>'RUNNING'
                    BEGIN SELECT RAISE(ABORT,'terminal local request is immutable'); END""")
                connection.execute("""CREATE TRIGGER local_translation_request_status_guard
                    BEFORE UPDATE OF status ON local_translation_requests
                    WHEN OLD.status<>'RUNNING' OR NEW.status='RUNNING'
                    BEGIN SELECT RAISE(ABORT,'invalid local request transition'); END""")
                connection.execute("""CREATE TRIGGER local_translation_final_text_immutable
                    BEFORE UPDATE OF final_text ON translation_units
                    WHEN OLD.final_text IS NOT NULL AND OLD.final_text IS NOT NEW.final_text
                    BEGIN SELECT RAISE(ABORT,'final translation text is immutable'); END""")

                connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(24,?)", (utc_now(),))
                fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                if fk_errors:
                    raise RuntimeError("Migration v24 failed foreign_key_check.")
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError("Migration v24 failed integrity_check.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA legacy_alter_table=OFF")
                connection.execute("PRAGMA foreign_keys=ON")

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
