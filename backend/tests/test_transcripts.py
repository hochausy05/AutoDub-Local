from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts import ArtifactPublicationService
from app.asr import ASRConfig, ASRService, TranscriptArtifactValidator
from app.database import Database, SCHEMA_VERSION
from app.jobs import JobService
from app.main import create_app
from app.media import FFprobe, LinkManifestValidator, MediaImportService, SourceVideoValidator, _path_identity
from app.recovery import StartupRecovery
from app.transcripts import TranscriptError, TranscriptService


class TranscriptTests(unittest.TestCase):
    class Adapter:
        def __init__(self, count=3):
            self.count = count

        def is_cached(self, _model):
            return True

        def run(self, _audio_path, _config, *, source_duration_us, source_offset_us, cancelled, progress):
            progress("loading_model")
            progress("transcribing")
            segments = []
            for index in range(self.count):
                start = 100_000 + index * 1_000
                end = start + 200_000
                segments.append({
                    "start_us": start + source_offset_us, "end_us": end + source_offset_us,
                    "source_text": ["Xin chào", "Thế giới", "再见"][index % 3],
                    "avg_logprob": -0.1, "no_speech_prob": 0.01, "compression_ratio": 1.0, "flags": [],
                })
            import faster_whisper, ctranslate2
            return {
                "segments": segments, "language": "vi", "language_probability": .99,
                "actual_device": "cpu", "compute_type": "int8", "fallback_reason": None,
                "model_id": "Systran/faster-whisper-base", "model_revision": "test-revision",
                "faster_whisper_version": faster_whisper.__version__, "ctranslate2_version": ctranslate2.__version__,
                "model_load_seconds": 0.01, "inference_seconds": 0.02, "run_status": "READY",
            }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "data")
        self.db.initialize()
        self.project, _ = self.db.create_project("Transcript revision tests", "transcript-project-idem-001")
        self.ffmpeg, self.ffprobe = shutil.which("ffmpeg"), FFprobe()
        if not self.ffmpeg or not self.ffprobe.executable:
            self.skipTest("ffmpeg/ffprobe unavailable")
        self.source = self.root / "字幕-字幕-日本語.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(self.source)], check=True, timeout=30)
        self.artifacts = self._artifacts(self.db)
        self.jobs = JobService(self.db, self.artifacts)
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        self.media = MediaImportService(self.db, self.jobs, self.artifacts, self.ffprobe)
        self.asset = self._import_source(self.source)
        self.run = self._make_run()
        self.service = TranscriptService(self.db)
        self.draft = self._draft()

    def tearDown(self):
        self.temp.cleanup()

    def _artifacts(self, db):
        return ArtifactPublicationService(db, {
            "source_video": SourceVideoValidator(self.ffprobe),
            "media_link_manifest": LinkManifestValidator(),
            "transcript_json": TranscriptArtifactValidator(),
        })

    def _import_source(self, source):
        previous = {item["id"] for item in self.media.list_assets(self.project["id"])}
        token, session = uuid.uuid4().hex * 2, "transcript-test-session"
        with closing(self.db.connect()) as c:
            c.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')", (hashlib.sha256(token.encode()).hexdigest(), self.media.session_hash(session), self.project["id"], str(source), source.name, json.dumps(_path_identity(source))))
        job = self.media.create_import_job(session, self.project["id"], token, "COPY", f"transcript-import-{uuid.uuid4().hex}")
        self.assertTrue(self.media.import_once())
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")
        return next(item for item in self.media.list_assets(self.project["id"]) if item["id"] not in previous)

    def _make_run(self, count=3):
        service = ASRService(self.db, self.jobs, self.artifacts, self.media, adapter=self.Adapter(count))
        job = service.create_job(self.project["id"], ASRConfig(device="cpu"), f"transcript-asr-{uuid.uuid4().hex}")
        self.assertTrue(service.run_once())
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")
        return service.list_runs(self.project["id"])[0]["id"]

    def _draft(self):
        return self.service.create_draft(self.project["id"], source_run_id=self.run, parent_revision_id=None, idempotency_key=f"transcript-create-{uuid.uuid4().hex}")

    def test_v10_migration_preserves_core_and_asr_and_is_repeatable(self):
        self.db.initialize()
        with closing(self.db.connect()) as c:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM segments WHERE run_id=?", (self.run,)).fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM projects WHERE id=?", (self.project["id"],)).fetchone()[0], 1)

    def test_full_ready_run_is_readable_and_original_segments_are_immutable(self):
        rows = self.draft["segments"]
        self.assertEqual(len(rows), 3)
        edit = {"row_id": rows[0]["row_id"], "text": "Xin chào, thế giới 世界"}
        updated = self.service.save_edits(self.project["id"], self.draft["id"], self.draft["lock_version"], [edit], "transcript-save-unicode-0001")
        self.assertEqual(updated["segments"][0]["text"], edit["text"])
        self.assertEqual((updated["segments"][0]["start_us"], updated["segments"][0]["end_us"]), (rows[0]["start_us"], rows[0]["end_us"]))
        original = self._asr_service().get_run(self.project["id"], self.run)
        self.assertEqual(original["segments"][0]["source_text"], "Xin chào")

    def test_noop_text_save_does_not_advance_revision_or_change_timestamps(self):
        row = self.draft["segments"][0]
        saved = self.service.save_edits(
            self.project["id"], self.draft["id"], self.draft["lock_version"],
            [{"row_id": row["row_id"], "text": row["text"]}], "transcript-noop-save-0001",
        )
        self.assertEqual(saved["lock_version"], self.draft["lock_version"])
        self.assertEqual((saved["segments"][0]["start_us"], saved["segments"][0]["end_us"]), (row["start_us"], row["end_us"]))
        self.assertEqual(saved["segments"][0]["text"], row["text"])
        self.assertEqual(len(self.service.list_revisions(self.project["id"], self.asset["id"])), 1)

    def test_accepting_original_baseline_once_keeps_it_as_single_active_revision(self):
        accepted = self.service.accept(self.project["id"], self.draft["id"], self.draft["lock_version"], None, "transcript-accept-original-0001")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertTrue(accepted["is_current"])
        self.assertTrue(all(segment["text"] == segment["source_texts"][0] for segment in accepted["segments"]))
        revisions = self.service.list_revisions(self.project["id"], self.asset["id"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(sum(revision["status"] == "ACCEPTED" and revision["is_active"] for revision in revisions), 1)

    def _asr_service(self):
        return ASRService(self.db, self.jobs, self.artifacts, self.media, adapter=self.Adapter())

    def test_multi_edit_commit_restart_and_stale_cas_preserve_newer_draft(self):
        rows = self.draft["segments"]
        edits = [{"row_id": rows[0]["row_id"], "text": "Tiếng Việt có dấu"}, {"row_id": rows[1]["row_id"], "text": "中文字符"}]
        saved = self.service.save_edits(self.project["id"], self.draft["id"], 1, edits, "transcript-save-two-edits-01")
        self.assertEqual([row["text"] for row in saved["segments"][:2]], ["Tiếng Việt có dấu", "中文字符"])
        with self.assertRaisesRegex(TranscriptError, "transcript_revision_conflict"):
            self.service.save_edits(self.project["id"], self.draft["id"], 1, [{"row_id": rows[0]["row_id"], "text": "stale local input"}], "transcript-save-stale-001")
        reopened = TranscriptService(Database(self.db.data_root)).get_revision(self.project["id"], self.draft["id"])
        self.assertEqual([row["text"] for row in reopened["segments"][:2]], ["Tiếng Việt có dấu", "中文字符"])

    def test_timestamp_bounds_and_unicode_scalar_are_validated(self):
        row = self.draft["segments"][0]
        with self.assertRaisesRegex(TranscriptError, "invalid_transcript_timing"):
            self.service.save_edits(self.project["id"], self.draft["id"], 1, [{"row_id": row["row_id"], "start_us": -1}], "transcript-invalid-time-001")
        with self.assertRaisesRegex(TranscriptError, "invalid_transcript_text"):
            self.service.save_edits(self.project["id"], self.draft["id"], 1, [{"row_id": row["row_id"], "text": "bad\ud800"}], "transcript-invalid-unicode-01")

    def test_split_shares_source_coverage_requires_review_and_accepts_snapshot(self):
        first = self.draft["segments"][0]
        split = self.service.split_segment(self.project["id"], self.draft["id"], first["row_id"], 1, 4, 200_000, "transcript-split-segment-001")
        children = split["segments"][:2]
        self.assertEqual(children[0]["coverage_group_id"], children[1]["coverage_group_id"])
        self.assertEqual(children[0]["source_segment_ids"], children[1]["source_segment_ids"])
        self.assertEqual(children[0]["parent_keys"], [first["stable_segment_key"]])
        with self.assertRaisesRegex(TranscriptError, "transcript_review_required"):
            self.service.accept(self.project["id"], self.draft["id"], split["lock_version"], None, "transcript-accept-pending-001")
        accepted_draft = self.service.review_group(self.project["id"], self.draft["id"], children[0]["coverage_group_id"], split["lock_version"], "transcript-review-split-0001")
        accepted = self.service.accept(self.project["id"], self.draft["id"], accepted_draft["lock_version"], None, "transcript-accept-split-0001")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertTrue(accepted["is_active"])
        with closing(self.db.connect()) as c:
            count, distinct_count = c.execute("SELECT COUNT(*),COUNT(DISTINCT source_id) FROM coverage_group_sources WHERE revision_id=?", (self.draft["id"],)).fetchone()
            self.assertEqual((count, distinct_count), (3, 3))
            with self.assertRaises(Exception):
                c.execute("UPDATE revision_segments SET text='tampered' WHERE revision_id=?", (self.draft["id"],))

    def test_split_rejects_grapheme_like_combining_boundary(self):
        row = self.draft["segments"][0]
        saved = self.service.save_edits(self.project["id"], self.draft["id"], 1, [{"row_id": row["row_id"], "text": "e\u0301cho"}], "transcript-save-grapheme-01")
        with self.assertRaisesRegex(TranscriptError, "unsafe_unicode_split"):
            self.service.split_segment(self.project["id"], self.draft["id"], row["row_id"], saved["lock_version"], 1, 200_000, "transcript-split-grapheme-01")

    def test_merge_within_split_group_retains_lineage_and_coverage(self):
        first = self.draft["segments"][0]
        split = self.service.split_segment(self.project["id"], self.draft["id"], first["row_id"], 1, 4, 200_000, "transcript-split-for-merge-01")
        left, right = split["segments"][:2]
        merged = self.service.merge_next(self.project["id"], self.draft["id"], left["row_id"], split["lock_version"], "Xin chào", "transcript-merge-split-group-01")
        self.assertEqual(len(merged["segments"]), 3)
        row = merged["segments"][0]
        self.assertEqual(row["parent_keys"], [left["stable_segment_key"], right["stable_segment_key"]])
        self.assertEqual(row["source_segment_ids"], first["source_segment_ids"])
        self.assertEqual([s["position"] for s in merged["segments"]], [0, 1, 2])

    def test_merge_distinct_groups_unions_sources_and_preserves_order(self):
        left, right = self.draft["segments"][:2]
        merged = self.service.merge_next(self.project["id"], self.draft["id"], left["row_id"], 1, "Xin chào Thế giới", "transcript-merge-distinct-groups-01")
        self.assertEqual(len(merged["segments"]), 2)
        self.assertEqual(len(merged["segments"][0]["source_segment_ids"]), 2)
        self.assertEqual(merged["segments"][0]["parent_keys"], [left["stable_segment_key"], right["stable_segment_key"]])
        self.assertEqual([s["position"] for s in merged["segments"]], [0, 1])

    def test_edit_after_accept_creates_new_revision_and_never_changes_accepted(self):
        accepted = self.service.accept(self.project["id"], self.draft["id"], 1, None, "transcript-accept-first-0001")
        new_draft = self.service.create_draft(self.project["id"], source_run_id=self.run, parent_revision_id=None, idempotency_key="transcript-create-after-accept-01")
        self.assertEqual(new_draft["parent_revision_id"], accepted["id"])
        self.assertEqual(new_draft["segments"][0]["stable_segment_key"], accepted["segments"][0]["stable_segment_key"])
        self.assertNotEqual(new_draft["segments"][0]["row_id"], accepted["segments"][0]["row_id"])
        changed = self.service.save_edits(self.project["id"], new_draft["id"], 1, [{"row_id": new_draft["segments"][0]["row_id"], "text": "Sửa sau duyệt"}], "transcript-edit-after-accept-01")
        self.assertEqual(self.service.get_revision(self.project["id"], accepted["id"])["segments"][0]["text"], "Xin chào")
        self.assertEqual(changed["segments"][0]["text"], "Sửa sau duyệt")

    def test_asr_rerun_keeps_accepted_revision_as_historical_snapshot(self):
        accepted = self.service.accept(self.project["id"], self.draft["id"], 1, None, "transcript-accept-before-rerun-01")
        rerun = self._make_run()
        self.assertNotEqual(rerun, self.run)
        preserved = self.service.get_revision(self.project["id"], accepted["id"])
        self.assertEqual(preserved["status"], "ACCEPTED")
        self.assertTrue(preserved["is_active"])
        self.assertFalse(preserved["is_current"])
        self.assertEqual(preserved["segments"][0]["text"], "Xin chào")

    def test_changed_processing_source_blocks_accept_and_keeps_saved_draft(self):
        draft = self.service.save_edits(self.project["id"], self.draft["id"], 1, [{"row_id": self.draft["segments"][0]["row_id"], "text": "Draft stays"}], "transcript-stale-save-00001")
        other_source = self.root / "another-video.mp4"
        other_source.write_bytes(self.source.read_bytes())
        self.media.delete_asset(self.project["id"],self.asset["id"],self.asset["lock_version"],"transcript-delete-before-replace-01")
        other = self._import_source(other_source)
        with closing(self.db.connect()) as c:
            current = c.execute("SELECT generation FROM project_processing_sources WHERE project_id=?", (self.project["id"],)).fetchone()[0]
        # Select through the service CAS to update the current asset.
        from app.asr import ASRService
        asr = ASRService(self.db, self.jobs, self.artifacts, self.media, adapter=self.Adapter())
        changed = asr.select_processing_source(self.project["id"], other["id"], current)
        with self.assertRaisesRegex(TranscriptError, "transcript_input_stale"):
            self.service.accept(self.project["id"], self.draft["id"], draft["lock_version"], None, "transcript-stale-accept-01")
        self.assertEqual(self.service.get_revision(self.project["id"], self.draft["id"])["segments"][0]["text"], "Draft stays")

    def test_many_segments_remain_complete_in_revision(self):
        run_id = self._make_run(500)
        long_draft = self.service.create_draft(self.project["id"], source_run_id=run_id, parent_revision_id=None, idempotency_key="transcript-create-500-segments-01")
        self.assertEqual(len(long_draft["segments"]), 500)
        self.assertEqual([s["position"] for s in long_draft["segments"]], list(range(500)))

    def test_revision_api_requires_session_csrf_and_project_ownership(self):
        app = create_app("transcript-api-bootstrap", data_root=self.root / "api-data")
        app.state.database.initialize()
        project, _ = app.state.database.create_project("Transcript API", "transcript-api-project-idem-01")
        with TestClient(app, client=("127.0.0.1", 50431)) as client:
            headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
            self.assertEqual(client.get(f"/api/v1/projects/{project['id']}/transcripts?source_asset_id={self.asset['id']}", headers=headers).status_code, 401)
            boot = client.post("/api/v1/session/bootstrap", headers={**headers, "content-type": "application/json", "x-local-bootstrap": "transcript-api-bootstrap"}, json={})
            csrf = boot.json()["csrf_token"]
            denied = client.post(f"/api/v1/projects/{project['id']}/transcripts/revisions", headers={**headers, "content-type": "application/json", "idempotency-key": "transcript-api-create-denied-01"}, json={"source_run_id": self.run})
            self.assertEqual(denied.status_code, 403)
            # A revision ID from this Project cannot be read through an unrelated Project path.
            wrong_project = client.get(f"/api/v1/projects/{project['id']}/transcripts/revisions/{self.draft['id']}", headers=headers)
            self.assertEqual(wrong_project.status_code, 404)

    def test_revision_api_saves_accepts_and_protects_session_and_project(self):
        app = create_app("transcript-api-full-bootstrap", data_root=self.db.data_root)
        other_project, _ = self.db.create_project("Another Project", "transcript-api-other-project-01")
        with TestClient(app, client=("127.0.0.1", 50432)) as client:
            headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
            boot = client.post("/api/v1/session/bootstrap", headers={**headers, "content-type": "application/json", "x-local-bootstrap": "transcript-api-full-bootstrap"}, json={})
            csrf = boot.json()["csrf_token"]
            denied = client.patch(f"/api/v1/projects/{self.project['id']}/transcripts/revisions/{self.draft['id']}", headers={**headers, "content-type": "application/json"}, json={"expected_lock_version": 1, "edits": [{"row_id": str(self.draft["segments"][0]["row_id"]), "text": "Blocked edit"}]})
            self.assertEqual(denied.status_code, 403)
            detail = client.get(f"/api/v1/projects/{self.project['id']}/transcripts/revisions/{self.draft['id']}", headers=headers)
            self.assertEqual(detail.status_code, 200)
            first = detail.json()["segments"][0]
            saved = client.patch(
                f"/api/v1/projects/{self.project['id']}/transcripts/revisions/{self.draft['id']}",
                headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "transcript-api-save-edit-0001"},
                json={"expected_lock_version": 1, "edits": [{"row_id": first["row_id"], "text": "Đã sửa lời thoại"}]},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["segments"][0]["text"], "Đã sửa lời thoại")
            stale = client.patch(
                f"/api/v1/projects/{self.project['id']}/transcripts/revisions/{self.draft['id']}",
                headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "transcript-api-save-stale-0001"},
                json={"expected_lock_version": 1, "edits": [{"row_id": first["row_id"], "text": "Không ghi đè"}]},
            )
            self.assertEqual(stale.status_code, 409)
            foreign = client.get(f"/api/v1/projects/{other_project['id']}/transcripts/revisions/{self.draft['id']}", headers=headers)
            self.assertEqual(foreign.status_code, 404)
            accepted = client.post(
                f"/api/v1/projects/{self.project['id']}/transcripts/revisions/{self.draft['id']}/accept",
                headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "transcript-api-accept-000001"},
                json={"expected_lock_version": 2, "expected_active_revision_id": None},
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["status"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()

