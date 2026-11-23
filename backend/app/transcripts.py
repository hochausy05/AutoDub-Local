"""Editable transcript revisions with immutable ASR provenance and coverage checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import uuid
from contextlib import closing, contextmanager
from typing import Any, Iterator

from app.database import Database, IdempotencyConflict, utc_now


class TranscriptError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TranscriptService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TranscriptError(f"invalid_{field}") from exc
        if str(parsed) != value.lower():
            raise TranscriptError(f"invalid_{field}")
        return str(parsed)

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not 16 <= len(value) <= 255 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
            raise TranscriptError("invalid_idempotency_key")
        return value

    @staticmethod
    def _digest(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8", errors="strict")).hexdigest()

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

    def _replay(self, connection: sqlite3.Connection, scope: str, key: str, digest: str) -> dict[str, Any] | None:
        prior = connection.execute(
            "SELECT request_digest,response_json FROM idempotency_records WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if prior is None:
            return None
        if prior["request_digest"] != digest:
            raise IdempotencyConflict
        return self._detail(connection, json.loads(prior["response_json"])["revision_id"])

    def _record(self, connection: sqlite3.Connection, scope: str, key: str, digest: str, project_id: str, revision_id: str) -> None:
        connection.execute(
            "INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES(?,?,?,?,?,?)",
            (scope, key, digest, project_id, json.dumps({"revision_id": revision_id}), utc_now()),
        )

    def _detail(self, connection: sqlite3.Connection, revision_id: str) -> dict[str, Any]:
        revision = connection.execute(
            """SELECT r.*,t.source_asset_id,t.artifact_id,t.status AS run_status,t.duration_us,
                      (a.revision_id=r.id) AS is_active
               FROM transcript_revisions r JOIN transcription_runs t
                 ON t.project_id=r.project_id AND t.id=r.run_id
               LEFT JOIN project_active_transcript_revisions a ON a.project_id=r.project_id
               WHERE r.id=?""", (revision_id,),
        ).fetchone()
        if revision is None:
            raise TranscriptError("transcript_revision_not_found")
        rows = connection.execute(
            """SELECT s.row_id,s.stable_segment_key,s.position,s.text,s.start_us,s.end_us,
                      m.group_id,m.ordinal AS member_ordinal,g.review_status,g.disposition,g.skip_reason
               FROM revision_segments s LEFT JOIN coverage_group_members m
                 ON m.project_id=s.project_id AND m.revision_id=s.revision_id AND m.row_id=s.row_id
               LEFT JOIN coverage_groups g ON g.project_id=m.project_id AND g.revision_id=m.revision_id AND g.group_id=m.group_id
               WHERE s.revision_id=? ORDER BY s.position""", (revision_id,),
        ).fetchall()
        previous_end = None
        segments = []
        for row in rows:
            parents = connection.execute(
                "SELECT parent_stable_segment_key FROM revision_segment_parents WHERE child_revision_id=? AND child_row_id=? ORDER BY ordinal",
                (revision_id, row["row_id"]),
            ).fetchall()
            sources = connection.execute(
                """SELECT s.id,s.source_text FROM coverage_group_sources c JOIN segments s ON s.id=c.source_id
                   WHERE c.revision_id=? AND c.group_id=? ORDER BY c.ordinal""",
                (revision_id, row["group_id"]),
            ).fetchall() if row["group_id"] else []
            segments.append({
                "row_id": row["row_id"], "stable_segment_key": row["stable_segment_key"],
                "position": row["position"], "text": row["text"], "start_us": row["start_us"],
                "end_us": row["end_us"], "parent_keys": [parent[0] for parent in parents],
                "coverage_group_id": row["group_id"], "coverage_status": row["review_status"],
                "source_segment_ids": [source["id"] for source in sources],
                "source_texts": [source["source_text"] for source in sources],
                "timing_warning": previous_end is not None and row["start_us"] < previous_end,
            })
            previous_end = row["end_us"]
        active = connection.execute(
            "SELECT revision_id,generation FROM project_active_transcript_revisions WHERE project_id=?",
            (revision["project_id"],),
        ).fetchone()
        current_source = connection.execute("SELECT media_asset_id FROM project_processing_sources WHERE project_id=?", (revision["project_id"],)).fetchone()
        active_asr = connection.execute("SELECT active_artifact_id FROM stage_pointers WHERE project_id=? AND stage='asr'", (revision["project_id"],)).fetchone()
        is_active = bool(active and active["revision_id"] == revision["id"])
        is_current = bool(is_active and current_source and current_source["media_asset_id"] == revision["source_asset_id"] and active_asr and active_asr["active_artifact_id"] == revision["artifact_id"])
        return {
            "id": revision["id"], "project_id": revision["project_id"], "run_id": revision["run_id"],
            "source_asset_id": revision["source_asset_id"], "parent_revision_id": revision["parent_revision_id"],
            "revision_no": revision["revision_no"], "status": revision["status"],
            "lock_version": revision["lock_version"], "schema_version": revision["schema_version"],
            "created_at": revision["created_at"], "accepted_at": revision["accepted_at"],
            "is_active": is_active, "is_current": is_current,
            "active_generation": active["generation"] if active else 0,
            "run_status": revision["run_status"], "duration_us": revision["duration_us"],
            "segments": segments,
        }

    def list_revisions(self, project_id: str, source_asset_id: str) -> list[dict[str, Any]]:
        project_id = self._uuid(project_id, "project_id")
        source_asset_id = self._uuid(source_asset_id, "asset_id")
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """SELECT r.id FROM transcript_revisions r JOIN transcription_runs t
                   ON t.project_id=r.project_id AND t.id=r.run_id
                   WHERE r.project_id=? AND t.source_asset_id=? ORDER BY r.revision_no DESC,r.created_at DESC""",
                (project_id, source_asset_id),
            ).fetchall()
            return [self._detail(connection, row["id"]) for row in rows]

    def get_revision(self, project_id: str, revision_id: str) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project_id")
        revision_id = self._uuid(revision_id, "revision_id")
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT project_id FROM transcript_revisions WHERE id=?", (revision_id,)).fetchone()
            if row is None or row["project_id"] != project_id:
                raise TranscriptError("transcript_revision_not_found")
            return self._detail(connection, revision_id)

    def create_draft(self, project_id: str, *, source_run_id: str | None, parent_revision_id: str | None, idempotency_key: str) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project_id")
        if (source_run_id is None) == (parent_revision_id is None):
            raise TranscriptError("transcript_base_required")
        source_run_id = self._uuid(source_run_id, "run_id") if source_run_id else None
        parent_revision_id = self._uuid(parent_revision_id, "revision_id") if parent_revision_id else None
        key = self._key(idempotency_key)
        digest = self._digest({"project_id": project_id, "source_run_id": source_run_id, "parent_revision_id": parent_revision_id})
        scope = f"transcript:create:{project_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            if parent_revision_id:
                parent = connection.execute(
                    "SELECT project_id,run_id,status FROM transcript_revisions WHERE id=?", (parent_revision_id,),
                ).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise TranscriptError("transcript_revision_not_found")
                if parent["status"] != "ACCEPTED":
                    raise TranscriptError("transcript_parent_not_accepted")
                source_run_id = parent["run_id"]
                base_id = parent_revision_id
            else:
                run = connection.execute(
                    "SELECT project_id,status FROM transcription_runs WHERE id=?", (source_run_id,),
                ).fetchone()
                if run is None or run["project_id"] != project_id:
                    raise TranscriptError("transcription_run_not_found")
                if run["status"] not in {"READY", "REVIEW_REQUIRED"}:
                    raise TranscriptError("transcript_run_not_ready")
                latest_accepted = connection.execute(
                    "SELECT id FROM transcript_revisions WHERE project_id=? AND run_id=? AND status='ACCEPTED' ORDER BY revision_no DESC LIMIT 1",
                    (project_id, source_run_id),
                ).fetchone()
                base_id = latest_accepted["id"] if latest_accepted else None
            existing = connection.execute(
                "SELECT id FROM transcript_revisions WHERE project_id=? AND run_id=? AND status='DRAFT'", (project_id, source_run_id),
            ).fetchone()
            if existing:
                revision_id = existing["id"]
                self._record(connection, scope, key, digest, project_id, revision_id)
                return self._detail(connection, revision_id)
            run = connection.execute("SELECT status FROM transcription_runs WHERE id=? AND project_id=?", (source_run_id, project_id)).fetchone()
            if run is None or run["status"] not in {"READY", "REVIEW_REQUIRED"}:
                raise TranscriptError("transcript_run_not_ready")
            source_rows = connection.execute("SELECT id,position,start_us,end_us,source_text FROM segments WHERE run_id=? ORDER BY position", (source_run_id,)).fetchall()
            if not source_rows:
                raise TranscriptError("transcript_empty")
            max_no = connection.execute("SELECT COALESCE(MAX(revision_no),0) FROM transcript_revisions WHERE run_id=?", (source_run_id,)).fetchone()[0]
            revision_id, stamp = str(uuid.uuid4()), utc_now()
            connection.execute(
                "INSERT INTO transcript_revisions(id,project_id,run_id,parent_revision_id,revision_no,status,created_at) VALUES(?,?,?,?,?,'DRAFT',?)",
                (revision_id, project_id, source_run_id, base_id, max_no + 1, stamp),
            )
            if base_id:
                base_rows = connection.execute("SELECT * FROM revision_segments WHERE revision_id=? ORDER BY position", (base_id,)).fetchall()
                for row in base_rows:
                    row_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO revision_segments(row_id,project_id,revision_id,stable_segment_key,position,text,start_us,end_us) VALUES(?,?,?,?,?,?,?,?)",
                        (row_id, project_id, revision_id, row["stable_segment_key"], row["position"], row["text"], row["start_us"], row["end_us"]),
                    )
                    connection.execute(
                        "INSERT INTO revision_segment_parents(project_id,child_revision_id,child_row_id,parent_revision_id,parent_stable_segment_key,ordinal) VALUES(?,?,?,?,?,0)",
                        (project_id, revision_id, row_id, base_id, row["stable_segment_key"]),
                    )
                groups = connection.execute("SELECT * FROM coverage_groups WHERE revision_id=? ORDER BY group_id", (base_id,)).fetchall()
                row_map = {row["stable_segment_key"]: row["row_id"] for row in connection.execute("SELECT row_id,stable_segment_key FROM revision_segments WHERE revision_id=?", (revision_id,))}
                for group in groups:
                    group_id = str(uuid.uuid4())
                    connection.execute("INSERT INTO coverage_groups(group_id,project_id,revision_id,review_status,disposition,skip_reason) VALUES(?,?,?,? ,?,?)", (group_id, project_id, revision_id, group["review_status"], group["disposition"], group["skip_reason"]))
                    for source in connection.execute("SELECT source_id,ordinal FROM coverage_group_sources WHERE revision_id=? AND group_id=? ORDER BY ordinal", (base_id, group["group_id"])):
                        connection.execute("INSERT INTO coverage_group_sources(project_id,revision_id,group_id,source_id,ordinal) VALUES(?,?,?,?,?)", (project_id, revision_id, group_id, source["source_id"], source["ordinal"]))
                    for member in connection.execute("SELECT rs.stable_segment_key,m.ordinal FROM coverage_group_members m JOIN revision_segments rs ON rs.row_id=m.row_id AND rs.revision_id=m.revision_id WHERE m.revision_id=? AND m.group_id=? ORDER BY m.ordinal", (base_id, group["group_id"])):
                        connection.execute("INSERT INTO coverage_group_members(project_id,revision_id,group_id,row_id,ordinal) VALUES(?,?,?,?,?)", (project_id, revision_id, group_id, row_map[member["stable_segment_key"]], member["ordinal"]))
            else:
                for source in source_rows:
                    row_id, stable_key, group_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
                    connection.execute("INSERT INTO revision_segments(row_id,project_id,revision_id,stable_segment_key,position,text,start_us,end_us) VALUES(?,?,?,?,?,?,?,?)", (row_id, project_id, revision_id, stable_key, source["position"], source["source_text"], source["start_us"], source["end_us"]))
                    connection.execute("INSERT INTO coverage_groups(group_id,project_id,revision_id,review_status,disposition) VALUES(?,?,?,'REVIEWED','INCLUDED')", (group_id, project_id, revision_id))
                    connection.execute("INSERT INTO coverage_group_sources(project_id,revision_id,group_id,source_id,ordinal) VALUES(?,?,?,?,0)", (project_id, revision_id, group_id, source["id"]))
                    connection.execute("INSERT INTO coverage_group_members(project_id,revision_id,group_id,row_id,ordinal) VALUES(?,?,?, ?,0)", (project_id, revision_id, group_id, row_id))
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)

    @staticmethod
    def _check_lock(connection: sqlite3.Connection, project_id: str, revision_id: str, expected: int) -> sqlite3.Row:
        if type(expected) is not int or expected < 1:
            raise TranscriptError("invalid_lock_version")
        row = connection.execute("SELECT r.*,t.duration_us,t.source_asset_id FROM transcript_revisions r JOIN transcription_runs t ON t.project_id=r.project_id AND t.id=r.run_id WHERE r.id=? AND r.project_id=?", (revision_id, project_id)).fetchone()
        if row is None:
            raise TranscriptError("transcript_revision_not_found")
        if row["status"] != "DRAFT":
            raise TranscriptError("transcript_revision_immutable")
        if row["lock_version"] != expected:
            raise TranscriptError("transcript_revision_conflict")
        return row

    def save_edits(self, project_id: str, revision_id: str, expected_lock_version: int, edits: list[dict[str, Any]], idempotency_key: str) -> dict[str, Any]:
        project_id, revision_id = self._uuid(project_id, "project_id"), self._uuid(revision_id, "revision_id")
        key = self._key(idempotency_key)
        if not isinstance(edits, list) or not edits or len(edits) > 10000:
            raise TranscriptError("invalid_transcript_edits")
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("text"), str) and any(0xD800 <= ord(ch) <= 0xDFFF for ch in edit["text"]):
                raise TranscriptError("invalid_transcript_text")
        digest = self._digest({"revision_id": revision_id, "expected_lock_version": expected_lock_version, "edits": edits})
        scope = f"transcript:save:{revision_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            revision = self._check_lock(connection, project_id, revision_id, expected_lock_version)
            seen: set[str] = set()
            changes: list[tuple[str, int, int, str]] = []
            for edit in edits:
                if not isinstance(edit, dict) or set(edit) - {"row_id", "text", "start_us", "end_us"} or "row_id" not in edit:
                    raise TranscriptError("invalid_transcript_edit")
                row_id = self._uuid(edit["row_id"], "row_id")
                if row_id in seen:
                    raise TranscriptError("duplicate_transcript_edit")
                seen.add(row_id)
                row = connection.execute("SELECT * FROM revision_segments WHERE row_id=? AND revision_id=? AND project_id=?", (row_id, revision_id, project_id)).fetchone()
                if row is None:
                    raise TranscriptError("transcript_segment_not_found")
                text = edit.get("text", row["text"])
                start_us, end_us = edit.get("start_us", row["start_us"]), edit.get("end_us", row["end_us"])
                if not isinstance(text, str) or not text.strip() or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                    raise TranscriptError("invalid_transcript_text")
                if type(start_us) is not int or type(end_us) is not int or not 0 <= start_us < end_us <= revision["duration_us"]:
                    raise TranscriptError("invalid_transcript_timing")
                if text != row["text"] or start_us != row["start_us"] or end_us != row["end_us"]:
                    changes.append((text, start_us, end_us, row_id))
            for text, start_us, end_us, row_id in changes:
                connection.execute("UPDATE revision_segments SET text=?,start_us=?,end_us=? WHERE row_id=? AND revision_id=?", (text, start_us, end_us, row_id, revision_id))
            if changes:
                changed = connection.execute("UPDATE transcript_revisions SET lock_version=lock_version+1 WHERE id=? AND lock_version=? AND status='DRAFT'", (revision_id, expected_lock_version)).rowcount
                if changed != 1:
                    raise TranscriptError("transcript_revision_conflict")
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)

    @staticmethod
    def _split_boundary_safe(text: str, index: int) -> bool:
        if not 0 < index < len(text) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
            return False
        before, after = text[index - 1], text[index]
        if unicodedata.combining(after) or after == "\u200d" or before == "\u200d":
            return False
        if 0xFE00 <= ord(after) <= 0xFE0F or 0x1F3FB <= ord(after) <= 0x1F3FF:
            return False
        if 0x1F1E6 <= ord(before) <= 0x1F1FF and 0x1F1E6 <= ord(after) <= 0x1F1FF:
            count = 0
            cursor = index - 1
            while cursor >= 0 and 0x1F1E6 <= ord(text[cursor]) <= 0x1F1FF:
                count += 1
                cursor -= 1
            if count % 2 == 1:
                return False
        return True

    @staticmethod
    def _shift_positions(connection: sqlite3.Connection, revision_id: str, rows: list[sqlite3.Row], removed_ids: set[str], inserted: list[tuple[str, str, str, int, str, int, int]], shift_after: int, position_delta: int) -> None:
        connection.execute("UPDATE revision_segments SET position=position+1000000 WHERE revision_id=?", (revision_id,))
        for row_id in removed_ids:
            connection.execute("DELETE FROM revision_segment_parents WHERE child_revision_id=? AND child_row_id=?", (revision_id, row_id))
            connection.execute("DELETE FROM revision_segments WHERE row_id=?", (row_id,))
        for row_id, stable_key, project_id, position, text, start_us, end_us in inserted:
            connection.execute("INSERT INTO revision_segments(row_id,project_id,revision_id,stable_segment_key,position,text,start_us,end_us) VALUES(?,?,?,?,?,?,?,?)", (row_id, project_id, revision_id, stable_key, 2_000_000 + position, text, start_us, end_us))
        for row in rows:
            if row["row_id"] in removed_ids:
                continue
            new_position = row["position"] + (position_delta if row["position"] >= shift_after else 0)
            connection.execute("UPDATE revision_segments SET position=? WHERE row_id=?", (new_position, row["row_id"]))
        for row_id, _stable_key, _project_id, position, _text, _start, _end in inserted:
            connection.execute("UPDATE revision_segments SET position=? WHERE row_id=?", (position, row_id))

    def split_segment(self, project_id: str, revision_id: str, row_id: str, expected_lock_version: int, split_at_codepoint: int, split_at_us: int, idempotency_key: str) -> dict[str, Any]:
        project_id, revision_id, row_id = self._uuid(project_id, "project_id"), self._uuid(revision_id, "revision_id"), self._uuid(row_id, "row_id")
        key = self._key(idempotency_key)
        digest = self._digest({"revision_id": revision_id, "row_id": row_id, "expected_lock_version": expected_lock_version, "split_at_codepoint": split_at_codepoint, "split_at_us": split_at_us})
        scope = f"transcript:split:{revision_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            self._check_lock(connection, project_id, revision_id, expected_lock_version)
            rows = connection.execute("SELECT * FROM revision_segments WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()
            source = next((row for row in rows if row["row_id"] == row_id), None)
            if source is None:
                raise TranscriptError("transcript_segment_not_found")
            text = source["text"]
            if type(split_at_codepoint) is not int or not self._split_boundary_safe(text, split_at_codepoint):
                raise TranscriptError("unsafe_unicode_split")
            if type(split_at_us) is not int or not source["start_us"] < split_at_us < source["end_us"]:
                raise TranscriptError("invalid_split_timing")
            members = connection.execute("SELECT * FROM coverage_group_members WHERE revision_id=? AND row_id=?", (revision_id, row_id)).fetchone()
            if members is None:
                raise TranscriptError("transcript_coverage_invalid")
            group_id, ordinal, position = members["group_id"], members["ordinal"], source["position"]
            connection.execute("UPDATE coverage_group_members SET ordinal=ordinal+1000000 WHERE revision_id=? AND group_id=?", (revision_id, group_id))
            connection.execute("DELETE FROM coverage_group_members WHERE revision_id=? AND row_id=?", (revision_id, row_id))
            for member in connection.execute("SELECT row_id,ordinal FROM coverage_group_members WHERE revision_id=? AND group_id=?", (revision_id, group_id)).fetchall():
                old_ord = member["ordinal"] - 1_000_000
                connection.execute("UPDATE coverage_group_members SET ordinal=? WHERE row_id=? AND revision_id=?", (old_ord + (1 if old_ord > ordinal else 0), member["row_id"], revision_id))
            first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
            first_key, second_key = str(uuid.uuid4()), str(uuid.uuid4())
            codepoints = list(text)
            inserts = [
                (first_id, first_key, project_id, position, "".join(codepoints[:split_at_codepoint]), source["start_us"], split_at_us),
                (second_id, second_key, project_id, position + 1, "".join(codepoints[split_at_codepoint:]), split_at_us, source["end_us"]),
            ]
            self._shift_positions(connection, revision_id, rows, {row_id}, inserts, position, 1)
            for child_id, child_key, _p, _pos, _text, _start, _end in inserts:
                connection.execute("INSERT INTO revision_segment_parents(project_id,child_revision_id,child_row_id,parent_revision_id,parent_stable_segment_key,ordinal) VALUES(?,?,?,?,?,0)", (project_id, revision_id, child_id, revision_id, source["stable_segment_key"]))
                connection.execute("INSERT INTO coverage_group_members(project_id,revision_id,group_id,row_id,ordinal) VALUES(?,?,?,?,?)", (project_id, revision_id, group_id, child_id, ordinal + (0 if child_id == first_id else 1)))
            connection.execute("UPDATE coverage_groups SET review_status='PENDING' WHERE revision_id=? AND group_id=?", (revision_id, group_id))
            connection.execute("UPDATE transcript_revisions SET lock_version=lock_version+1 WHERE id=? AND lock_version=?", (revision_id, expected_lock_version))
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)

    def merge_next(self, project_id: str, revision_id: str, row_id: str, expected_lock_version: int, merged_text: str, idempotency_key: str) -> dict[str, Any]:
        project_id, revision_id, row_id = self._uuid(project_id, "project_id"), self._uuid(revision_id, "revision_id"), self._uuid(row_id, "row_id")
        key = self._key(idempotency_key)
        if not isinstance(merged_text, str) or not merged_text.strip() or any(0xD800 <= ord(ch) <= 0xDFFF for ch in merged_text):
            raise TranscriptError("invalid_transcript_text")
        digest = self._digest({"revision_id": revision_id, "row_id": row_id, "expected_lock_version": expected_lock_version, "merged_text": merged_text})
        scope = f"transcript:merge:{revision_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            self._check_lock(connection, project_id, revision_id, expected_lock_version)
            rows = connection.execute("SELECT * FROM revision_segments WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()
            left_index = next((index for index, row in enumerate(rows) if row["row_id"] == row_id), -1)
            if left_index < 0 or left_index + 1 >= len(rows):
                raise TranscriptError("transcript_next_segment_missing")
            left, right = rows[left_index], rows[left_index + 1]
            left_member = connection.execute("SELECT * FROM coverage_group_members WHERE revision_id=? AND row_id=?", (revision_id, left["row_id"])).fetchone()
            right_member = connection.execute("SELECT * FROM coverage_group_members WHERE revision_id=? AND row_id=?", (revision_id, right["row_id"])).fetchone()
            if left_member is None or right_member is None:
                raise TranscriptError("transcript_coverage_invalid")
            new_id, stable_key, position = str(uuid.uuid4()), str(uuid.uuid4()), left["position"]
            if left_member["group_id"] == right_member["group_id"]:
                group_id = left_member["group_id"]
                new_ordinal = left_member["ordinal"]
                if right_member["ordinal"] != left_member["ordinal"] + 1:
                    raise TranscriptError("transcript_merge_incompatible")
                connection.execute("UPDATE coverage_group_members SET ordinal=ordinal+1000000 WHERE revision_id=? AND group_id=?", (revision_id, group_id))
                connection.execute("DELETE FROM coverage_group_members WHERE revision_id=? AND row_id IN (?,?)", (revision_id, left["row_id"], right["row_id"]))
                for member in connection.execute("SELECT row_id,ordinal FROM coverage_group_members WHERE revision_id=? AND group_id=?", (revision_id, group_id)).fetchall():
                    old_ord = member["ordinal"] - 1_000_000
                    connection.execute("UPDATE coverage_group_members SET ordinal=? WHERE row_id=? AND revision_id=?", (old_ord - (1 if old_ord > right_member["ordinal"] else 0), member["row_id"], revision_id))
            else:
                for member in (left_member, right_member):
                    count = connection.execute("SELECT COUNT(*) FROM coverage_group_members WHERE revision_id=? AND group_id=?", (revision_id, member["group_id"])).fetchone()[0]
                    if count != 1:
                        raise TranscriptError("transcript_merge_incompatible")
                source_sets = []
                for member in (left_member, right_member):
                    source_sets.append([r["source_id"] for r in connection.execute("SELECT source_id FROM coverage_group_sources WHERE revision_id=? AND group_id=? ORDER BY ordinal", (revision_id, member["group_id"])).fetchall()])
                if set(source_sets[0]) & set(source_sets[1]):
                    raise TranscriptError("transcript_source_coverage_duplicate")
                all_sources = source_sets[0] + source_sets[1]
                placeholders = ",".join("?" for _ in all_sources)
                ordered_sources = connection.execute(f"SELECT id FROM segments WHERE id IN ({placeholders}) ORDER BY position", all_sources).fetchall()
                connection.execute("DELETE FROM coverage_group_members WHERE revision_id=? AND row_id IN (?,?)", (revision_id, left["row_id"], right["row_id"]))
                for member in (left_member, right_member):
                    connection.execute("DELETE FROM coverage_group_sources WHERE revision_id=? AND group_id=?", (revision_id, member["group_id"]))
                    connection.execute("DELETE FROM coverage_groups WHERE revision_id=? AND group_id=?", (revision_id, member["group_id"]))
                group_id = str(uuid.uuid4())
                connection.execute("INSERT INTO coverage_groups(group_id,project_id,revision_id,review_status,disposition) VALUES(?,?,?,'PENDING','INCLUDED')", (group_id, project_id, revision_id))
                for ordinal, source in enumerate(ordered_sources):
                    connection.execute("INSERT INTO coverage_group_sources(project_id,revision_id,group_id,source_id,ordinal) VALUES(?,?,?,?,?)", (project_id, revision_id, group_id, source["id"], ordinal))
                new_ordinal = 0
            connection.execute("UPDATE coverage_groups SET review_status='PENDING' WHERE revision_id=? AND group_id=?", (revision_id, group_id))
            inserts = [(new_id, stable_key, project_id, position, merged_text, left["start_us"], right["end_us"])]
            self._shift_positions(connection, revision_id, rows, {left["row_id"], right["row_id"]}, inserts, position + 1, -1)
            for ordinal, parent in enumerate((left["stable_segment_key"], right["stable_segment_key"])):
                connection.execute("INSERT INTO revision_segment_parents(project_id,child_revision_id,child_row_id,parent_revision_id,parent_stable_segment_key,ordinal) VALUES(?,?,?,?,?,?)", (project_id, revision_id, new_id, revision_id, parent, ordinal))
            connection.execute("INSERT INTO coverage_group_members(project_id,revision_id,group_id,row_id,ordinal) VALUES(?,?,?,?,?)", (project_id, revision_id, group_id, new_id, new_ordinal))
            connection.execute("UPDATE transcript_revisions SET lock_version=lock_version+1 WHERE id=? AND lock_version=?", (revision_id, expected_lock_version))
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)

    def review_group(self, project_id: str, revision_id: str, group_id: str, expected_lock_version: int, idempotency_key: str) -> dict[str, Any]:
        project_id, revision_id, group_id = self._uuid(project_id, "project_id"), self._uuid(revision_id, "revision_id"), self._uuid(group_id, "group_id")
        key = self._key(idempotency_key)
        digest = self._digest({"revision_id": revision_id, "group_id": group_id, "expected_lock_version": expected_lock_version})
        scope = f"transcript:review:{revision_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            self._check_lock(connection, project_id, revision_id, expected_lock_version)
            changed = connection.execute("UPDATE coverage_groups SET review_status='REVIEWED' WHERE project_id=? AND revision_id=? AND group_id=? AND review_status='PENDING'", (project_id, revision_id, group_id)).rowcount
            if changed != 1:
                raise TranscriptError("transcript_coverage_group_not_pending")
            connection.execute("UPDATE transcript_revisions SET lock_version=lock_version+1 WHERE id=? AND lock_version=?", (revision_id, expected_lock_version))
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)

    def _validate_coverage(self, connection: sqlite3.Connection, revision: sqlite3.Row) -> None:
        run_id, revision_id = revision["run_id"], revision["id"]
        source_ids = [row["id"] for row in connection.execute("SELECT id FROM segments WHERE run_id=? ORDER BY position", (run_id,)).fetchall()]
        covered = connection.execute("SELECT source_id FROM coverage_group_sources WHERE revision_id=? ORDER BY source_id", (revision_id,)).fetchall()
        covered_ids = [row["source_id"] for row in covered]
        if sorted(source_ids) != covered_ids:
            raise TranscriptError("transcript_source_coverage_invalid")
        revision_rows = connection.execute("SELECT row_id,position,text,start_us,end_us FROM revision_segments WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()
        if not revision_rows or [row["position"] for row in revision_rows] != list(range(len(revision_rows))):
            raise TranscriptError("transcript_segment_order_invalid")
        for row in revision_rows:
            if not row["text"].strip() or not 0 <= row["start_us"] < row["end_us"] <= revision["duration_us"]:
                raise TranscriptError("transcript_segment_invalid")
            count = connection.execute("SELECT COUNT(*) FROM coverage_group_members WHERE revision_id=? AND row_id=?", (revision_id, row["row_id"])).fetchone()[0]
            if count != 1:
                raise TranscriptError("transcript_member_coverage_invalid")
        groups = connection.execute("SELECT * FROM coverage_groups WHERE revision_id=?", (revision_id,)).fetchall()
        if not groups:
            raise TranscriptError("transcript_source_coverage_invalid")
        for group in groups:
            source_count = connection.execute("SELECT COUNT(*) FROM coverage_group_sources WHERE revision_id=? AND group_id=?", (revision_id, group["group_id"])).fetchone()[0]
            member_count = connection.execute("SELECT COUNT(*) FROM coverage_group_members WHERE revision_id=? AND group_id=?", (revision_id, group["group_id"])).fetchone()[0]
            if source_count == 0 or group["review_status"] != "REVIEWED":
                raise TranscriptError("transcript_review_required")
            if group["disposition"] == "SKIPPED":
                if not group["skip_reason"] or member_count != 0:
                    raise TranscriptError("transcript_coverage_invalid")
            elif member_count == 0:
                raise TranscriptError("transcript_member_coverage_invalid")

    def accept(self, project_id: str, revision_id: str, expected_lock_version: int, expected_active_revision_id: str | None, idempotency_key: str) -> dict[str, Any]:
        project_id, revision_id = self._uuid(project_id, "project_id"), self._uuid(revision_id, "revision_id")
        if expected_active_revision_id is not None:
            expected_active_revision_id = self._uuid(expected_active_revision_id, "expected_active_revision_id")
        key = self._key(idempotency_key)
        digest = self._digest({"revision_id": revision_id, "expected_lock_version": expected_lock_version, "expected_active_revision_id": expected_active_revision_id})
        scope = f"transcript:accept:{project_id}"
        with self._write() as connection:
            replay = self._replay(connection, scope, key, digest)
            if replay is not None:
                return replay
            revision = self._check_lock(connection, project_id, revision_id, expected_lock_version)
            self._validate_coverage(connection, revision)
            current = connection.execute("SELECT media_asset_id,generation FROM project_processing_sources WHERE project_id=?", (project_id,)).fetchone()
            active_run = connection.execute(
                """SELECT p.active_artifact_id,t.artifact_id,p.input_fingerprint,p.input_generation,
                          j.input_fingerprint AS job_input_fingerprint,j.expected_input_generation
                   FROM transcription_runs t JOIN artifacts a ON a.project_id=t.project_id AND a.id=t.artifact_id
                   JOIN jobs j ON j.project_id=a.project_id AND j.id=a.producer_job_id
                   JOIN stage_pointers p ON p.project_id=t.project_id AND p.stage='asr'
                   WHERE t.project_id=? AND t.id=?""",
                (project_id, revision["run_id"]),
            ).fetchone()
            if (current is None or current["media_asset_id"] != revision["source_asset_id"] or active_run is None or
                active_run["active_artifact_id"] != active_run["artifact_id"] or
                active_run["input_fingerprint"] != active_run["job_input_fingerprint"] or
                active_run["input_generation"] != active_run["expected_input_generation"]):
                raise TranscriptError("transcript_input_stale")
            pointer = connection.execute("SELECT revision_id,generation FROM project_active_transcript_revisions WHERE project_id=?", (project_id,)).fetchone()
            actual_active = pointer["revision_id"] if pointer else None
            if actual_active != expected_active_revision_id:
                raise TranscriptError("transcript_active_revision_conflict")
            stamp = utc_now()
            updated = connection.execute("UPDATE transcript_revisions SET status='ACCEPTED',accepted_at=?,lock_version=lock_version+1 WHERE id=? AND status='DRAFT' AND lock_version=?", (stamp, revision_id, expected_lock_version)).rowcount
            if updated != 1:
                raise TranscriptError("transcript_revision_conflict")
            if pointer:
                changed = connection.execute("UPDATE project_active_transcript_revisions SET revision_id=?,generation=generation+1,updated_at=? WHERE project_id=? AND revision_id=? AND generation=?", (revision_id, stamp, project_id, expected_active_revision_id, pointer["generation"])).rowcount
                if changed != 1:
                    raise TranscriptError("transcript_active_revision_conflict")
            else:
                connection.execute("INSERT INTO project_active_transcript_revisions(project_id,revision_id,generation,updated_at) VALUES(?,?,1,?)", (project_id, revision_id, stamp))
            self._record(connection, scope, key, digest, project_id, revision_id)
            return self._detail(connection, revision_id)
