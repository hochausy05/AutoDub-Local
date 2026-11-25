"""Translation domain contracts and read-only access to historical Runs."""
from __future__ import annotations
import json
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from app.database import Database

class TranslationError(Exception):
    def __init__(self, code: str, *, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)

def normalize_translation_unit(text: str) -> str:
    if not isinstance(text, str) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise TranslationError("translation_invalid_unicode")
    value = text.strip()
    closing = "".join(map(chr, [34, 39, 8221, 8217, 187, 41, 93, 125, 12301, 12303, 12305, 65289]))
    punctuation = "".join(map(chr, [46, 12290, 65294, 65377, 33, 65281, 63, 65311, 44, 65292, 12289, 58, 65306, 59, 65307, 8230]))
    while value:
        suffix = ""
        while value and value[-1] in closing:
            suffix = value[-1] + suffix
            value = value[:-1]
        if value and value[-1] in punctuation:
            value = value[:-1].rstrip() + suffix
        else:
            value = value.rstrip() + suffix
            break
    value = value.rstrip()
    if not value:
        raise TranslationError("translation_empty_after_normalization")
    return value

class TranslationArtifactValidator:
    version = "translation-json-v1"

    def __init__(self, database: Database | None = None):
        self.database = database

    def validate(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationError("translation_artifact_invalid") from exc
        if (not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("run_id"), str)
                or not isinstance(value.get("input_fingerprint"), str) or len(value["input_fingerprint"]) != 64
                or not isinstance(value.get("units"), list)):
            raise TranslationError("translation_artifact_invalid")
        strategy = value.get("pass_strategy", "TWO_PASS")
        if strategy not in {"TWO_PASS", "SINGLE_PASS"}:
            raise TranslationError("translation_artifact_invalid")
        ids = [unit.get("unit_id") for unit in value["units"] if isinstance(unit, dict)]
        def unit_invalid(unit: dict[str, Any]) -> bool:
            result_missing = (not isinstance(unit.get("final_text"), str) or not unit["final_text"].strip()) if strategy == "SINGLE_PASS" else (
                not isinstance(unit.get("pass1_text"), str) or not unit["pass1_text"].strip()
                or not isinstance(unit.get("pass2_text"), str) or not unit["pass2_text"].strip())
            return result_missing or not isinstance(unit.get("revision_segment_ids"), list) or not unit["revision_segment_ids"] or not isinstance(unit.get("source_segment_ids"), list) or not unit["source_segment_ids"]
        if (len(ids) != len(value["units"]) or len(ids) != len(set(ids)) or any(unit_invalid(unit) for unit in value["units"])):
            raise TranslationError("translation_artifact_invalid")
        if self.database is not None:
            with closing(self.database.connect()) as connection:
                run = connection.execute("SELECT input_fingerprint,pass_strategy FROM translation_runs WHERE id=?", (value["run_id"],)).fetchone()
                expected = connection.execute("SELECT unit_id,position,revision_segment_ids_json,source_segment_ids_json,source_text,pass1_text,pass2_text,final_text,final_notes_json FROM translation_units WHERE run_id=? ORDER BY position", (value["run_id"],)).fetchall()
            if run is None or run["input_fingerprint"] != value["input_fingerprint"] or run["pass_strategy"] != strategy or len(expected) != len(value["units"]):
                raise TranslationError("translation_artifact_input_mismatch")
            for stored, published in zip(expected, value["units"], strict=True):
                if (stored["unit_id"] != published.get("unit_id") or stored["position"] != published.get("position")
                        or json.loads(stored["revision_segment_ids_json"]) != published.get("revision_segment_ids")
                        or json.loads(stored["source_segment_ids_json"]) != published.get("source_segment_ids")
                        or stored["source_text"] != published.get("source_text")
                        or stored["pass1_text"] != published.get("pass1_text") or stored["pass2_text"] != published.get("pass2_text")
                        or stored["final_text"] != published.get("final_text")
                        or json.loads(stored["final_notes_json"]) != published.get("final_notes", [])):
                    raise TranslationError("translation_artifact_mapping_mismatch")
        return {"run_id": value["run_id"], "unit_count": len(ids), "schema_version": 1}



class TranslationService:
    def __init__(self, database: Database, jobs: Any):
        self.database, self.jobs = database, jobs

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try: parsed=uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc: raise TranslationError(f"invalid_{field}") from exc
        if str(parsed) != value.lower(): raise TranslationError(f"invalid_{field}")
        return str(parsed)

    def _build_input(self, project_id: str, revision_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with closing(self.database.connect()) as connection:
            revision = connection.execute(
                """SELECT r.*,t.status AS run_status,t.source_asset_id,t.artifact_id AS asr_artifact_id,
                          p.revision_id AS active_revision,p.generation AS transcript_generation,
                          ps.media_asset_id AS selected_asset,ps.generation AS source_generation,
                          asp.active_artifact_id AS active_asr,asp.input_fingerprint AS asr_fingerprint,
                          a.status AS source_status
                   FROM transcript_revisions r JOIN transcription_runs t ON t.project_id=r.project_id AND t.id=r.run_id
                   JOIN project_active_transcript_revisions p ON p.project_id=r.project_id
                   JOIN project_processing_sources ps ON ps.project_id=r.project_id
                   JOIN stage_pointers asp ON asp.project_id=r.project_id AND asp.stage='asr'
                   JOIN media_assets a ON a.project_id=r.project_id AND a.id=t.source_asset_id
                   WHERE r.project_id=? AND r.id=?""", (project_id, revision_id),
            ).fetchone()
            if revision is None:
                raise TranslationError("accepted_transcript_required")
            if revision["status"] != "ACCEPTED" or revision["active_revision"] != revision_id or revision["selected_asset"] != revision["source_asset_id"] or revision["source_status"] != "READY":
                raise TranslationError("accepted_transcript_stale")
            if revision["run_status"] not in {"READY", "REVIEW_REQUIRED"} or revision["active_asr"] != revision["asr_artifact_id"]:
                raise TranslationError("accepted_transcript_stale")
            groups = connection.execute("SELECT * FROM coverage_groups WHERE revision_id=?", (revision_id,)).fetchall()
            def group_position(group):
                member = connection.execute("SELECT MIN(rs.position) FROM coverage_group_members gm JOIN revision_segments rs ON rs.row_id=gm.row_id WHERE gm.revision_id=? AND gm.group_id=?", (revision_id, group["group_id"])).fetchone()[0]
                if member is not None: return member
                source_position = connection.execute("SELECT MIN(s.position) FROM coverage_group_sources gs JOIN segments s ON s.id=gs.source_id WHERE gs.revision_id=? AND gs.group_id=?", (revision_id, group["group_id"])).fetchone()[0]
                return source_position if source_position is not None else 0
            groups = sorted(groups, key=group_position)
            units: list[dict[str, Any]] = []
            seen_rows: set[str] = set()
            seen_sources: set[str] = set()
            for group in groups:
                sources = connection.execute("SELECT source_id FROM coverage_group_sources WHERE revision_id=? AND group_id=? ORDER BY ordinal", (revision_id, group["group_id"])).fetchall()
                source_ids = [row["source_id"] for row in sources]
                if seen_sources.intersection(source_ids):
                    raise TranslationError("source_coverage_invalid")
                seen_sources.update(source_ids)
                if group["review_status"] != "REVIEWED":
                    raise TranslationError("transcript_review_required")
                if group["disposition"] == "SKIPPED":
                    continue
                members = connection.execute(
                    """SELECT rs.row_id,rs.stable_segment_key,rs.position,rs.text,rs.start_us,rs.end_us
                       FROM coverage_group_members gm JOIN revision_segments rs ON rs.project_id=gm.project_id AND rs.revision_id=gm.revision_id AND rs.row_id=gm.row_id
                       WHERE gm.revision_id=? AND gm.group_id=? ORDER BY gm.ordinal""", (revision_id, group["group_id"]),
                ).fetchall()
                row_ids = [row["row_id"] for row in members]
                stable_keys = [row["stable_segment_key"] for row in members]
                if not members or seen_rows.intersection(row_ids):
                    raise TranslationError("translation_unit_coverage_invalid")
                seen_rows.update(row_ids)
                texts = [row["text"] for row in members]
                source_text = "".join(texts)
                if not source_text.strip():
                    raise TranslationError("translation_unit_empty")
                unit_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"autodub:{project_id}:translation-unit:{':'.join(stable_keys)}"))
                units.append({"unit_id": unit_id, "logical_unit_key": unit_id, "position": len(units),
                    "revision_segment_ids": row_ids, "source_segment_ids": source_ids, "source_text": source_text,
                    "start_us": min(row["start_us"] for row in members), "end_us": max(row["end_us"] for row in members)})
            included_rows = connection.execute("SELECT row_id FROM revision_segments WHERE revision_id=? ORDER BY position", (revision_id,)).fetchall()
            all_rows = [row["row_id"] for row in included_rows]
            source_rows = connection.execute("SELECT id FROM segments WHERE run_id=? ORDER BY position", (revision["run_id"],)).fetchall()
            if not units or set(all_rows) != seen_rows or len(all_rows) != len(seen_rows) or set(row["id"] for row in source_rows) != seen_sources:
                raise TranslationError("translation_unit_coverage_invalid")
            return {"schema_version": 1, "project_id": project_id, "transcript_revision_id": revision_id,
                "source_asset_id": revision["source_asset_id"], "source_generation": revision["source_generation"],
                "transcript_generation": revision["transcript_generation"], "asr_artifact_id": revision["asr_artifact_id"],
                "asr_input_fingerprint": revision["asr_fingerprint"]}, units


    def get_run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        with closing(self.database.connect()) as connection:
            run = connection.execute("SELECT * FROM translation_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if run is None:
                return None
            value = dict(run)
            active = connection.execute("SELECT active_artifact_id,input_fingerprint FROM stage_pointers WHERE project_id=? AND stage='translation'", (project_id,)).fetchone()
            value["is_active"] = bool(active and active["active_artifact_id"] == run["artifact_id"] and active["input_fingerprint"] == self.jobs.get_job(run["producer_job_id"])["input_fingerprint"])
            value["chunks"] = [dict(row) for row in connection.execute("SELECT * FROM translation_chunks WHERE run_id=? ORDER BY chunk_no", (run_id,)).fetchall()]
            value["units"] = []
            for row in connection.execute("SELECT * FROM translation_units WHERE run_id=? ORDER BY position", (run_id,)).fetchall():
                item = dict(row)
                item["final_text"] = item["final_text"] if item["final_text"] is not None else item["pass2_text"]
                item["revision_segment_ids"] = json.loads(item.pop("revision_segment_ids_json"))
                item["source_segment_ids"] = json.loads(item.pop("source_segment_ids_json"))
                item["pass1_notes"] = json.loads(item.pop("pass1_notes_json"))
                item["final_notes"] = json.loads(item.pop("final_notes_json"))
                item["review_flags"] = json.loads(item.pop("review_flags_json"))
                value["units"].append(item)
            return value

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM translation_runs WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()]
        return [self.get_run(project_id, run_id) for run_id in ids]
