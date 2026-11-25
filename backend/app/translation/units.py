"""Canonical Translation Unit construction and source coverage validation."""
from contextlib import closing, nullcontext
from typing import Any
import uuid
from app.translation.service import TranslationError

class CanonicalUnitBuilder:
    def __init__(self, database):
        self.database = database

    def build(self, project_id: str, revision_id: str, connection=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with (closing(self.database.connect()) if connection is None else nullcontext(connection)) as connection:
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

