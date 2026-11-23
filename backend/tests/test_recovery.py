import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts import ArtifactConflict, ArtifactError, ArtifactPublicationService
from app.database import Database
from app.jobs import JobConflict
from app.main import create_app


class SimulatedCrash(BaseException):
    pass


class TestBlobValidator:
    version = "recovery-fixture-v1"

    def validate(self, path):
        with path.open("rb") as stream:
            if stream.readline(32) != b"ADTEST-v1\n":
                raise ArtifactError("test_blob_invalid")
            size = 0
            while chunk := stream.read(65536):
                size += len(chunk)
        return {"format": "ADTEST-v1", "payload_size": size}


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="AutoDub-Recovery-Cà phê-")
        self.data_root = Path(self.temp.name) / "data"
        self.app = self._new_app("recovery-test-secret")
        self.client = TestClient(self.app, client=("127.0.0.1", 50300))
        self.client.__enter__()
        self.app = self.client.app
        self.project_id = self.app.state.database.create_project("Recovery project", "recovery-project-key-0001")[0]["id"]

    def _new_app(self, secret):
        app = create_app(secret, data_root=self.data_root)
        app.state.artifact_service.validators["test_blob"] = TestBlobValidator()
        return app

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def restart(self):
        self.client.__exit__(None, None, None)
        self.client = TestClient(self._new_app("reopened-recovery-secret"), client=("127.0.0.1", 50301))
        self.client.__enter__()
        self.app = self.client.app
        return self.app

    def create_job(self, key=None, snapshot=None, stage=None):
        return self.app.state.job_service.create_job(
            self.project_id,
            "recovery_test",
            snapshot or {"schema_version": 1, "case": str(uuid.uuid4())},
            key or f"recovery-job-key-{uuid.uuid4().hex}",
            activation_stage=stage,
        )

    def claim(self, job):
        claimed = self.app.state.job_service.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job["id"])
        return claimed

    def publish(self, job, attempt, source=b"ADTEST-v1\nrecovery fixture", *, fault_hook=None):
        service = self.app.state.artifact_service
        if fault_hook:
            service = ArtifactPublicationService(
                self.app.state.database, {"test_blob": TestBlobValidator()}, fault_hook=fault_hook
            )
        return service.publish(
            project_id=self.project_id,
            job_id=job["id"],
            attempt_id=attempt["attempt"]["id"],
            kind="test_blob",
            input_fingerprint=job["input_fingerprint"],
            fingerprint_version=1,
            source=source,
        )

    def evidence(self, job, attempt, artifact):
        return self.app.state.artifact_service.completion_evidence(
            job["id"], attempt["attempt"]["id"], [artifact["id"]]
        )

    def test_restart_interrupts_running_and_cancel_requested_jobs_idempotently(self):
        running_job = self.create_job()
        self.claim(running_job)
        app = self.restart()
        running = app.state.job_service.get_job(running_job["id"])
        self.assertEqual((running["status"], running["attempt"]["status"]), ("INTERRUPTED", "INTERRUPTED"))
        self.assertTrue(running["attempt"]["retryable"])

        first_count = len(running["attempts"])
        second_report = app.state.startup_recovery.reconcile()
        again = app.state.job_service.get_job(running_job["id"])
        self.assertEqual(len(again["attempts"]), first_count)
        self.assertEqual(second_report["jobs_interrupted"], 0)
        retry = app.state.job_service.retry_job(running_job["id"], "INTERRUPTED", "recovery-retry-idem-key-01")
        replay = app.state.job_service.retry_job(running_job["id"], "INTERRUPTED", "recovery-retry-idem-key-01")
        self.assertEqual(retry["status"], "QUEUED")
        self.assertEqual(replay["id"], running_job["id"])
        claimed_again = app.state.job_service.claim_next_job()
        self.assertEqual(claimed_again["attempt"]["attempt_no"], 2)
        app.state.job_service.fail_attempt(running_job["id"], claimed_again["attempt"]["id"], "test_finished", retryable=False)

        cancel_job = self.create_job()
        self.claim(cancel_job)
        self.app.state.job_service.request_cancel(cancel_job["id"], "recovery-cancel-key-000001")
        app = self.restart()
        canceled = app.state.job_service.get_job(cancel_job["id"])
        self.assertEqual((canceled["status"], canceled["attempt"]["status"]), ("INTERRUPTED", "INTERRUPTED"))
        self.assertNotEqual(canceled["status"], "READY")

    def test_claims_are_blocked_until_startup_recovery(self):
        unstarted = self.create_job()
        claimed = self.claim(unstarted)
        self.create_job(key="single-supervisor-queued-job-01")
        self.assertIsNone(self.app.state.job_service.claim_next_job())
        service = type(self.app.state.job_service)(self.app.state.database, self.app.state.artifact_service)
        with self.assertRaisesRegex(JobConflict, "startup_recovery_required"):
            service.claim_next_job()
        self.assertEqual(claimed["status"], "RUNNING")

    def test_verified_and_renamed_candidates_recover_without_false_job_success(self):
        for phase, expected_before in (("after_stage_flush", "STAGING"), ("after_verified", "VERIFIED"), ("after_publish", "VERIFIED")):
            with self.subTest(phase=phase):
                job = self.create_job()
                attempt = self.claim(job)
                def crash(observed, target=phase):
                    if observed == target:
                        raise SimulatedCrash
                with self.assertRaises(SimulatedCrash):
                    self.publish(job, attempt, fault_hook=crash)
                with closing(self.app.state.database.connect()) as connection:
                    artifact_before = connection.execute("SELECT id,status FROM artifacts ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
                self.assertEqual(artifact_before["status"], expected_before)
                app = self.restart()
                self.assertEqual(app.state.artifact_service.get_artifact(artifact_before["id"])["status"], "READY")
                self.assertEqual(app.state.job_service.get_job(job["id"])["status"], "INTERRUPTED")
                self.assertEqual(app.state.artifact_service.verify_ready(artifact_before["id"])["status"], "READY")

    def test_manifestless_orphan_is_inventoried_and_never_deleted(self):
        orphan = self.data_root / "projects" / self.project_id / "artifacts" / "test_blob" / "manual-orphan.bin"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"unclaimed bytes")
        first = self.app.state.startup_recovery.reconcile()
        second = self.app.state.startup_recovery.reconcile()
        self.assertEqual(orphan.read_bytes(), b"unclaimed bytes")
        self.assertGreaterEqual(first["open_findings"], 1)
        self.assertEqual(first["open_findings"], second["open_findings"])
        with closing(self.app.state.database.connect()) as connection:
            finding = connection.execute(
                "SELECT finding_code,status FROM recovery_findings WHERE relative_path=?", (orphan.relative_to(self.data_root).as_posix(),)
            ).fetchone()
        self.assertEqual((finding["finding_code"], finding["status"]), ("orphan_file", "OPEN"))

    def test_ready_artifact_lost_on_restart_becomes_missing_project_survives(self):
        job = self.create_job()
        attempt = self.claim(job)
        artifact = self.publish(job, attempt)
        self.app.state.job_service.complete_attempt(job["id"], attempt["attempt"]["id"], self.evidence(job, attempt, artifact))
        final_path = self.data_root / Path(artifact["managed_relative_path"])
        final_path.unlink()
        app = self.restart()
        self.assertEqual(app.state.artifact_service.get_artifact(artifact["id"])["status"], "MISSING")
        self.assertIsNotNone(app.state.database.get_project(self.project_id))

    def test_valid_ready_job_and_artifact_remain_terminal_after_restart(self):
        job = self.create_job()
        attempt = self.claim(job)
        artifact = self.publish(job, attempt)
        self.app.state.job_service.complete_attempt(job["id"], attempt["attempt"]["id"], self.evidence(job, attempt, artifact))
        app = self.restart()
        self.assertEqual(app.state.job_service.get_job(job["id"])["status"], "READY")
        self.assertEqual(app.state.job_service.get_job(job["id"])["attempt"]["status"], "SUCCEEDED")
        self.assertEqual(app.state.artifact_service.verify_ready(artifact["id"])["status"], "READY")

    def test_startup_fails_closed_when_ready_artifact_has_no_registered_validator(self):
        job = self.create_job()
        attempt = self.claim(job)
        artifact = self.publish(job, attempt)
        self.app.state.job_service.complete_attempt(job["id"], attempt["attempt"]["id"], self.evidence(job, attempt, artifact))
        unsupported = create_app("unsupported-validator-secret", data_root=self.data_root)
        unsupported.state.database.initialize()
        with self.assertRaisesRegex(RuntimeError, "cannot validate an existing READY artifact"):
            unsupported.state.startup_recovery.reconcile()
        self.assertFalse(unsupported.state.job_service._recovery_complete)

    def test_stage_activation_uses_input_and_pointer_cas_and_keeps_stale_artifact(self):
        jobs = self.app.state.job_service
        snapshot_b = {"schema_version": 1, "dependency": "B"}
        jobs.set_stage_input(self.project_id, "translation", snapshot_b)
        job_b = self.create_job(key="stage-job-b-idempotency-01", snapshot=snapshot_b, stage="translation")
        attempt_b = self.claim(job_b)
        artifact_b = self.publish(job_b, attempt_b)
        completed_b = jobs.complete_attempt(job_b["id"], attempt_b["attempt"]["id"], self.evidence(job_b, attempt_b, artifact_b))
        self.assertEqual(completed_b["attempt"]["activation_outcome"], "ACTIVE")

        snapshot_a = {"schema_version": 1, "dependency": "A"}
        jobs.set_stage_input(self.project_id, "translation", snapshot_a)
        job_a = self.create_job(key="stage-job-a-idempotency-01", snapshot=snapshot_a, stage="translation")
        attempt_a = self.claim(job_a)
        jobs.set_stage_input(self.project_id, "translation", snapshot_b)
        unchanged_b = jobs.set_stage_input(self.project_id, "translation", snapshot_b)
        self.assertEqual(unchanged_b["input_generation"], 3)
        artifact_a = self.publish(job_a, attempt_a)
        completed_a = jobs.complete_attempt(job_a["id"], attempt_a["attempt"]["id"], self.evidence(job_a, attempt_a, artifact_a))
        self.assertEqual(completed_a["status"], "READY")
        self.assertEqual(completed_a["attempt"]["activation_outcome"], "STALE")
        self.assertEqual(self.app.state.artifact_service.get_artifact(artifact_a["id"])["status"], "READY")
        with closing(self.app.state.database.connect()) as connection:
            pointer = connection.execute("SELECT active_artifact_id,pointer_generation FROM stage_pointers WHERE project_id=? AND stage='translation'", (self.project_id,)).fetchone()
        self.assertEqual(pointer["active_artifact_id"], artifact_b["id"])
        self.assertEqual(pointer["pointer_generation"], 1)
        (self.data_root / Path(artifact_b["managed_relative_path"])).unlink()
        with self.assertRaisesRegex(ArtifactConflict, "artifact_missing"):
            self.app.state.artifact_service.verify_ready(artifact_b["id"])
        with closing(self.app.state.database.connect()) as connection:
            pointer = connection.execute("SELECT active_artifact_id,pointer_generation FROM stage_pointers WHERE project_id=? AND stage='translation'", (self.project_id,)).fetchone()
        self.assertIsNone(pointer["active_artifact_id"])
        self.assertEqual(pointer["pointer_generation"], 2)

    def test_cancel_winning_stage_job_never_activates_verified_artifact(self):
        snapshot = {"schema_version": 1, "dependency": "cancelled"}
        jobs = self.app.state.job_service
        jobs.set_stage_input(self.project_id, "tts", snapshot)
        job = self.create_job(key="cancel-stage-job-idempotency-01", snapshot=snapshot, stage="tts")
        attempt = self.claim(job)
        artifact = self.publish(job, attempt)
        jobs.request_cancel(job["id"], "cancel-stage-request-key-0001")
        canceled = jobs.complete_attempt(job["id"], attempt["attempt"]["id"], self.evidence(job, attempt, artifact))
        self.assertEqual(canceled["status"], "CANCELLED")
        self.assertEqual(canceled["attempt"]["activation_outcome"], "CANCELLED")
        with closing(self.app.state.database.connect()) as connection:
            pointer = connection.execute("SELECT active_artifact_id,pointer_generation FROM stage_pointers WHERE project_id=? AND stage='tts'", (self.project_id,)).fetchone()
        self.assertIsNone(pointer["active_artifact_id"])
        self.assertEqual(pointer["pointer_generation"], 0)
        self.assertEqual(self.app.state.artifact_service.get_artifact(artifact["id"])["status"], "READY")

    def test_retry_rejects_stage_job_after_its_input_generation_changes(self):
        jobs = self.app.state.job_service
        snapshot_a = {"schema_version": 1, "dependency": "retry-A"}
        jobs.set_stage_input(self.project_id, "asr", snapshot_a)
        job = self.create_job(key="stale-retry-stage-job-0001", snapshot=snapshot_a, stage="asr")
        attempt = self.claim(job)
        jobs.fail_attempt(job["id"], attempt["attempt"]["id"], "provider_failed", retryable=True)
        jobs.set_stage_input(self.project_id, "asr", {"schema_version": 1, "dependency": "retry-B"})
        with self.assertRaisesRegex(JobConflict, "job_stale"):
            jobs.retry_job(job["id"], "FAILED", "stale-stage-retry-idem-01")

    @unittest.skipUnless(os.name == "nt", "Actual Windows TerminateProcess test requires Windows.")
    def test_windows_worker_process_kill_after_rename_recovers_on_restart(self):
        marker = Path(self.temp.name) / "published.marker"
        child = r'''
import sys,time
from pathlib import Path
from app.database import Database
from app.artifacts import ArtifactPublicationService
from app.jobs import JobService
from app.recovery import StartupRecovery
class V:
    version="recovery-fixture-v1"
    def validate(self,path):
        data=path.read_bytes()
        if not data.startswith(b"ADTEST-v1\n"): raise ValueError("bad")
        return {"format":"ADTEST-v1","payload_size":len(data)-10}
db=Database(sys.argv[1]); db.initialize()
art=ArtifactPublicationService(db,{"test_blob":V()})
jobs=JobService(db,art); StartupRecovery(db,jobs,art).reconcile()
job=jobs.create_job(sys.argv[2],"hard_crash_test",{"schema_version":1},"hard-crash-job-idem-key-01")
attempt=jobs.claim_next_job()
def hold(phase):
    if phase=="after_publish":
        Path(sys.argv[3]).write_text("renamed",encoding="utf-8")
        while True: time.sleep(0.1)
art.fault_hook=hold
art.publish(project_id=sys.argv[2],job_id=job["id"],attempt_id=attempt["attempt"]["id"],kind="test_blob",input_fingerprint=job["input_fingerprint"],fingerprint_version=1,source=b"ADTEST-v1\nsubprocess")
'''
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(self.data_root), self.project_id, str(marker)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not marker.exists() and process.poll() is None:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "subprocess never reached post-rename crash point")
        process.kill()
        process.wait(timeout=10)
        self.assertIsNotNone(process.returncode)
        app = self.restart()
        with closing(app.state.database.connect()) as connection:
            job = connection.execute("SELECT id,status FROM jobs WHERE kind='hard_crash_test'").fetchone()
            artifact = connection.execute("SELECT id,status,managed_relative_path FROM artifacts WHERE producer_job_id=?", (job["id"],)).fetchone()
        self.assertEqual(job["status"], "INTERRUPTED")
        self.assertEqual(artifact["status"], "READY")
        self.assertTrue((self.data_root / Path(artifact["managed_relative_path"])).is_file())


if __name__ == "__main__":
    unittest.main()
