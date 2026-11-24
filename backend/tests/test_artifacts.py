import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts import ArtifactConflict, ArtifactError, ArtifactPublicationService
from app.main import create_app


class SimulatedCrash(BaseException):
    pass


class TestBlobValidator:
    version = "adtest-blob-v1"

    def validate(self, path):
        with path.open("rb") as stream:
            header = stream.readline(32)
            if header != b"ADTEST-v1\n":
                raise ArtifactError("test_blob_invalid")
            size = 0
            while chunk := stream.read(65536):
                size += len(chunk)
        return {"format": "ADTEST-v1", "payload_size": size}


class ArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="AutoDub-Cà phê-漢字-")
        self.data_root = Path(self.temp.name) / "dữ liệu"
        self.app = create_app("artifact-test-secret", data_root=self.data_root)
        self.client = TestClient(self.app, client=("127.0.0.1", 50200))
        self.client.__enter__()
        self.headers = {
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:5173",
            "sec-fetch-site": "same-origin",
        }
        boot = self.client.post(
            "/api/v1/session/bootstrap",
            headers={**self.headers, "content-type": "application/json", "x-local-bootstrap": "artifact-test-secret"},
            json={},
        )
        self.assertEqual(boot.status_code, 200)
        project = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "x-csrf-token": boot.json()["csrf_token"], "idempotency-key": "artifact-project-key-01"},
            json={"name": "Artifact test"},
        )
        self.assertEqual(project.status_code, 201)
        self.project_id = project.json()["id"]
        self.service = self.app.state.artifact_service
        self.service.validators["test_blob"] = TestBlobValidator()
        self.job = self.app.state.job_service.create_job(
            self.project_id, "artifact_test", {"schema_version": 1, "test": "fixture"}, "artifact-job-key-01"
        )
        self.claimed = self.app.state.job_service.claim_next_job()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def publish(self, source=b"ADTEST-v1\nfixture payload", service=None, **kwargs):
        return (service or self.service).publish(
            project_id=self.project_id,
            job_id=self.job["id"],
            attempt_id=self.claimed["attempt"]["id"],
            kind="test_blob",
            input_fingerprint=self.job["input_fingerprint"],
            fingerprint_version=1,
            source=source,
            **kwargs,
        )

    def test_valid_stream_publication_and_restart_integrity(self):
        chunks = [b"ADTEST-v1\n", b"fixture ", b"payload"]
        artifact = self.publish(source=iter(chunks))
        self.assertEqual(artifact["status"], "READY")
        self.assertEqual(artifact["sha256"], hashlib.sha256(b"".join(chunks)).hexdigest())
        self.assertEqual(artifact["file_size"], len(b"".join(chunks)))
        self.assertEqual(artifact["verification_metadata"], {"format": "ADTEST-v1", "payload_size": 15})
        reopened = ArtifactPublicationService(self.app.state.database, {"test_blob": TestBlobValidator()})
        self.assertEqual(reopened.verify_ready(artifact["id"])["status"], "READY")

    def test_validator_failure_leaves_failed_not_ready(self):
        with self.assertRaisesRegex(ArtifactError, "validator_failed|test_blob_invalid"):
            self.publish(source=b"not an artifact")
        with closing(self.app.state.database.connect()) as connection:
            row = connection.execute("SELECT status,error_code FROM artifacts").fetchone()
        self.assertEqual(row["status"], "FAILED")

    def test_no_overwrite_destination_collision(self):
        artifact_id = str(uuid.uuid4())
        service = ArtifactPublicationService(
            self.app.state.database,
            {"test_blob": TestBlobValidator()},
            id_factory=lambda: uuid.UUID(artifact_id),
        )
        relative = f"projects/{self.project_id}/artifacts/test_blob/{artifact_id}.artifact"
        destination = service._path(relative, ensure_parent=True)
        destination.write_bytes(b"sentinel")
        with self.assertRaisesRegex(ArtifactConflict, "destination_exists"):
            self.publish(service=service)
        self.assertEqual(destination.read_bytes(), b"sentinel")
        with closing(self.app.state.database.connect()) as connection:
            status = connection.execute("SELECT status FROM artifacts WHERE id=?", (artifact_id,)).fetchone()[0]
        self.assertEqual(status, "VERIFIED")

    def test_concurrent_publications_keep_distinct_immutable_results(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: self.publish(), range(2)))
        self.assertEqual(len({item["id"] for item in results}), 2)
        self.assertTrue(all(item["status"] == "READY" for item in results))
        self.assertEqual(len({item["managed_relative_path"] for item in results}), 2)

    def test_commit_failure_after_publish_remains_verified_and_not_ready(self):
        service = ArtifactPublicationService(
            self.app.state.database,
            {"test_blob": TestBlobValidator()},
            fault_hook=lambda phase: (_ for _ in ()).throw(sqlite3.OperationalError("injected commit failure"))
            if phase == "before_ready_commit" else None,
        )
        with self.assertRaisesRegex(ArtifactError, "publication_commit_failed"):
            self.publish(service=service)
        with closing(self.app.state.database.connect()) as connection:
            row = connection.execute("SELECT status,managed_relative_path FROM artifacts").fetchone()
        self.assertEqual(row["status"], "VERIFIED")
        self.assertTrue((self.data_root / Path(row["managed_relative_path"])).is_file())

    def test_crash_injection_states_are_never_false_ready(self):
        expected = {
            "after_manifest": "STAGING",
            "after_stage_flush": "STAGING",
            "after_verified": "VERIFIED",
            "after_publish": "VERIFIED",
            "before_ready_commit": "VERIFIED",
            "after_ready_commit": "READY",
        }
        for phase, expected_status in expected.items():
            with self.subTest(phase=phase):
                artifact_id = str(uuid.uuid4())
                service = ArtifactPublicationService(
                    self.app.state.database,
                    {"test_blob": TestBlobValidator()},
                    id_factory=lambda artifact_id=artifact_id: uuid.UUID(artifact_id),
                    fault_hook=lambda observed, target=phase: (_ for _ in ()).throw(SimulatedCrash()) if observed == target else None,
                )
                with self.assertRaises(SimulatedCrash):
                    self.publish(service=service)
                artifact = service.get_artifact(artifact_id)
                self.assertEqual(artifact["status"], expected_status)

    def test_missing_and_modified_ready_artifacts_are_not_reusable(self):
        artifact = self.publish()
        ready_path = self.data_root / Path(artifact["managed_relative_path"])
        ready_path.unlink()
        with self.assertRaisesRegex(ArtifactConflict, "artifact_missing"):
            self.service.verify_ready(artifact["id"])
        self.assertEqual(self.service.get_artifact(artifact["id"])["status"], "MISSING")

        another = self.publish()
        path = self.data_root / Path(another["managed_relative_path"])
        path.write_bytes(b"ADTEST-v1\nchanged")
        with self.assertRaisesRegex(ArtifactConflict, "artifact_corrupt"):
            self.service.verify_ready(another["id"])
        self.assertEqual(self.service.get_artifact(another["id"])["status"], "CORRUPT")

    def test_canceled_job_retains_ready_artifact_without_activation(self):
        artifact = self.publish()
        jobs = self.app.state.job_service
        jobs.request_cancel(self.job["id"], "artifact-cancel-key-01")
        self.assertEqual(artifact["status"], "READY")
        self.assertEqual(self.service.get_artifact(artifact["id"])["status"], "READY")
        with closing(self.app.state.database.connect()) as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (self.project_id,)).fetchone()
            self.assertNotIn("active_artifact_id", project.keys())

    def test_failed_attempt_retains_artifact_but_cannot_reuse_it_for_retry(self):
        artifact = self.publish()
        jobs = self.app.state.job_service
        jobs.fail_attempt(self.job["id"], self.claimed["attempt"]["id"], "fixture_failed", retryable=True)
        self.assertEqual(self.service.get_artifact(artifact["id"])["status"], "READY")
        retried = jobs.retry_job(self.job["id"], "FAILED", "artifact-stale-retry-key-01")
        next_attempt = jobs.claim_next_job()
        self.assertEqual(next_attempt["id"], retried["id"])
        with self.assertRaisesRegex(ArtifactConflict, "producer_not_running"):
            self.service.completion_evidence(self.job["id"], self.claimed["attempt"]["id"], [artifact["id"]])
        self.assertEqual(self.service.get_artifact(artifact["id"])["status"], "READY")

    def test_cross_project_and_non_running_producer_are_rejected(self):
        # The API only exposes no Job creation endpoint, so establish a distinct valid owner directly via the internal DB service.
        second, _ = self.app.state.database.create_project("Other", "artifact-other-project-key-01")
        with self.assertRaises(ArtifactError):
            self.service.publish(
                project_id=second["id"], job_id=self.job["id"], attempt_id=self.claimed["attempt"]["id"],
                kind="test_blob", input_fingerprint=self.job["input_fingerprint"], fingerprint_version=1,
                source=b"ADTEST-v1\nfixture",
            )
        self.app.state.job_service.fail_attempt(
            self.job["id"], self.claimed["attempt"]["id"], "fixture_failed", retryable=False
        )
        with self.assertRaisesRegex(ArtifactConflict, "producer_not_running"):
            self.publish()

    def test_migration_v8_is_additive_and_repeatable(self):
        with closing(self.app.state.database.connect()) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 12)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_processing_sources'").fetchone())
        self.app.state.database.initialize()
        with closing(self.app.state.database.connect()) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 12)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_processing_sources'").fetchone())


if __name__ == "__main__":
    unittest.main()
