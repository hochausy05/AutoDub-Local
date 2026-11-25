from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from app.artifacts import ArtifactError
from app.database import Database, SCHEMA_VERSION
from app.main import create_app
from app.jobs import JobService
from app.recovery import StartupRecovery
from app.tts import EdgeTTSAdapter, TTSError, TTSService, TTSWorker, _RequestPacer
from app.translation_revisions import TranslationRevisionService


class FixtureEdgeAdapter:
    def __init__(self, ffmpeg: str, *, fail_text: str | None = None, fail_calls: int = 0):
        self.ffmpeg = ffmpeg
        self.calls: list[str] = []
        self.fail_text = fail_text
        self.fail_calls = fail_calls

    def list_voices(self, *, refresh=False):
        return {"provider": "edge", "voices": [{"short_name": "vi-VN-NamMinhNeural", "locale": "vi-VN"}],
                "default_voice": "vi-VN-NamMinhNeural", "cached": not refresh}

    def synthesize(self, text: str, target: Path, **_options):
        self.calls.append(text)
        if self.fail_text and self.fail_text in text and self.fail_calls:
            self.fail_calls -= 1
            raise TTSError("tts_provider_timeout", retryable=True)
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=550:sample_rate=24000", "-t", "0.25", "-ar", "24000", "-ac", "1",
            "-b:a", "48k", "-y", str(target)], check=True, timeout=20)


class TTSIntegrationTests(unittest.TestCase):
    """Reuse the established synthetic media/ASR/translation fixture; no provider calls."""

    def setUp(self):
        from importlib import import_module
        fixture = getattr(import_module("tests.test_translation"), "TranslationTests")
        self._translation_fixture = fixture
        fixture.setUp(self)
        if not self.ffmpeg:
            self.skipTest("ffmpeg unavailable")
        self.translation_run_id, _provider = fixture._ready_single_pass_translation(self)
        self.translation_revisions = TranslationRevisionService(self.db)
        self.translation_revisions.accept_original(self.project["id"], self.translation_run_id, None, "tts-fixture-accept-key-01")
        self.tts_adapter = FixtureEdgeAdapter(self.ffmpeg)
        self.tts = TTSService(self.db, self.jobs, self.artifacts, self.translation_revisions, adapter=self.tts_adapter)
        self.tts_worker = TTSWorker(self.tts)

    def tearDown(self):
        self._translation_fixture.tearDown(self)

    def _create(self, key: str, revision_id: str | None = None):
        accepted = self.translation_revisions.editor_state(self.project["id"], self.translation_run_id)["accepted"]
        return self.tts.create_batch(self.project["id"], revision_id or accepted["id"],
            {"voice": "vi-VN-NamMinhNeural", "rate_percent": 0}, True, key)

    def test_idempotent_batch_creation_returns_persisted_run_identity(self):
        first = self._create("tts-idempotency-key-0001")
        replay = self._create("tts-idempotency-key-0001")
        self.assertEqual(first["run_id"], replay["run_id"])
        self.assertEqual(first["job"]["id"], replay["job"]["id"])

    def test_batch_creation_rollback_keeps_stage_pointer_and_job_rows_unchanged(self):
        snapshot = {"schema_version": 1, "kind": "edge_tts_batch", "unit_count": 2, "units_sha256": "a" * 64}
        with closing(self.db.connect()) as connection:
            before_jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND kind='edge_tts_batch'", (self.project["id"],)).fetchone()[0]
            before_stage = connection.execute("SELECT input_fingerprint,input_generation FROM stage_pointers WHERE project_id=? AND stage='tts'", (self.project["id"],)).fetchone()
        def fail_hook(_connection, _job_id):
            raise RuntimeError("synthetic rollback")
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            self.jobs.create_job(self.project["id"],"edge_tts_batch",snapshot,"tts-atomic-rollback-0001",
                activation_stage="tts",update_stage_input="tts",creation_hook=fail_hook)
        with closing(self.db.connect()) as connection:
            after_jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND kind='edge_tts_batch'", (self.project["id"],)).fetchone()[0]
            after_stage = connection.execute("SELECT input_fingerprint,input_generation FROM stage_pointers WHERE project_id=? AND stage='tts'", (self.project["id"],)).fetchone()
        self.assertEqual(after_jobs,before_jobs)
        self.assertEqual(tuple(after_stage) if after_stage else None,tuple(before_stage) if before_stage else None)

    def test_verified_unit_audio_and_projected_duration(self):
        created = self._create("tts-verified-key-00001")
        self.assertTrue(self.tts_worker.run_once())
        run = self.tts.get_run(self.project["id"], created["run_id"])
        self.assertEqual(run["status"], "READY")
        self.assertEqual(run["progress"]["ready_units"], run["progress"]["total_units"])
        for unit in run["units"]:
            self.assertEqual(unit["status"], "READY")
            self.assertGreater(unit["artifact"]["sample_count"], 0)
            self.assertEqual(unit["artifact"]["sample_rate"], 24000)
            self.assertGreater(unit["artifact"]["duration_us"], 0)
            opened = self.tts.open_audio(self.project["id"], unit["artifact"]["id"])
            opened[0].close()
        reopened = Database(self.db.data_root)
        reopened.initialize()
        from app.artifacts import ArtifactPublicationService
        artifact_service = ArtifactPublicationService(reopened, self.artifacts.validators.copy())
        job_service = JobService(reopened, artifact_service)
        recovered_tts = TTSService(reopened, job_service, artifact_service, TranslationRevisionService(reopened), adapter=self.tts_adapter)
        artifact_report = artifact_service.reconcile_publications()
        self.assertFalse(artifact_report["startup_blocked"], artifact_report)
        StartupRecovery(reopened, job_service, artifact_service).reconcile()
        recovered = recovered_tts.get_run(self.project["id"], created["run_id"])
        self.assertEqual(recovered["status"], "READY")
        self.assertEqual(recovered["progress"]["ready_units"], len(run["units"]))

    def test_batch_retries_only_failed_retryable_unit_across_bounded_rounds(self):
        accepted = self.translation_revisions.editor_state(self.project["id"], self.translation_run_id)["accepted"]
        failing_text = accepted["units"][1]["final_text"]
        self.tts_adapter.fail_text = failing_text
        self.tts_adapter.fail_calls = 2  # Two failed rounds; third round succeeds.
        created = self._create("tts-retry-key-000001", accepted["id"])
        self.assertTrue(self.tts_worker.run_once())
        ready = self.tts.get_run(self.project["id"], created["run_id"])
        self.assertEqual(ready["status"], "READY", ready)
        self.assertEqual(len(self.tts_adapter.calls), 5)
        with closing(self.db.connect()) as connection:
            calls = connection.execute("SELECT round_no,status FROM tts_provider_calls WHERE tts_run_id=? AND unit_id=? ORDER BY call_no", (created["run_id"], accepted["units"][1]["unit_id"])).fetchall()
        self.assertEqual([(row["round_no"], row["status"]) for row in calls], [(1, "FAILED"), (2, "FAILED"), (3, "READY")])

    def test_unchanged_units_reuse_artifacts_when_accepted_revision_changes(self):
        initial = self._create("tts-cache-first-key-01")
        self.tts_worker.run_once()
        before_calls = len(self.tts_adapter.calls)
        revisions = self.translation_revisions
        editor = revisions.editor_state(self.project["id"], self.translation_run_id)
        accepted = editor["accepted"]
        draft = revisions.create_draft(self.project["id"], accepted["run_id"], accepted["id"])
        changed = revisions.save_draft(self.project["id"], accepted["run_id"], draft["id"], draft["lock_version"],
            [{"unit_id": draft["units"][1]["unit_id"], "text": "Bản dịch mới, vẫn giữ nguyên ý."}], "tts-cache-edit-key-001")
        newer = revisions.accept_draft(self.project["id"], accepted["run_id"], draft["id"], changed["lock_version"], accepted["id"], "tts-cache-accept-key-01")
        second = self._create("tts-cache-second-key-01", newer["id"])
        self.assertEqual(second["status"]["progress"]["reused_units"], 2)
        self.tts_worker.run_once()
        final = self.tts.get_run(self.project["id"], second["run_id"])
        self.assertEqual(final["status"], "READY")
        self.assertEqual(len(self.tts_adapter.calls) - before_calls, 1)
        self.assertTrue(all(unit["status"] == "READY" for unit in final["units"]))

    def test_consent_is_required_before_provider_catalog_or_synthesis(self):
        accepted = self.translation_revisions.editor_state(self.project["id"], self.translation_run_id)["accepted"]
        with self.assertRaisesRegex(TTSError, "edge_tts_consent_required"):
            self.tts.create_batch(self.project["id"], accepted["id"], {"voice": "vi-VN-NamMinhNeural"}, False, "tts-consent-denied-01")
        self.assertEqual(self.tts_adapter.calls, [])

    def test_cancel_queued_batch_closes_run_and_units(self):
        created = self._create("tts-queued-cancel-key-01")
        self.jobs.request_cancel(created["job"]["id"], "tts-cancel-request-key-1")
        self.tts.reconcile_cancelled_job(created["job"]["id"])
        run = self.tts.get_run(self.project["id"], created["run_id"])
        self.assertEqual(run["status"], "CANCELLED")
        self.assertEqual({unit["status"] for unit in run["units"]}, {"CANCELLED"})

    def test_recovery_interrupts_call_and_requeues_only_running_tts_unit(self):
        created = self._create("tts-recovery-key-00001")
        claimed = self.jobs.claim_next_job(kind="edge_tts_batch")
        self.assertEqual(claimed["id"], created["job"]["id"])
        attempt_id = claimed["attempt"]["id"]
        with closing(self.db.connect()) as connection:
            unit = connection.execute("SELECT unit_id FROM tts_run_units WHERE tts_run_id=? ORDER BY position LIMIT 1", (created["run_id"],)).fetchone()
            connection.execute("UPDATE tts_run_units SET status='RUNNING' WHERE tts_run_id=? AND unit_id=?", (created["run_id"], unit["unit_id"]))
            connection.execute("INSERT INTO tts_provider_calls(id,project_id,tts_run_id,unit_id,job_attempt_id,call_kind,round_no,call_no,status,started_at) VALUES(?,?,?,?,?,'batch_unit',1,1,'RUNNING',?)", (str(uuid.uuid4()),self.project["id"],created["run_id"],unit["unit_id"],attempt_id,"2026-01-01T00:00:00Z"))
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        with closing(self.db.connect()) as connection:
            unit_status = connection.execute("SELECT status,retryable FROM tts_run_units WHERE tts_run_id=? AND unit_id=?", (created["run_id"],unit["unit_id"])).fetchone()
            call_status = connection.execute("SELECT status,error_code FROM tts_provider_calls WHERE job_attempt_id=?", (attempt_id,)).fetchone()
        self.assertEqual((unit_status["status"],unit_status["retryable"]), ("QUEUED",1))
        self.assertEqual((call_status["status"],call_status["error_code"]), ("INTERRUPTED","process_interrupted"))

    def test_preview_is_separate_verified_cached_artifact(self):
        config = {"voice": "vi-VN-NamMinhNeural", "rate_percent": 0}
        created = self.tts.create_preview(self.project["id"], "Câu nghe thử tổng hợp.", config, True, "tts-preview-key-00001")
        self.assertTrue(self.tts_worker.run_once())
        result = self.tts.preview_result(self.project["id"], created["job"]["id"])
        self.assertEqual(result["job"]["status"], "READY")
        self.assertIsNotNone(result["artifact_id"])
        stream = self.tts.open_audio(self.project["id"], result["artifact_id"])
        stream[0].close()
        cached = self.tts.create_preview(self.project["id"], "Câu nghe thử tổng hợp.", config, True, "tts-preview-key-00002")
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["artifact_id"], result["artifact_id"])

    def test_session_csrf_protection_and_http_range_playback(self):
        from fastapi.testclient import TestClient
        accepted = self.translation_revisions.editor_state(self.project["id"], self.translation_run_id)["accepted"]
        app = create_app("synthetic-bootstrap", data_root=self.db.data_root)
        app.state.tts_service.adapter = self.tts_adapter
        app.state.tts_worker.adapter = self.tts_adapter
        origin = "http://127.0.0.1:5173"
        base_headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
        with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            no_session = client.get("/api/v1/providers/edge-tts/voices", headers=base_headers)
            self.assertEqual(no_session.status_code, 401)
            boot = client.post("/api/v1/session/bootstrap", headers={**base_headers, "Content-Type": "application/json", "X-Local-Bootstrap": "synthetic-bootstrap"}, json={})
            self.assertEqual(boot.status_code, 200)
            csrf = boot.json()["csrf_token"]
            payload = {"translation_revision_id": accepted["id"], "voice": "vi-VN-NamMinhNeural", "consent_confirmed": True}
            denied = client.post(f"/api/v1/projects/{self.project['id']}/tts/jobs", headers={**base_headers, "Content-Type": "application/json", "Idempotency-Key": "tts-api-no-csrf-0001"}, json=payload)
            self.assertEqual(denied.status_code, 403)
            valid = client.post(f"/api/v1/projects/{self.project['id']}/tts/jobs", headers={**base_headers, "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": "tts-api-valid-key-001"}, json=payload)
            self.assertEqual(valid.status_code, 202)
            run_id = valid.json()["run_id"]
            deadline = time.monotonic() + 15
            body = None
            while time.monotonic() < deadline:
                detail = client.get(f"/api/v1/projects/{self.project['id']}/tts/runs/{run_id}", headers=base_headers)
                self.assertEqual(detail.status_code, 200)
                body = detail.json()
                if body["status"] in {"READY", "FAILED", "PARTIAL"}:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(body)
            if body["status"] != "READY":
                with closing(self.db.connect()) as connection:
                    diagnostics = [dict(row) for row in connection.execute("SELECT call_kind,round_no,call_no,status,error_code,exception_class,http_status FROM tts_provider_calls WHERE tts_run_id=?", (run_id,)).fetchall()]
                self.fail(f"TTS run did not complete: status={body['status']}; safe diagnostics={diagnostics}")
            unit_id = body["units"][0]["unit_id"]
            url = f"/api/v1/projects/{self.project['id']}/tts/runs/{run_id}/units/{unit_id}/audio"
            ranged = client.get(url, headers={**base_headers, "Range": "bytes=0-63"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(len(ranged.content), 64)
            self.assertTrue(ranged.headers["Content-Range"].startswith("bytes 0-63/"))


class TTSAdapterContractTests(unittest.TestCase):
    def test_request_pacer_spaces_calls_and_checks_cancellation(self):
        pacer = _RequestPacer()
        with patch("app.tts.TTS_REQUEST_INTERVAL_SECONDS", 0.03):
            pacer.wait_turn(lambda: False)
            started = time.monotonic()
            pacer.wait_turn(lambda: False)
            self.assertGreaterEqual(time.monotonic() - started, 0.02)
            with self.assertRaises(TTSError) as caught:
                pacer.wait_turn(lambda: True)
        self.assertEqual(caught.exception.code, "job_cancelled")

    def test_text_integrity_rejects_lossy_transform_and_preserves_unicode(self):
        self.assertEqual(EdgeTTSAdapter._validate_text_integrity("Xin chào — tiếng Việt."), "Xin chào — tiếng Việt.")
        self.assertEqual(EdgeTTSAdapter._validate_text_integrity("câu\x00nối"), "câu nối")

    def test_long_text_integrity_check_does_not_exhaust_provider_chunk_generator(self):
        class FakeCommunicate:
            def __init__(self, text, *_args, **_kwargs):
                self.texts = (part.encode("utf-8") for part in [escape(text)])
            async def save(self, filename):
                chunks = list(self.texts)
                Path(filename).write_bytes(b"".join(chunks))

        adapter = EdgeTTSAdapter()
        with tempfile.TemporaryDirectory() as directory, patch("app.tts.edge_tts.Communicate", FakeCommunicate):
            target = Path(directory) / "adapter.mp3"
            adapter.synthesize("Xin chào & cảm ơn", target, voice="vi-VN-NamMinhNeural", rate=0, pitch_hz=0, volume=0)
            self.assertGreater(target.stat().st_size, 0)

    def test_schema_v18_and_v19_migrate_repeatably(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "data")
            database.initialize(target_version=17)
            database.initialize(target_version=18)
            database.initialize()
            database.initialize()
            with closing(database.connect()) as connection:
                version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                unit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tts_run_units)")}
                artifact_columns = {row["name"] for row in connection.execute("PRAGMA table_info(artifacts)")}
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertTrue({"retryable", "synthesis_fingerprint", "source_segment_ids_json"}.issubset(unit_columns))
            self.assertIn("content_fingerprint", artifact_columns)

