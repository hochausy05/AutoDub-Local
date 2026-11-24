"""Editable translation drafts and immutable accepted translation snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from typing import Any, Iterator

from app.database import Database, IdempotencyConflict, utc_now
from app.translation import TranslationError, normalize_translation_unit


class TranslationRevisionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TranslationRevisionService:
    FINGERPRINT_VERSION = 1

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TranslationRevisionError(f"invalid_{field}") from exc
        if str(parsed) != value.lower():
            raise TranslationRevisionError(f"invalid_{field}")
        return str(parsed)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical(value).encode("utf-8", errors="strict")).hexdigest()

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not 16 <= len(value) <= 255 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
            raise TranslationRevisionError("invalid_idempotency_key")
        return value

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

    def _run(self, connection: sqlite3.Connection, project_id: str, run_id: str, *, require_current: bool) -> sqlite3.Row:
        row = connection.execute(
            """SELECT r.*,j.status AS job_status,j.input_fingerprint AS job_fingerprint,
                      p.active_artifact_id AS active_artifact_id,p.input_fingerprint AS active_input_fingerprint,
                      tr.revision_id AS active_transcript_revision,ps.media_asset_id AS selected_asset,
                      m.status AS media_status
               FROM translation_runs r JOIN jobs j ON j.id=r.producer_job_id AND j.project_id=r.project_id
               LEFT JOIN stage_pointers p ON p.project_id=r.project_id AND p.stage='translation'
               LEFT JOIN project_active_transcript_revisions tr ON tr.project_id=r.project_id
               LEFT JOIN project_processing_sources ps ON ps.project_id=r.project_id
               LEFT JOIN media_assets m ON m.project_id=r.project_id AND m.id=r.source_asset_id
               WHERE r.id=? AND r.project_id=?""", (run_id, project_id),
        ).fetchone()
        if row is None:
            raise TranslationRevisionError("translation_run_not_found")
        if row["status"] != "READY":
            raise TranslationRevisionError("translation_run_not_complete")
        if require_current and (row["job_status"] != "READY" or row["artifact_id"] is None
                or row["active_artifact_id"] != row["artifact_id"]
                or row["active_input_fingerprint"] != row["job_fingerprint"]
                or row["active_transcript_revision"] != row["transcript_revision_id"]
                or row["selected_asset"] != row["source_asset_id"] or row["media_status"] != "READY"):
            raise TranslationRevisionError("translation_run_stale")
        return row

    def _original_units(self, connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT unit_id,logical_unit_key,position,revision_segment_ids_json,source_segment_ids_json,
                      source_text,start_us,end_us,COALESCE(final_text,pass2_text) AS final_text
               FROM translation_units WHERE run_id=? ORDER BY position""", (run_id,),
        ).fetchall()
        units = []
        for row in rows:
            text = row["final_text"]
            if not isinstance(text, str) or not text.strip():
                raise TranslationRevisionError("translation_revision_coverage_invalid")
            units.append({"unit_id": row["unit_id"], "logical_unit_key": row["logical_unit_key"],
                "position": row["position"], "revision_segment_ids": json.loads(row["revision_segment_ids_json"]),
                "source_segment_ids": json.loads(row["source_segment_ids_json"]), "source_text": row["source_text"],
                "start_us": row["start_us"], "end_us": row["end_us"], "final_text": text,
                "text_origin": "PROVIDER_OUTPUT"})
        if not units or [unit["position"] for unit in units] != list(range(len(units))):
            raise TranslationRevisionError("translation_revision_coverage_invalid")
        seen_revision_segments: set[str] = set()
        seen_source_segments: set[str] = set()
        for unit in units:
            revision_ids, source_ids = unit["revision_segment_ids"], unit["source_segment_ids"]
            if (not revision_ids or not source_ids or len(revision_ids) != len(set(revision_ids))
                    or len(source_ids) != len(set(source_ids)) or seen_revision_segments.intersection(revision_ids)
                    or seen_source_segments.intersection(source_ids)):
                raise TranslationRevisionError("translation_revision_coverage_invalid")
            seen_revision_segments.update(revision_ids)
            seen_source_segments.update(source_ids)
            unit["unit_fingerprint"] = self._hash({"logical_unit_key": unit["logical_unit_key"], "text": unit["final_text"]})
        return units

    def _revision_units(self, connection: sqlite3.Connection, revision_id: str) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM translation_revision_units WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()
        return [{**dict(row), "revision_segment_ids": json.loads(row["revision_segment_ids_json"]),
                 "source_segment_ids": json.loads(row["source_segment_ids_json"])} for row in rows]

    def _fingerprints(self, units: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str]]]:
        unit_fingerprints = []
        for unit in units:
            fingerprint = self._hash({"logical_unit_key": unit["logical_unit_key"], "text": unit["final_text"]})
            unit_fingerprints.append((unit["unit_id"], fingerprint))
        overall = self._hash({"fingerprint_version": self.FINGERPRINT_VERSION,
            "units": [{"logical_unit_key": unit["logical_unit_key"], "text_fingerprint": fp}
                      for unit, (_unit_id, fp) in zip(units, unit_fingerprints, strict=True)]})
        return overall, unit_fingerprints

    def _detail(self, connection: sqlite3.Connection, revision_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT r.*,p.revision_id AS active_revision_id,p.generation AS active_generation,
                      tr.status AS run_status,tr.source_asset_id,tr.transcript_revision_id,tr.artifact_id AS run_artifact,
                      j.status AS producer_job_status,j.input_fingerprint AS job_fingerprint,
                      sp.active_artifact_id AS active_run_artifact,sp.input_fingerprint AS active_run_fingerprint,
                      at.revision_id AS active_transcript_revision,ps.media_asset_id AS selected_asset,m.status AS media_status
               FROM translation_revisions r JOIN translation_runs tr ON tr.project_id=r.project_id AND tr.id=r.run_id
               JOIN jobs j ON j.project_id=tr.project_id AND j.id=tr.producer_job_id
               LEFT JOIN project_active_translation_revisions p ON p.project_id=r.project_id
               LEFT JOIN stage_pointers sp ON sp.project_id=r.project_id AND sp.stage='translation'
               LEFT JOIN project_active_transcript_revisions at ON at.project_id=r.project_id
               LEFT JOIN project_processing_sources ps ON ps.project_id=r.project_id
               LEFT JOIN media_assets m ON m.project_id=r.project_id AND m.id=tr.source_asset_id
               WHERE r.id=?""", (revision_id,),
        ).fetchone()
        if row is None:
            raise TranslationRevisionError("translation_revision_not_found")
        value = dict(row)
        value["provenance"] = json.loads(value.pop("provenance_json"))
        value["units"] = self._revision_units(connection, revision_id)
        for unit in value["units"]:
            unit.pop("revision_segment_ids_json", None)
            unit.pop("source_segment_ids_json", None)
        value["is_active"] = (row["status"] == "ACCEPTED" and row["active_revision_id"] == revision_id
            and row["run_status"] == "READY" and row["producer_job_status"] == "READY"
            and row["active_run_artifact"] == row["run_artifact"] and row["active_run_fingerprint"] == row["job_fingerprint"]
            and row["active_transcript_revision"] == row["transcript_revision_id"]
            and row["selected_asset"] == row["source_asset_id"] and row["media_status"] == "READY")
        for key in ("active_revision_id", "active_generation", "active_run_artifact", "active_run_fingerprint", "run_artifact", "producer_job_status", "job_fingerprint", "active_transcript_revision", "selected_asset", "media_status"):
            value.pop(key, None)
        return value

    def editor_state(self, project_id: str, run_id: str) -> dict[str, Any]:
        project_id, run_id = self._uuid(project_id, "project_id"), self._uuid(run_id, "run_id")
        with closing(self.database.connect()) as connection:
            run = self._run(connection, project_id, run_id, require_current=False)
            active = connection.execute("SELECT revision_id,generation FROM project_active_translation_revisions WHERE project_id=?", (project_id,)).fetchone()
            original = self._original_units(connection, run_id)
            draft = connection.execute("SELECT id FROM translation_revisions WHERE project_id=? AND run_id=? AND status='DRAFT'", (project_id, run_id)).fetchone()
            accepted = connection.execute("SELECT id FROM translation_revisions WHERE project_id=? AND run_id=? AND status='ACCEPTED' ORDER BY revision_no DESC LIMIT 1", (project_id, run_id)).fetchone()
            accepted_for_run = self._detail(connection, accepted["id"]) if accepted else None
            state = {"project_id": project_id, "run_id": run_id, "source_asset_id": run["source_asset_id"],
                "transcript_revision_id": run["transcript_revision_id"], "run_is_current": self._is_current(run),
                "active_revision_id": active["revision_id"] if active else None, "active_generation": active["generation"] if active else 0,
                "original_units": original, "draft": self._detail(connection, draft["id"]) if draft else None,
                "accepted": accepted_for_run if accepted_for_run and accepted_for_run["is_active"] else None,
                "latest_accepted": accepted_for_run}
            return state

    @staticmethod
    def _is_current(run: sqlite3.Row) -> bool:
        return (run["job_status"] == "READY" and run["artifact_id"] is not None
            and run["active_artifact_id"] == run["artifact_id"]
            and run["active_input_fingerprint"] == run["job_fingerprint"]
            and run["active_transcript_revision"] == run["transcript_revision_id"]
            and run["selected_asset"] == run["source_asset_id"] and run["media_status"] == "READY")

    def _insert_revision(self, connection: sqlite3.Connection, *, project_id: str, run_id: str,
                         units: list[dict[str, Any]], status: str, source_kind: str,
                         source_revision_id: str | None, parent_revision_id: str | None,
                         provenance: dict[str, Any], revision_id: str | None = None) -> str:
        revision_id = revision_id or str(uuid.uuid4())
        fingerprint, unit_fingerprints = self._fingerprints(units)
        revision_no = connection.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM translation_revisions WHERE run_id=?", (run_id,)).fetchone()[0]
        stamp = utc_now()
        accepted_at = None
        connection.execute("""INSERT INTO translation_revisions(id,project_id,run_id,parent_revision_id,source_revision_id,
            source_kind,revision_no,status,lock_version,schema_version,fingerprint,fingerprint_version,provenance_json,created_at,accepted_at)
            VALUES(?,?,?,?,?,?,?,?,1,1,?,?,?,?,?)""", (revision_id, project_id, run_id, parent_revision_id, source_revision_id,
            source_kind, revision_no, "DRAFT", fingerprint, self.FINGERPRINT_VERSION, self._canonical(provenance), stamp, accepted_at))
        fp_by_id = dict(unit_fingerprints)
        for unit in units:
            connection.execute("""INSERT INTO translation_revision_units(project_id,revision_id,run_id,unit_id,logical_unit_key,
                position,revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us,final_text,unit_fingerprint,text_origin)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (project_id, revision_id, run_id, unit["unit_id"], unit["logical_unit_key"],
                unit["position"], self._canonical(unit["revision_segment_ids"]), self._canonical(unit["source_segment_ids"]),
                unit["source_text"], unit["start_us"], unit["end_us"], unit["final_text"], fp_by_id[unit["unit_id"]], unit["text_origin"]))
        if status == "ACCEPTED":
            connection.execute("UPDATE translation_revisions SET status='ACCEPTED',accepted_at=? WHERE id=?", (stamp, revision_id))
        return revision_id

    def create_draft(self, project_id: str, run_id: str, base_accepted_revision_id: str | None = None) -> dict[str, Any]:
        project_id, run_id = self._uuid(project_id, "project_id"), self._uuid(run_id, "run_id")
        if base_accepted_revision_id is not None:
            base_accepted_revision_id = self._uuid(base_accepted_revision_id, "translation_revision_id")
        with self._write() as connection:
            run = self._run(connection, project_id, run_id, require_current=True)
            existing = connection.execute("SELECT id FROM translation_revisions WHERE project_id=? AND run_id=? AND status='DRAFT'", (project_id, run_id)).fetchone()
            if existing:
                return self._detail(connection, existing["id"])
            if base_accepted_revision_id:
                base = connection.execute("""SELECT r.id,r.status,r.run_id FROM translation_revisions r
                    JOIN project_active_translation_revisions p ON p.project_id=r.project_id AND p.revision_id=r.id
                    WHERE r.id=? AND r.project_id=?""", (base_accepted_revision_id, project_id)).fetchone()
                if base is None or base["status"] != "ACCEPTED" or base["run_id"] != run_id:
                    raise TranslationRevisionError("translation_revision_not_found")
                units = self._revision_units(connection, base_accepted_revision_id)
                source_kind, parent_id = "ACCEPTED_REVISION", base_accepted_revision_id
            else:
                units = self._original_units(connection, run_id)
                prior = connection.execute("""SELECT r.id FROM project_active_translation_revisions p JOIN translation_revisions r
                    ON r.project_id=p.project_id AND r.id=p.revision_id WHERE p.project_id=? AND r.run_id=? AND r.status='ACCEPTED'""", (project_id, run_id)).fetchone()
                source_kind, parent_id = "PROVIDER_OUTPUT", prior["id"] if prior else None
            if not units:
                raise TranslationRevisionError("translation_revision_coverage_invalid")
            source_revision_id = base_accepted_revision_id
            revision_id = self._insert_revision(connection, project_id=project_id, run_id=run_id, units=units,
                status="DRAFT", source_kind=source_kind, source_revision_id=source_revision_id,
                parent_revision_id=parent_id, provenance={"source_kind": source_kind, "translation_run_id": run_id,
                    "translation_artifact_id": run["artifact_id"], "input_fingerprint": run["input_fingerprint"]})
            return self._detail(connection, revision_id)

    def save_draft(self, project_id: str, run_id: str, revision_id: str, expected_lock_version: int,
                   edits: list[dict[str, Any]], idempotency_key: str) -> dict[str, Any]:
        project_id, run_id = self._uuid(project_id, "project_id"), self._uuid(run_id, "run_id")
        revision_id = self._uuid(revision_id, "translation_revision_id")
        key = self._key(idempotency_key)
        if type(expected_lock_version) is not int or expected_lock_version < 1:
            raise TranslationRevisionError("invalid_translation_revision_version")
        if not isinstance(edits, list) or len(edits) > 10000:
            raise TranslationRevisionError("invalid_translation_edits")
        if any(not isinstance(edit, dict) or set(edit) != {"unit_id", "text"} for edit in edits):
            raise TranslationRevisionError("invalid_translation_edit")
        digest = self._hash({"run_id": run_id, "revision_id": revision_id, "expected_lock_version": expected_lock_version, "edits": edits})
        scope = f"translation:save:{revision_id}"
        with self._write() as connection:
            prior = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
            if prior:
                if prior["request_digest"] != digest:
                    raise IdempotencyConflict
                return self._detail(connection, json.loads(prior["response_json"])["revision_id"])
            self._run(connection, project_id, run_id, require_current=True)
            revision = connection.execute("SELECT * FROM translation_revisions WHERE id=? AND project_id=? AND run_id=?", (revision_id, project_id, run_id)).fetchone()
            if revision is None:
                raise TranslationRevisionError("translation_revision_not_found")
            if revision["status"] != "DRAFT":
                raise TranslationRevisionError("translation_revision_immutable")
            if revision["lock_version"] != expected_lock_version:
                raise TranslationRevisionError("translation_revision_conflict")
            seen: set[str] = set()
            normalized: dict[str, str] = {}
            for edit in edits:
                unit_id = self._uuid(edit["unit_id"], "translation_unit_id")
                if unit_id in seen:
                    raise TranslationRevisionError("duplicate_translation_unit")
                seen.add(unit_id)
                if not isinstance(edit["text"], str) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in edit["text"]):
                    raise TranslationRevisionError("invalid_translation_text")
                try:
                    # Editor saves trim surrounding whitespace without changing the
                    # shared provider-output punctuation normalizer contract.
                    text = normalize_translation_unit(edit["text"].strip())
                except TranslationError as exc:
                    raise TranslationRevisionError(exc.code) from exc
                if len(text) > 200000:
                    raise TranslationRevisionError("translation_text_too_large")
                normalized[unit_id] = text
            current = {row["unit_id"]: row for row in connection.execute("SELECT unit_id,final_text,text_origin FROM translation_revision_units WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()}
            unknown = set(normalized) - set(current)
            if unknown:
                raise TranslationRevisionError("translation_unit_not_found")
            changes = [(unit_id, text) for unit_id, text in normalized.items() if current[unit_id]["final_text"] != text]
            if changes:
                for unit_id, text in changes:
                    fingerprint = self._hash({"logical_unit_key": connection.execute("SELECT logical_unit_key FROM translation_revision_units WHERE revision_id=? AND unit_id=?", (revision_id, unit_id)).fetchone()[0], "text": text})
                    connection.execute("UPDATE translation_revision_units SET final_text=?,unit_fingerprint=?,text_origin='USER_EDIT' WHERE revision_id=? AND unit_id=?", (text, fingerprint, revision_id, unit_id))
                overall_units = self._revision_units(connection, revision_id)
                fingerprint, _ = self._fingerprints(overall_units)
                changed = connection.execute("UPDATE translation_revisions SET lock_version=lock_version+1,fingerprint=? WHERE id=? AND status='DRAFT' AND lock_version=?", (fingerprint, revision_id, expected_lock_version)).rowcount
                if changed != 1:
                    raise TranslationRevisionError("translation_revision_conflict")
            response_json = self._canonical({"revision_id": revision_id})
            connection.execute("INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)", (scope, key, digest, project_id, response_json, utc_now()))
            return self._detail(connection, revision_id)

    def _validate_complete_revision(self, connection: sqlite3.Connection, revision_id: str, run_id: str) -> list[dict[str, Any]]:
        units = self._revision_units(connection, revision_id)
        original = self._original_units(connection, run_id)
        if len(units) != len(original):
            raise TranslationRevisionError("translation_revision_coverage_invalid")
        for saved, source in zip(units, original, strict=True):
            if any(saved[key] != source[key] for key in ("unit_id", "logical_unit_key", "position", "revision_segment_ids", "source_segment_ids", "source_text", "start_us", "end_us")):
                raise TranslationRevisionError("translation_revision_coverage_invalid")
            if not isinstance(saved["final_text"], str) or not saved["final_text"].strip():
                raise TranslationRevisionError("invalid_translation_text")
        seen_revision_segments: set[str] = set()
        seen_source_segments: set[str] = set()
        for unit in units:
            revision_ids, source_ids = unit["revision_segment_ids"], unit["source_segment_ids"]
            if (not revision_ids or not source_ids or len(revision_ids) != len(set(revision_ids))
                    or len(source_ids) != len(set(source_ids)) or seen_revision_segments.intersection(revision_ids)
                    or seen_source_segments.intersection(source_ids)):
                raise TranslationRevisionError("translation_revision_coverage_invalid")
            seen_revision_segments.update(revision_ids)
            seen_source_segments.update(source_ids)
        return units

    def _activate(self, connection: sqlite3.Connection, project_id: str, run_id: str, revision_id: str,
                  expected_active_revision_id: str | None, units: list[dict[str, Any]]) -> dict[str, Any]:
        if expected_active_revision_id is not None:
            expected_active_revision_id = self._uuid(expected_active_revision_id, "active_translation_revision_id")
        pointer = connection.execute("SELECT revision_id,generation FROM project_active_translation_revisions WHERE project_id=?", (project_id,)).fetchone()
        actual = pointer["revision_id"] if pointer else None
        if actual != expected_active_revision_id:
            raise TranslationRevisionError("translation_active_revision_conflict")
        run = self._run(connection, project_id, run_id, require_current=True)
        stamp = utc_now()
        fingerprint, _ = self._fingerprints(units)
        if pointer:
            changed = connection.execute("UPDATE project_active_translation_revisions SET revision_id=?,generation=generation+1,updated_at=? WHERE project_id=? AND revision_id=? AND generation=?", (revision_id, stamp, project_id, actual, pointer["generation"])).rowcount
            if changed != 1:
                raise TranslationRevisionError("translation_active_revision_conflict")
        else:
            connection.execute("INSERT INTO project_active_translation_revisions(project_id,revision_id,generation,updated_at) VALUES(?,?,1,?)", (project_id, revision_id, stamp))
        previous_fp = None
        if actual:
            row = connection.execute("SELECT fingerprint FROM translation_revisions WHERE id=?", (actual,)).fetchone()
            previous_fp = row["fingerprint"] if row else None
        if previous_fp != fingerprint:
            dependency = self._canonical({"schema_version": 1, "translation_revision_fingerprint": fingerprint})
            dependency_fp = hashlib.sha256(dependency.encode("utf-8")).hexdigest()
            for stage in ("tts", "sync", "render"):
                existing = connection.execute("SELECT input_fingerprint FROM stage_pointers WHERE project_id=? AND stage=?", (project_id, stage)).fetchone()
                if existing and existing["input_fingerprint"] != dependency_fp:
                    connection.execute("UPDATE stage_pointers SET input_fingerprint=?,input_generation=input_generation+1,updated_at=? WHERE project_id=? AND stage=?", (dependency_fp, stamp, project_id, stage))
        return self._detail(connection, revision_id)

    def accept_original(self, project_id: str, run_id: str, expected_active_revision_id: str | None,
                        idempotency_key: str) -> dict[str, Any]:
        project_id, run_id = self._uuid(project_id, "project_id"), self._uuid(run_id, "run_id")
        key = self._key(idempotency_key)
        digest = self._hash({"run_id": run_id, "expected_active_revision_id": expected_active_revision_id, "source": "PROVIDER_OUTPUT"})
        scope = f"translation:accept:{project_id}"
        with self._write() as connection:
            prior = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
            if prior:
                if prior["request_digest"] != digest:
                    raise IdempotencyConflict
                return self._detail(connection, json.loads(prior["response_json"])["revision_id"])
            run = self._run(connection, project_id, run_id, require_current=True)
            units = self._original_units(connection, run_id)
            prior = connection.execute("""SELECT r.id FROM project_active_translation_revisions p JOIN translation_revisions r
                ON r.project_id=p.project_id AND r.id=p.revision_id WHERE p.project_id=? AND r.run_id=? AND r.status='ACCEPTED'""", (project_id, run_id)).fetchone()
            revision_id = self._insert_revision(connection, project_id=project_id, run_id=run_id, units=units,
                status="ACCEPTED", source_kind="PROVIDER_OUTPUT", source_revision_id=None, parent_revision_id=prior["id"] if prior else None,
                provenance={"source_kind": "PROVIDER_OUTPUT", "translation_run_id": run_id,
                    "translation_artifact_id": run["artifact_id"], "input_fingerprint": run["input_fingerprint"]})
            result = self._activate(connection, project_id, run_id, revision_id, expected_active_revision_id, units)
            connection.execute("INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)", (scope, key, digest, project_id, self._canonical({"revision_id": revision_id}), utc_now()))
            return result

    def accept_draft(self, project_id: str, run_id: str, revision_id: str, expected_lock_version: int,
                     expected_active_revision_id: str | None, idempotency_key: str) -> dict[str, Any]:
        project_id, run_id = self._uuid(project_id, "project_id"), self._uuid(run_id, "run_id")
        revision_id = self._uuid(revision_id, "translation_revision_id")
        key = self._key(idempotency_key)
        digest = self._hash({"run_id": run_id, "revision_id": revision_id, "expected_lock_version": expected_lock_version,
            "expected_active_revision_id": expected_active_revision_id})
        scope = f"translation:accept:{project_id}"
        with self._write() as connection:
            prior = connection.execute("SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?", (scope, key)).fetchone()
            if prior:
                if prior["request_digest"] != digest:
                    raise IdempotencyConflict
                return self._detail(connection, json.loads(prior["response_json"])["revision_id"])
            self._run(connection, project_id, run_id, require_current=True)
            revision = connection.execute("SELECT * FROM translation_revisions WHERE id=? AND project_id=? AND run_id=?", (revision_id, project_id, run_id)).fetchone()
            if revision is None:
                raise TranslationRevisionError("translation_revision_not_found")
            if revision["status"] != "DRAFT":
                raise TranslationRevisionError("translation_revision_immutable")
            if revision["lock_version"] != expected_lock_version:
                raise TranslationRevisionError("translation_revision_conflict")
            units = self._validate_complete_revision(connection, revision_id, run_id)
            run = connection.execute("SELECT artifact_id,input_fingerprint FROM translation_runs WHERE id=?", (run_id,)).fetchone()
            stamp = utc_now()
            fingerprint, _ = self._fingerprints(units)
            changed = connection.execute("UPDATE translation_revisions SET status='ACCEPTED',accepted_at=?,lock_version=lock_version+1,fingerprint=?,provenance_json=? WHERE id=? AND status='DRAFT' AND lock_version=?", (stamp, fingerprint, self._canonical({"source_kind": revision["source_kind"], "source_revision_id": revision["source_revision_id"], "translation_run_id": run_id, "translation_artifact_id": run["artifact_id"], "input_fingerprint": run["input_fingerprint"]}), revision_id, expected_lock_version)).rowcount
            if changed != 1:
                raise TranslationRevisionError("translation_revision_conflict")
            result = self._activate(connection, project_id, run_id, revision_id, expected_active_revision_id, units)
            connection.execute("INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)", (scope, key, digest, project_id, self._canonical({"revision_id": revision_id}), utc_now()))
            return result
