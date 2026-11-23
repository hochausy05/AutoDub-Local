import tempfile
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

from app.database import Database, IdempotencyConflict
from app.jobs import CompletionEvidence, JobConflict, JobInputError, JobNotFound, JobService
from app.main import create_app


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name) / "data"
        self.app = create_app("jobs-test-secret", data_root=self.data_root)
        self.client = TestClient(self.app, client=("127.0.0.1", 50100))
        self.client.__enter__()
        self.headers = {
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:5173",
            "sec-fetch-site": "same-origin",
        }
        bootstrap = self.client.post(
            "/api/v1/session/bootstrap",
            headers={**self.headers, "content-type": "application/json", "x-local-bootstrap": "jobs-test-secret"},
            json={},
        )
        self.assertEqual(bootstrap.status_code, 200)
        self.csrf = bootstrap.json()["csrf_token"]
        project = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "x-csrf-token": self.csrf, "idempotency-key": "jobs-project-idem-key-01"},
            json={"name": "Job test project"},
        )
        self.assertEqual(project.status_code, 201)
        self.project_id = project.json()["id"]
        self.service: JobService = self.app.state.job_service

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def create_job(self, key="job-create-idempotency-01", snapshot=None):
        return self.service.create_job(
            self.project_id,
            "test_controlled",
            snapshot or {"schema_version": 1, "test_case": "controlled_lifecycle"},
            key,
        )

    def claim(self, job):
        claimed = self.service.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job["id"])
        return claimed

    @staticmethod
    def evidence():
        # This test-only executor kind is not an ASR/TTS/render success claim.
        return CompletionEvidence(True, True, "controlled_test_executor_verified")

    def test_migration_v2_preserves_core_01a_project_data(self):
        legacy_root = Path(self.temp.name) / "legacy"
        legacy = Database(legacy_root)
        legacy.initialize(target_version=1)
        project, _ = legacy.create_project("Pre-v2 project", "legacy-project-idem-key-01")
        with closing(legacy.connect()) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 1)
        legacy.initialize()
        with closing(legacy.connect()) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects WHERE id=?", (project["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        legacy.initialize()
        with closing(legacy.connect()) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)

    def test_job_and_running_attempt_survive_backend_restart_without_reconciliation(self):
        job = self.create_job()
        claimed = self.claim(job)
        attempt_id = claimed["attempt"]["id"]
        self.client.__exit__(None, None, None)
        reopened = TestClient(create_app("reopened-jobs-secret", data_root=self.data_root), client=("127.0.0.1", 50101))
        with reopened:
            headers = {**self.headers, "content-type": "application/json", "x-local-bootstrap": "reopened-jobs-secret"}
            self.assertEqual(reopened.post("/api/v1/session/bootstrap", headers=headers, json={}).status_code, 200)
            response = reopened.get(f"/api/v1/jobs/{job['id']}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "RUNNING")
            self.assertEqual(response.json()["attempt"]["id"], attempt_id)
            self.assertEqual(response.json()["attempt"]["status"], "RUNNING")
            self.assertNotIn("input_snapshot", response.json())

    def test_job_creation_idempotency_replays_same_job_and_conflicts_on_changed_payload(self):
        job = self.create_job()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replays = list(pool.map(lambda _index: self.create_job(snapshot={"test_case": "controlled_lifecycle", "schema_version": 1}), range(2)))
        self.assertEqual([item["id"] for item in replays], [job["id"], job["id"]])
        self.assertEqual(replays[0]["status"], "QUEUED")
        with self.assertRaises(IdempotencyConflict):
            self.create_job(snapshot={"schema_version": 1, "test_case": "different"})
        with closing(self.app.state.database.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE jobs SET input_snapshot='{}' WHERE id=?", (job["id"],))

    def test_concurrent_claimers_create_one_attempt_for_one_job(self):
        job = self.create_job()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: self.service.claim_next_job(), range(2)))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], job["id"])
        self.assertEqual(len(claimed[0]["attempts"]), 1)
        self.assertEqual(claimed[0]["attempt"]["attempt_no"], 1)
        self.assertIsNone(self.service.claim_next_job())

    def test_cancel_queued_job_without_attempt(self):
        job = self.create_job()
        cancelled = self.service.request_cancel(job["id"], "job-cancel-queued-key-01")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertTrue(cancelled["cancel_requested"])
        self.assertIsNone(cancelled["attempt"])

    def test_cancel_running_job_waits_for_attempt_stop_and_never_marks_ready(self):
        job = self.create_job()
        claimed = self.claim(job)
        pending = self.service.request_cancel(job["id"], "job-cancel-running-key-01")
        self.assertEqual(pending["status"], "CANCEL_REQUESTED")
        stopped = self.service.complete_attempt(job["id"], claimed["attempt"]["id"], self.evidence())
        self.assertEqual(stopped["status"], "CANCELLED")
        self.assertEqual(stopped["attempt"]["status"], "CANCELLED")

    def test_cancel_completion_race_has_one_transactional_winner(self):
        job = self.create_job()
        claimed = self.claim(job)
        gate = Barrier(2)

        def cancel():
            gate.wait()
            return self.service.request_cancel(job["id"], "job-race-cancel-key-01")

        def complete():
            gate.wait()
            return self.service.complete_attempt(job["id"], claimed["attempt"]["id"], self.evidence())

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            complete_future = pool.submit(complete)
            cancel_result = cancel_future.result()
            complete_result = complete_future.result()
        final = self.service.get_job(job["id"])
        self.assertIn(final["status"], {"READY", "CANCELLED"})
        if final["status"] == "READY":
            self.assertEqual(final["attempt"]["status"], "SUCCEEDED")
            self.assertEqual(cancel_result["status"], "READY")
        else:
            self.assertEqual(final["attempt"]["status"], "CANCELLED")
            self.assertNotEqual(complete_result["status"], "READY")

    def test_failure_retry_keeps_attempt_history_and_sanitizes_error(self):
        job = self.create_job()
        first = self.claim(job)
        failed = self.service.fail_attempt(job["id"], first["attempt"]["id"], "C:\\private\\traceback.txt", retryable=True)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error_code"], "worker_failed")
        self.assertTrue(failed["attempt"]["retryable"])
        queued = self.service.retry_job(job["id"], "FAILED", "job-retry-idempotency-key-01")
        replay = self.service.retry_job(job["id"], "FAILED", "job-retry-idempotency-key-01")
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(replay["id"], job["id"])
        second = self.claim(queued)
        self.assertEqual(len(second["attempts"]), 2)
        self.assertEqual(second["attempts"][0]["status"], "FAILED")
        self.assertEqual(second["attempts"][0]["error_code"], "worker_failed")
        self.assertEqual(second["attempts"][1]["attempt_no"], 2)

    def test_interrupted_job_can_be_retried_after_reconciliation_state_is_recorded(self):
        job = self.create_job()
        first = self.claim(job)
        stamp = first["attempt"]["started_at"]
        with closing(self.app.state.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE job_attempts SET status='INTERRUPTED',ended_at=?,retryable=1 WHERE id=?",
                (stamp, first["attempt"]["id"]),
            )
            connection.execute(
                "UPDATE jobs SET status='INTERRUPTED',lock_version=lock_version+1,updated_at=? WHERE id=?",
                (stamp, job["id"]),
            )
            connection.commit()
        retried = self.service.retry_job(job["id"], "INTERRUPTED", "job-interrupted-retry-key-01")
        self.assertEqual(retried["status"], "QUEUED")
        second = self.claim(retried)
        self.assertEqual([attempt["status"] for attempt in second["attempts"]], ["INTERRUPTED", "RUNNING"])

    def test_retry_requires_retryable_error_and_stops_at_attempt_limit(self):
        permanent = self.create_job(key="job-permanent-failure-key-01")
        permanent_attempt = self.claim(permanent)
        self.service.fail_attempt(permanent["id"], permanent_attempt["attempt"]["id"], "invalid_input", retryable=False)
        with self.assertRaisesRegex(JobConflict, "job_not_retryable"):
            self.service.retry_job(permanent["id"], "FAILED", "job-permanent-retry-key-01")

        bounded = self.create_job(key="job-retry-cap-create-key-01")
        for attempt_number in range(1, 4):
            claimed = self.claim(bounded)
            self.service.fail_attempt(bounded["id"], claimed["attempt"]["id"], "temporary_error", retryable=True)
            if attempt_number < 3:
                bounded = self.service.retry_job(
                    bounded["id"], "FAILED", f"job-retry-cap-key-{attempt_number:04d}"
                )
        with self.assertRaisesRegex(JobConflict, "retry_limit_reached"):
            self.service.retry_job(bounded["id"], "FAILED", "job-retry-cap-final-key-01")
        self.assertEqual(len(self.service.get_job(bounded["id"])["attempts"]), 3)

    def test_unverified_completion_and_invalid_transitions_are_rejected(self):
        job = self.create_job()
        with self.assertRaises(JobConflict):
            self.service.retry_job(job["id"], "FAILED", "invalid-retry-key-000001")
        with self.assertRaises(JobNotFound):
            self.service.complete_attempt(job["id"], "00000000-0000-4000-8000-000000000000", self.evidence())
        claimed = self.claim(job)
        with self.assertRaises(JobConflict):
            self.service.complete_attempt(job["id"], claimed["attempt"]["id"], CompletionEvidence(False, False, "not_verified"))
        self.assertEqual(self.service.get_job(job["id"])["status"], "RUNNING")

    def test_progress_is_bounded_and_reported_by_api_service(self):
        job = self.create_job()
        claimed = self.claim(job)
        result = self.service.update_attempt_progress(
            job["id"], claimed["attempt"]["id"], {"phase": "controlled", "completed_units": 1, "total_units": 3, "fraction": 0.33}
        )
        self.assertEqual(result["progress"]["total_units"], 3)
        with self.assertRaisesRegex(JobInputError, "invalid_progress"):
            self.service.update_attempt_progress(job["id"], claimed["attempt"]["id"], {"message": "private text"})

    def test_api_requires_session_csrf_and_hides_project_mismatch(self):
        job = self.create_job()
        session_cookie = self.client.cookies.get("autodub_session")
        self.client.cookies.clear()
        anonymous = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers)
        self.assertEqual(anonymous.status_code, 401)
        denied = self.client.post(
            f"/api/v1/jobs/{job['id']}/cancel",
            headers={**self.headers, "idempotency-key": "job-api-cancel-key-0001"},
            json={},
        )
        self.assertEqual(denied.status_code, 401)
        self.client.cookies.set("autodub_session", session_cookie)
        missing_csrf = self.client.post(
            f"/api/v1/jobs/{job['id']}/cancel",
            headers={**self.headers, "idempotency-key": "job-api-cancel-key-0001"},
            json={},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        other = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "x-csrf-token": self.csrf, "idempotency-key": "jobs-other-project-key-01"},
            json={"name": "Other project"},
        ).json()["id"]
        mismatch = self.client.get(f"/api/v1/jobs/{job['id']}?project_id={other}", headers=self.headers)
        self.assertEqual(mismatch.status_code, 404)
        self.assertEqual(self.client.post("/api/v1/jobs", headers=self.headers, json={}).status_code, 404)

    def test_job_cancel_and_retry_api_use_idempotency_and_error_envelope(self):
        queued = self.create_job()
        cancel = self.client.post(
            f"/api/v1/jobs/{queued['id']}/cancel",
            headers={**self.headers, "x-csrf-token": self.csrf, "idempotency-key": "job-api-cancel-key-0002"},
            json={},
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["status"], "CANCELLED")

        failed_job = self.create_job(key="job-create-for-api-retry-01")
        claimed = self.claim(failed_job)
        self.service.fail_attempt(failed_job["id"], claimed["attempt"]["id"], "provider_unavailable", retryable=True)
        retry_headers = {**self.headers, "x-csrf-token": self.csrf, "idempotency-key": "job-api-retry-idempotency-01"}
        retried = self.client.post(
            f"/api/v1/jobs/{failed_job['id']}/retry",
            headers=retry_headers,
            json={"expected_status": "FAILED"},
        )
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()["status"], "QUEUED")
        replay = self.client.post(
            f"/api/v1/jobs/{failed_job['id']}/retry",
            headers=retry_headers,
            json={"expected_status": "FAILED"},
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["id"], failed_job["id"])
        conflict = self.client.post(
            f"/api/v1/jobs/{failed_job['id']}/retry",
            headers={**self.headers, "x-csrf-token": self.csrf, "idempotency-key": "job-api-retry-conflict-01"},
            json={"expected_status": "FAILED"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "job_status_conflict")


if __name__ == "__main__":
    unittest.main()
