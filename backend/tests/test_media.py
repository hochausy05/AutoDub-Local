from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts import ArtifactPublicationService
from app.database import Database
from app.jobs import JobService
from app.media import FFprobe, LinkManifestValidator, MediaError, MediaImportService, SourceVideoValidator, _path_identity, parse_single_byte_range
from app.main import create_app
from app.recovery import StartupRecovery


class MediaImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "data")
        self.db.initialize(target_version=4)
        self.db.initialize()
        self.project, _ = self.db.create_project("Media Unicode — 試験", "project-media-idem-0001")
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = FFprobe()
        if not self.ffmpeg or not self.ffprobe.executable:
            self.skipTest("ffmpeg/ffprobe unavailable")
        self.video = self.root / "vídeo-日本語.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10", "-t", "1", "-an", "-c:v", "mpeg4", "-y", str(self.video)], check=True, timeout=30)
        self.artifacts = ArtifactPublicationService(self.db, {"source_video": SourceVideoValidator(self.ffprobe), "media_link_manifest": LinkManifestValidator()})
        self.jobs = JobService(self.db, self.artifacts)
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        self.service = MediaImportService(self.db, self.jobs, self.artifacts, self.ffprobe)
        self.session = "test-local-session-token"

    def tearDown(self):
        self.temp.cleanup()

    def issue_selection(self, video=None, *, expires=600):
        path = video or self.video
        token = uuid.uuid4().hex + uuid.uuid4().hex
        identity = _path_identity(path)
        with closing(self.db.connect()) as connection:
            connection.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+?,'ISSUED','now')", (hashlib.sha256(token.encode()).hexdigest(), self.service.session_hash(self.session), self.project["id"], str(path), path.name, json.dumps(identity), expires))
        return token

    def test_ffprobe_reads_real_encoded_unicode_file_without_audio(self):
        metadata = self.ffprobe.probe(self.video)
        self.assertEqual((metadata["width"], metadata["height"]), (64, 48))
        self.assertGreater(metadata["duration_us"], 0)
        self.assertEqual(metadata["audio_streams"], 0)
        self.assertEqual(metadata["video_codec"], "mpeg4")

    def test_copy_import_publishes_verified_asset_and_keeps_original(self):
        expected = hashlib.sha256(self.video.read_bytes()).hexdigest()
        selection = self.issue_selection()
        job = self.service.create_import_job(self.session, self.project["id"], selection, "COPY", "copy-media-idempotency-key-01")
        self.assertEqual(job["status"], "QUEUED")
        self.assertTrue(self.service.import_once())
        assets = self.service.list_assets(self.project["id"])
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["mode"], "COPY")
        self.assertEqual(assets[0]["sha256"], expected)
        self.assertEqual(assets[0]["metadata"]["audio_streams"], 0)
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), expected)

    def test_link_import_keeps_external_source_and_replays_idempotently(self):
        expected = hashlib.sha256(self.video.read_bytes()).hexdigest()
        selection = self.issue_selection()
        key = "link-media-idempotency-key-01"
        first = self.service.create_import_job(self.session, self.project["id"], selection, "LINK", key)
        replay = self.service.create_import_job(self.session, self.project["id"], selection, "LINK", key)
        self.assertEqual(first["id"], replay["id"])
        with closing(self.db.connect()) as connection:
            snapshot = json.loads(connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (first["id"],)).fetchone()["input_snapshot"])
        self.assertIn("selection_ref", snapshot)
        self.assertNotIn("source_path", snapshot)
        self.service.import_once()
        asset = self.service.list_assets(self.project["id"])[0]
        self.assertEqual(asset["mode"], "LINK")
        self.assertEqual(asset["sha256"], expected)
        self.assertEqual(self.video.exists(), True)
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT private_source_path,managed_artifact_id FROM media_assets WHERE id=?", (asset["id"],)).fetchone()
            artifact = connection.execute("SELECT kind FROM artifacts WHERE id=?", (row["managed_artifact_id"],)).fetchone()
        self.assertEqual(row["private_source_path"], str(self.video))
        self.assertEqual(artifact["kind"], "media_link_manifest")

    def test_selection_is_bound_to_session_and_consumed_once(self):
        token = self.issue_selection()
        with self.assertRaises(MediaError):
            self.service.create_import_job("other-session", self.project["id"], token, "COPY", "wrong-session-idempotency-key")
        self.service.create_import_job(self.session, self.project["id"], token, "COPY", "selection-consume-idempotency-01")
        with self.assertRaises(MediaError):
            self.service.create_import_job(self.session, self.project["id"], token, "COPY", "selection-second-consume-0001")

    def test_changed_source_fails_before_job_creation(self):
        token = self.issue_selection()
        self.video.write_bytes(self.video.read_bytes() + b"changed")
        with self.assertRaisesRegex(MediaError, "source_changed"):
            self.service.create_import_job(self.session, self.project["id"], token, "COPY", "changed-source-idempotency-01")

    def test_expired_selection_and_cross_project_use_are_rejected(self):
        expired = self.issue_selection(expires=-1)
        with self.assertRaisesRegex(MediaError, "selection_expired"):
            self.service.create_import_job(self.session, self.project["id"], expired, "COPY", "expired-selection-idempotency-01")
        other, _ = self.db.create_project("Other Project", "project-media-idem-0002")
        token = self.issue_selection()
        with self.assertRaisesRegex(MediaError, "selection_invalid"):
            self.service.create_import_job(self.session, other["id"], token, "COPY", "cross-project-idempotency-key-01")

    def test_source_change_during_copy_fails_job_without_ready_asset(self):
        selection = self.issue_selection()
        job = self.service.create_import_job(self.session, self.project["id"], selection, "COPY", "copy-change-during-idempotency-01")
        original_probe = self.service.ffprobe

        class MutatingProbe:
            def probe(inner, path, cancelled=None):
                result = original_probe.probe(path, cancelled)
                path.write_bytes(path.read_bytes() + b"changed during probe")
                return result

        self.service.ffprobe = MutatingProbe()
        self.assertTrue(self.service.import_once())
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "FAILED")
        self.assertEqual(self.service.list_assets(self.project["id"]), [])

    def test_stale_import_does_not_replace_newer_project_source(self):
        first = self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "COPY", "older-import-idempotency-key-0001")
        second = self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "COPY", "newer-import-idempotency-key-0002")
        self.assertTrue(self.service.import_once())
        with closing(self.db.connect()) as connection:
            state = connection.execute("SELECT active_generation FROM media_import_state WHERE project_id=?", (self.project["id"],)).fetchone()
            first_generation = connection.execute("SELECT generation FROM media_assets WHERE producer_job_id=?", (first["id"],)).fetchone()
        self.assertEqual(first_generation["generation"], 1)
        self.assertEqual(state["active_generation"], 0)
        self.assertTrue(self.service.import_once())
        with closing(self.db.connect()) as connection:
            state = connection.execute("SELECT active_generation FROM media_import_state WHERE project_id=?", (self.project["id"],)).fetchone()
        self.assertEqual(state["active_generation"], 2)

    def test_restart_interrupts_durable_import_and_explicit_retry_recovers(self):
        job = self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "COPY", "restart-import-idempotency-key-01")
        self.assertEqual(self.jobs.claim_next_job(kind="media_import")["status"], "RUNNING")
        recovered_jobs = JobService(self.db, self.artifacts)
        StartupRecovery(self.db, recovered_jobs, self.artifacts).reconcile()
        interrupted = recovered_jobs.get_job(job["id"])
        self.assertEqual(interrupted["status"], "INTERRUPTED")
        recovered_jobs.retry_job(job["id"], "INTERRUPTED", "restart-retry-idempotency-key-01")
        recovered_service = MediaImportService(self.db, recovered_jobs, self.artifacts, self.ffprobe)
        self.assertTrue(recovered_service.import_once())
        self.assertEqual(recovered_jobs.get_job(job["id"])["status"], "READY")
        self.assertEqual(len(recovered_service.list_assets(self.project["id"])), 1)

    def test_cancel_queued_import_does_not_publish_asset(self):
        job = self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "COPY", "cancel-import-idempotency-key-01")
        result = self.jobs.request_cancel(job["id"], "cancel-import-action-key-001")
        self.assertEqual(result["status"], "CANCELLED")
        self.assertFalse(self.service.import_once())
        self.assertEqual(self.service.list_assets(self.project["id"]), [])

    def test_import_api_copy_and_link_end_to_end_then_survive_restart(self):
        api_root = self.root / "api-data"
        app = create_app("media-api-secret", data_root=api_root)
        host_headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
        with TestClient(app, client=("127.0.0.1", 50002)) as client:
            boot = client.post("/api/v1/session/bootstrap", headers={**host_headers, "content-type": "application/json", "x-local-bootstrap": "media-api-secret"}, json={})
            csrf = boot.json()["csrf_token"]
            project_response = client.post("/api/v1/projects", headers={**host_headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "media-api-project-idem-0001"}, json={"name": "API media"})
            project_id = project_response.json()["id"]
            original_hash = hashlib.sha256(self.video.read_bytes()).hexdigest()
            for index, mode in enumerate(("COPY", "LINK"), start=1):
                token = uuid.uuid4().hex + uuid.uuid4().hex
                session_token = client.cookies.get("autodub_session")
                with closing(app.state.database.connect()) as connection:
                    connection.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')", (hashlib.sha256(token.encode()).hexdigest(), app.state.media_service.session_hash(session_token), project_id, str(self.video), self.video.name, json.dumps(_path_identity(self.video))))
                response = client.post(
                    f"/api/v1/projects/{project_id}/media/imports",
                    headers={**host_headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": f"media-api-import-idem-{index:04d}"},
                    json={"selection_id": token, "mode": mode},
                )
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["id"]
                replay = client.post(
                    f"/api/v1/projects/{project_id}/media/imports",
                    headers={**host_headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": f"media-api-import-idem-{index:04d}"},
                    json={"selection_id": token, "mode": mode},
                )
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(replay.json()["id"], job_id)
                for _ in range(200):
                    job = client.get(f"/api/v1/jobs/{job_id}?project_id={project_id}", headers=host_headers).json()
                    if job["status"] in {"READY", "FAILED", "CANCELLED", "INTERRUPTED"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(job["status"], "READY", job)
                assets = client.get(f"/api/v1/projects/{project_id}/media", headers=host_headers).json()["items"]
                item = next(asset for asset in assets if asset["mode"] == mode)
                self.assertEqual(item["metadata"]["video_codec"], "mpeg4")
                self.assertEqual(item["metadata"]["audio_streams"], 0)
                stream_url = f"/api/v1/projects/{project_id}/media/{item['id']}/stream"
                full = client.get(stream_url, headers=host_headers)
                self.assertEqual(full.status_code, 200, full.text)
                self.assertEqual(full.content, self.video.read_bytes())
                self.assertEqual(full.headers["accept-ranges"], "bytes")
                self.assertEqual(full.headers["content-type"], "video/mp4")
                self.assertEqual(full.headers["x-content-type-options"], "nosniff")
                head = client.head(stream_url, headers=host_headers)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(head.headers["content-length"], str(len(full.content)))
                ranged = client.get(stream_url, headers={**host_headers, "range": "bytes=0-31"})
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.content, full.content[:32])
                self.assertEqual(ranged.headers["content-range"], f"bytes 0-31/{len(full.content)}")
                self.assertEqual(client.get(stream_url, headers={**host_headers, "range": f"bytes=-12"}).content, full.content[-12:])
                unsatisfiable = client.get(stream_url, headers={**host_headers, "range": f"bytes={len(full.content)}-"})
                self.assertEqual(unsatisfiable.status_code, 416)
                self.assertEqual(unsatisfiable.headers["content-range"], f"bytes */{len(full.content)}")
                self.assertEqual(client.get(stream_url, headers={**host_headers, "range": "bytes=0-1,4-5"}).status_code, 400)
                self.assertEqual(client.get(stream_url, headers={**host_headers, "if-none-match": full.headers["etag"]}).status_code, 304)
                self.assertEqual(client.get(stream_url, headers={**host_headers, "range": "bytes=0-1", "if-range": '"different"'}).status_code, 200)
                other_project, _ = app.state.database.create_project("Other", f"media-stream-other-{index:02d}")
                self.assertEqual(client.get(f"/api/v1/projects/{other_project['id']}/media/{item['id']}/stream", headers=host_headers).status_code, 404)
                self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), original_hash)
        reopened = create_app("media-api-restart-secret", data_root=api_root)
        with TestClient(reopened, client=("127.0.0.1", 50003)) as client:
            boot = client.post("/api/v1/session/bootstrap", headers={**host_headers, "content-type": "application/json", "x-local-bootstrap": "media-api-restart-secret"}, json={})
            self.assertEqual(boot.status_code, 200)
            assets = client.get(f"/api/v1/projects/{project_id}/media", headers=host_headers)
            self.assertEqual(assets.status_code, 200)
            self.assertEqual({asset["mode"] for asset in assets.json()["items"]}, {"COPY", "LINK"})
            asset = assets.json()["items"][0]
            stream_url = f"/api/v1/projects/{project_id}/media/{asset['id']}/stream"
            self.assertEqual(client.get(stream_url, headers={**host_headers, "origin": "http://evil.invalid"}).status_code, 403)
            anonymous = TestClient(reopened, client=("127.0.0.1", 50004))
            self.assertEqual(anonymous.get(stream_url, headers=host_headers).status_code, 401)

    def test_corrupt_file_is_not_accepted_by_real_ffprobe(self):
        corrupt = self.root / "bad.mp4"
        corrupt.write_bytes(b"not an mp4")
        with self.assertRaises(MediaError):
            self.ffprobe.probe(corrupt)

    def test_single_range_parser_supports_open_and_suffix_and_rejects_ambiguous(self):
        self.assertEqual(parse_single_byte_range("bytes=0-4", 10), (0, 4))
        self.assertEqual(parse_single_byte_range("bytes=5-", 10), (5, 9))
        self.assertEqual(parse_single_byte_range("bytes=-3", 10), (7, 9))
        for value in ("bytes=0-1,4-5", "bytes=-0", "items=0-1", "bytes=8-4"):
            with self.subTest(value=value), self.assertRaises(MediaError):
                parse_single_byte_range(value, 10)

    def test_link_stream_detects_change_before_serving_and_closes_on_disconnect(self):
        job = self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "LINK", "link-stream-idempotency-key-001")
        self.service.import_once()
        asset = self.service.list_assets(self.project["id"])[0]
        opened = self.service.open_media_stream(self.project["id"], asset["id"])
        chunks = self.service.stream_chunks(opened, 0, opened.size)
        next(chunks)
        chunks.close()
        self.assertTrue(opened.handle.closed)
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")

        self.video.write_bytes(self.video.read_bytes() + b"tampered")
        with self.assertRaisesRegex(MediaError, "asset_changed"):
            self.service.open_media_stream(self.project["id"], asset["id"])
        self.assertEqual(self.service.list_assets(self.project["id"])[0]["status"], "CHANGED")
        with closing(self.db.connect()) as connection:
            connection.execute("UPDATE media_assets SET private_source_path=? WHERE id=?", (str(self.root / "deleted-link.mp4"), asset["id"]))
        self.assertEqual(self.service.list_assets(self.project["id"])[0]["status"], "MISSING")

    def test_relink_same_content_requires_bound_selection_and_cas(self):
        self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "LINK", "link-relink-idempotency-key-01")
        self.service.import_once()
        asset = self.service.list_assets(self.project["id"])[0]
        with closing(self.db.connect()) as connection:
            connection.execute("UPDATE media_assets SET status='MISSING',private_source_path=? WHERE id=?", (str(self.root / "missing-source.mp4"), asset["id"]))
        relink_token = uuid.uuid4().hex + uuid.uuid4().hex
        identity = _path_identity(self.video)
        with closing(self.db.connect()) as connection:
            connection.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,asset_id,created_at) VALUES(?,?,?,'relink',?,?,?,strftime('%s','now')+600,'ISSUED',?,'now')", (hashlib.sha256(relink_token.encode()).hexdigest(), self.service.session_hash(self.session), self.project["id"], str(self.video), self.video.name, json.dumps(identity), asset["id"]))
        result = self.service.relink_asset(self.session, self.project["id"], asset["id"], relink_token, asset["lock_version"], "relink-confirm-idempotency-key-01")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(self.service.list_assets(self.project["id"])[0]["status"], "READY")
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM media_relink_history WHERE asset_id=?", (asset["id"],)).fetchone()["n"], 1)

    def test_relink_different_content_is_rejected_without_changing_asset(self):
        self.service.create_import_job(self.session, self.project["id"], self.issue_selection(), "LINK", "link-relink-mismatch-idem-01")
        self.service.import_once()
        asset = self.service.list_assets(self.project["id"])[0]
        with closing(self.db.connect()) as connection:
            connection.execute("UPDATE media_assets SET status='MISSING',private_source_path=? WHERE id=?", (str(self.root / "missing-source.mp4"), asset["id"]))
        other_video = self.root / "different.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:r=10", "-t", "1", "-an", "-c:v", "mpeg4", "-y", str(other_video)], check=True, timeout=30)
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with closing(self.db.connect()) as connection:
            connection.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,asset_id,created_at) VALUES(?,?,?,'relink',?,?,?,strftime('%s','now')+600,'ISSUED',?,'now')", (hashlib.sha256(token.encode()).hexdigest(), self.service.session_hash(self.session), self.project["id"], str(other_video), other_video.name, json.dumps(_path_identity(other_video)), asset["id"]))
        with self.assertRaisesRegex(MediaError, "relink_content_mismatch"):
            self.service.relink_asset(self.session, self.project["id"], asset["id"], token, asset["lock_version"], "relink-mismatch-action-idem-01")
        with closing(self.db.connect()) as connection:
            unchanged = connection.execute("SELECT private_source_path,status,sha256 FROM media_assets WHERE id=?", (asset["id"],)).fetchone()
            selection_state = connection.execute("SELECT state FROM selections WHERE id_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()["state"]
        self.assertEqual(unchanged["private_source_path"], str(self.root / "missing-source.mp4"))
        self.assertEqual(unchanged["status"], "MISSING")
        self.assertEqual(unchanged["sha256"], asset["sha256"])
        self.assertEqual(selection_state, "REVOKED")


if __name__ == "__main__":
    unittest.main()
