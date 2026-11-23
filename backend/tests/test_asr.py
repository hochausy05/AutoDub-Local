from __future__ import annotations

import hashlib
import errno
import json
import shutil
import subprocess
import threading
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.artifacts import ArtifactPublicationService
from app.asr import ASRConfig, ASRError, ASRService, FFmpegAudioExtractor, FasterWhisperAdapter, TranscriptArtifactValidator, validate_config
from app.database import Database
from app.jobs import JobService
from app.media import FFprobe, LinkManifestValidator, MediaError, MediaImportService, SourceVideoValidator, _path_identity
from app.recovery import StartupRecovery
from fastapi.testclient import TestClient
from app.main import create_app


class FakeASRAdapter:
    def __init__(self, result=None, cached=True):
        self.result = result
        self.cached = cached

    def is_cached(self, _model):
        return self.cached

    def run(self, _audio_path, _config, *, source_duration_us, source_offset_us, cancelled, progress):
        progress("loading_model")
        progress("transcribing")
        if cancelled():
            raise ASRError("job_cancelled")
        if self.result is not None:
            return self.result
        return {
            "segments": [{"start_us": 200_000 + source_offset_us, "end_us": 900_000 + source_offset_us,
                          "source_text": "Xin chào.", "avg_logprob": -0.2, "no_speech_prob": 0.01,
                          "compression_ratio": 1.1, "flags": []}],
            "language": "vi", "language_probability": 0.99, "actual_device": "cpu", "compute_type": "int8",
            "fallback_reason": None, "model_id": "Systran/faster-whisper-base",
            "model_revision": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
            "faster_whisper_version": "test-fake", "ctranslate2_version": "test-fake",
            "model_load_seconds": 0.01, "inference_seconds": 0.02, "run_status": "READY",
        }


class ASRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "data")
        self.db.initialize()
        self.project, _ = self.db.create_project("ASR test", "asr-project-idempotency-key-001")
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = FFprobe()
        if not self.ffmpeg or not self.ffprobe.executable:
            self.skipTest("ffmpeg/ffprobe unavailable")
        self.source = self.root / "vídeo-asr-日本語.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-shortest",
            "-c:v", "mpeg4", "-c:a", "aac", "-y", str(self.source),
        ], check=True, timeout=30)
        self.artifacts = ArtifactPublicationService(self.db, {
            "source_video": SourceVideoValidator(self.ffprobe),
            "media_link_manifest": LinkManifestValidator(),
            "transcript_json": TranscriptArtifactValidator(),
        })
        self.jobs = JobService(self.db, self.artifacts)
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        self.media = MediaImportService(self.db, self.jobs, self.artifacts, self.ffprobe)
        self.session = "asr-integration-session"
        self.asset = self._import_asset(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def _import_asset(self, source: Path, mode="COPY") -> dict:
        previous_ids = {item["id"] for item in self.media.list_assets(self.project["id"])}
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with closing(self.db.connect()) as connection:
            connection.execute(
                "INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')",
                (hashlib.sha256(token.encode()).hexdigest(), self.media.session_hash(self.session), self.project["id"], str(source), source.name, json.dumps(_path_identity(source))),
            )
        job = self.media.create_import_job(self.session, self.project["id"], token, mode, f"asr-source-import-{uuid.uuid4().hex}")
        self.assertTrue(self.media.import_once())
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")
        return next(item for item in self.media.list_assets(self.project["id"]) if item["id"] not in previous_ids)

    def _service(self, adapter=None):
        return ASRService(self.db, self.jobs, self.artifacts, self.media, adapter=adapter or FakeASRAdapter())

    def _create_job(self, service, config, key, asset_id=None):
        if asset_id:
            current = service.get_processing_source(self.project["id"])
            if current["media_asset_id"] != asset_id:
                service.select_processing_source(self.project["id"], asset_id, current["generation"])
        return service.create_job(self.project["id"], config, key)

    def test_extractor_creates_real_mono_16khz_wav_from_unicode_mp4(self):
        source = self.media.get_transcription_source(self.project["id"], self.asset["id"])
        destination = self.root / "staging" / "音声.wav"
        info = FFmpegAudioExtractor().extract(source["path"], destination, lambda: False)
        self.assertGreater(info.duration_us, 1_000_000)
        self.assertEqual(info.sample_rate, 16_000)
        self.assertTrue(destination.exists())
        self.assertTrue(self.media.verify_transcription_source(source))

    def test_extractor_preserves_nonzero_audio_stream_offset(self):
        delayed = self.root / "delayed-audio.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x48:r=10",
            "-itsoffset", "0.65", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-t", "2",
            "-c:v", "mpeg4", "-c:a", "aac", "-y", str(delayed),
        ], check=True, timeout=30)
        destination = self.root / "delayed.wav"
        info = FFmpegAudioExtractor().extract(delayed, destination, lambda: False)
        self.assertGreater(info.source_offset_us, 400_000)
        self.assertEqual(info.source_video_start_us, 0)

    def test_cuda_initialization_failure_retries_cpu_once_and_reports_actual_device(self):
        import faster_whisper
        import huggingface_hub

        class Probe:
            @staticmethod
            def inspect():
                return {"cuda_device_count": 1, "cuda_compute_types": ["float16"], "cpu_compute_types": ["int8"], "probe_error": None}

        class Model:
            def transcribe(self, *_args, **_kwargs):
                return iter(()), type("Info", (), {"language": "vi", "language_probability": 0.8})()

        calls = []
        def model_factory(_path, *, device, **_kwargs):
            calls.append(device)
            if device == "cuda":
                raise RuntimeError("CUDA cuDNN initialization failed")
            return Model()

        with patch.object(faster_whisper, "WhisperModel", side_effect=model_factory), patch.object(huggingface_hub, "snapshot_download", return_value=str(self.root)):
            adapter = FasterWhisperAdapter(self.root / "models", probe=Probe())
            result = adapter.run(
                self.source, ASRConfig(device="cuda", allow_cpu_fallback=True), source_duration_us=2_000_000,
                source_offset_us=0, cancelled=lambda: False, progress=lambda _phase: None,
            )
        self.assertEqual(calls, ["cuda", "cpu"])
        self.assertEqual(result["actual_device"], "cpu")
        self.assertEqual(result["fallback_reason"], "cuda_initialization_failed")
        self.assertEqual(result["run_status"], "NO_SPEECH")

    def test_cuda_failure_does_not_fallback_when_policy_disallows_it(self):
        import faster_whisper
        import huggingface_hub

        class Probe:
            @staticmethod
            def inspect():
                return {"cuda_device_count": 1, "cuda_compute_types": ["float16"], "cpu_compute_types": ["int8"], "probe_error": None}

        calls = []
        def model_factory(_path, *, device, **_kwargs):
            calls.append(device)
            raise RuntimeError("CUDA device initialization failed")

        with patch.object(faster_whisper, "WhisperModel", side_effect=model_factory), patch.object(huggingface_hub, "snapshot_download", return_value=str(self.root)):
            adapter = FasterWhisperAdapter(self.root / "models", probe=Probe())
            with self.assertRaisesRegex(ASRError, "cuda_initialization_failed"):
                adapter.run(self.source, ASRConfig(device="cuda", allow_cpu_fallback=False), source_duration_us=2_000_000, source_offset_us=0, cancelled=lambda: False, progress=lambda _phase: None)
        self.assertEqual(calls, ["cuda"])

    def test_job_persists_immutable_segments_and_is_visible_after_service_restart(self):
        service = self._service()
        config = ASRConfig(language="vi", device="cpu")
        first = self._create_job(service, config, "asr-job-idempotency-key-0001", self.asset["id"])
        replay = self._create_job(service, config, "asr-job-idempotency-key-0001", self.asset["id"])
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(service.run_once())
        self.assertEqual(self.jobs.get_job(first["id"])["status"], "READY")
        runs = service.list_runs(self.project["id"])
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["is_active"])
        run = service.get_run(self.project["id"], runs[0]["id"])
        self.assertEqual(run["segments"][0]["source_text"], "Xin chào.")
        self.assertGreater(run["segments"][0]["end_us"], run["segments"][0]["start_us"])
        self.assertEqual(run["provenance"]["actual_device"], "cpu")
        with closing(self.db.connect()) as connection:
            with self.assertRaises(Exception):
                connection.execute("UPDATE segments SET source_text='changed' WHERE run_id=?", (run["id"],))
        restarted = self._service()
        self.assertEqual(restarted.get_run(self.project["id"], run["id"])["segments"], run["segments"])

    def test_single_video_default_persists_and_second_same_name_requires_explicit_choice(self):
        service = self._service()
        first = service.get_processing_source(self.project["id"])
        self.assertEqual((first["media_asset_id"], first["generation"]), (self.asset["id"], 1))
        duplicate_name = self.root / "another" / self.source.name
        duplicate_name.parent.mkdir()
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=96x64:r=10", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "2", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(duplicate_name)], check=True, timeout=30)
        second = self._import_asset(duplicate_name)
        self.assertEqual(second["display_name"], self.asset["display_name"])
        self.assertNotEqual(second["id"], self.asset["id"])
        self.assertEqual(service.get_processing_source(self.project["id"])["media_asset_id"], self.asset["id"])
        with closing(self.db.connect()) as connection:
            connection.execute("DELETE FROM project_processing_sources WHERE project_id=?", (self.project["id"],))
        self.assertEqual(service.get_processing_source(self.project["id"])["status"], "UNSELECTED")
        selected = service.select_processing_source(self.project["id"], second["id"], 0)
        self.assertEqual(selected["generation"], 1)
        restarted_db = Database(self.root / "data")
        restarted_db.initialize()
        restarted_artifacts = ArtifactPublicationService(restarted_db, {"source_video": SourceVideoValidator(self.ffprobe), "media_link_manifest": LinkManifestValidator(), "transcript_json": TranscriptArtifactValidator()})
        restarted_jobs = JobService(restarted_db, restarted_artifacts)
        restarted_media = MediaImportService(restarted_db, restarted_jobs, restarted_artifacts, self.ffprobe)
        restarted = ASRService(restarted_db, restarted_jobs, restarted_artifacts, restarted_media).get_processing_source(self.project["id"])
        self.assertEqual((restarted["media_asset_id"], restarted["generation"]), (second["id"], 1))

    def test_source_switch_during_job_keeps_snapshot_and_blocks_late_activation(self):
        source_b = self.root / "source-b.mp4"
        source_b.write_bytes(self.source.read_bytes())
        second = self._import_asset(source_b)
        entered, resume = threading.Event(), threading.Event()

        class BlockingAdapter(FakeASRAdapter):
            def run(self, *args, **kwargs):
                entered.set()
                if not resume.wait(10):
                    raise ASRError("test_timeout")
                return super().run(*args, **kwargs)

        service = self._service(BlockingAdapter())
        job = self._create_job(service, ASRConfig(device="cpu"), "asr-source-snapshot-idem-001", self.asset["id"])
        with closing(self.db.connect()) as connection:
            snapshot = json.loads(connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
        self.assertEqual(snapshot["source_asset_id"], self.asset["id"])
        worker = threading.Thread(target=service.run_once)
        worker.start()
        self.assertTrue(entered.wait(5))
        current = service.get_processing_source(self.project["id"])
        service.select_processing_source(self.project["id"], second["id"], current["generation"])
        resume.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")
        self.assertEqual(self.jobs.get_job(job["id"])["attempts"][-1]["activation_outcome"], "STALE")
        old_run = service.list_runs(self.project["id"])[0]
        self.assertEqual(old_run["source_asset_id"], self.asset["id"])
        self.assertFalse(old_run["is_active"])

    def test_missing_selected_source_blocks_job_without_erasing_old_runs(self):
        linked_path = self.root / "linked-source.mp4"
        linked_path.write_bytes(self.source.read_bytes())
        linked = self._import_asset(linked_path, "LINK")
        service = self._service()
        current = service.get_processing_source(self.project["id"])
        service.select_processing_source(self.project["id"], linked["id"], current["generation"])
        linked_path.unlink()
        self.assertEqual(service.get_processing_source(self.project["id"])["status"], "MISSING")
        with self.assertRaisesRegex(ASRError, "asset_unavailable"):
            service.create_job(self.project["id"], ASRConfig(), "asr-missing-source-idem-0001")

    def test_no_speech_is_explicit_and_creates_no_fake_segment(self):
        silent = self.root / "silence.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x48:r=10",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1.5", "-shortest",
            "-c:v", "mpeg4", "-c:a", "aac", "-y", str(silent),
        ], check=True, timeout=30)
        silent_asset = self._import_asset(silent)
        result = FakeASRAdapter().run(None, ASRConfig(), source_duration_us=2_000_000, source_offset_us=0, cancelled=lambda: False, progress=lambda _phase: None)
        result["segments"] = []
        result["run_status"] = "NO_SPEECH"
        service = self._service(FakeASRAdapter(result))
        job = self._create_job(service, ASRConfig(device="cpu"), "asr-no-speech-idempotency-key-01", silent_asset["id"])
        service.run_once()
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "READY")
        run = service.list_runs(self.project["id"])[0]
        self.assertEqual(run["status"], "NO_SPEECH")
        self.assertEqual(service.get_run(self.project["id"], run["id"])["segments"], [])

    def test_no_audio_stream_fails_job_without_transcription_run(self):
        silent_video = self.root / "no-audio.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x48:r=10", "-t", "1", "-an", "-c:v", "mpeg4", "-y", str(silent_video)], check=True, timeout=30)
        asset = self._import_asset(silent_video)
        service = self._service()
        job = self._create_job(service, ASRConfig(device="cpu"), "asr-no-audio-idempotency-key-01", asset["id"])
        service.run_once()
        failed = self.jobs.get_job(job["id"])
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error_code"], "no_audio_stream")
        self.assertFalse(any(run["source_asset_id"] == asset["id"] for run in service.list_runs(self.project["id"])))

    def test_corrupt_media_is_rejected_by_ffprobe_extractor(self):
        corrupt = self.root / "broken.mp4"
        corrupt.write_bytes(b"not an MP4")
        with self.assertRaisesRegex(ASRError, "media_probe_failed"):
            FFmpegAudioExtractor().extract(corrupt, self.root / "broken.wav", lambda: False)

    def test_model_config_validation_and_explicit_download_consent(self):
        with self.assertRaisesRegex(ASRError, "invalid_beam_size"):
            validate_config(ASRConfig(beam_size=9))
        with self.assertRaisesRegex(ASRError, "invalid_model"):
            validate_config(ASRConfig(model="large-v3"))
        service = self._service(FakeASRAdapter(cached=False))
        with self.assertRaisesRegex(ASRError, "model_download_consent_required"):
            self._create_job(service, ASRConfig(), "asr-download-consent-idempotency-01", self.asset["id"])

    def test_model_download_reports_insufficient_disk_space_without_raw_os_error(self):
        import faster_whisper
        import huggingface_hub

        with patch.object(faster_whisper, "WhisperModel"), patch.object(huggingface_hub, "snapshot_download", side_effect=OSError(errno.ENOSPC, "disk full")):
            adapter = FasterWhisperAdapter(self.root / "models")
            with self.assertRaisesRegex(ASRError, "insufficient_model_disk_space"):
                adapter.run(self.source, ASRConfig(device="cpu", allow_model_download=True), source_duration_us=2_000_000, source_offset_us=0, cancelled=lambda: False, progress=lambda _phase: None)

    def test_failure_writes_local_diagnostic_without_private_path_or_source_text(self):
        class BrokenAdapter(FakeASRAdapter):
            def run(self, *_args, **_kwargs):
                try:
                    raise TypeError(f"open failed at {self_source} for PRIVATE DIALOGUE")
                except TypeError as exc:
                    raise ASRError("asr_inference_failed") from exc

        self_source = str(self.source)
        service = self._service(BrokenAdapter())
        job = self._create_job(service, ASRConfig(device="cpu"), "asr-diagnostic-job-key-001", self.asset["id"])
        self.assertTrue(service.run_once())
        diagnostic_path = self.db.data_root / "logs" / "asr-diagnostics.jsonl"
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(diagnostic["job_id"], job["id"])
        self.assertEqual(diagnostic["error_code"], "asr_inference_failed")
        self.assertEqual(diagnostic["exception_type"], "typeerror")
        self.assertNotIn(self_source, diagnostic_path.read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE DIALOGUE", diagnostic_path.read_text(encoding="utf-8"))

    def test_pyav_decode_audio_compatibility(self):
        import av
        import faster_whisper.audio

        self.assertEqual(int(av.__version__.split(".")[0]), 18)
        decoded = faster_whisper.audio.decode_audio(str(self.source), sampling_rate=16_000)
        self.assertGreater(len(decoded), 0)

    def test_stale_run_is_persisted_but_does_not_replace_active_pointer(self):
        service = self._service()
        first = self._create_job(service, ASRConfig(language="vi", device="cpu"), "asr-active-idempotency-key-001", self.asset["id"])
        service.run_once()
        active_before = next(run for run in service.list_runs(self.project["id"]) if run["is_active"])
        second = self._create_job(service, ASRConfig(language="en", device="cpu"), "asr-stale-idempotency-key-0001", self.asset["id"])
        self.jobs.set_stage_input(self.project["id"], "asr", {"schema_version": 1, "changed": True})
        service.run_once()
        runs = service.list_runs(self.project["id"])
        self.assertEqual(sum(bool(run["is_active"]) for run in runs), 1)
        active_after = next(run for run in runs if run["is_active"])
        self.assertEqual(active_after["id"], active_before["id"])
        self.assertEqual(self.jobs.get_job(second["id"])["attempts"][-1]["activation_outcome"], "STALE")

    def test_interrupted_asr_job_retries_and_preserves_attempt_history(self):
        service = self._service()
        job = self._create_job(service, ASRConfig(device="cpu"), "asr-restart-idempotency-key-01", self.asset["id"])
        self.jobs.claim_next_job(kind="asr_transcription")
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        interrupted = self.jobs.get_job(job["id"])
        self.assertEqual(interrupted["status"], "INTERRUPTED")
        self.jobs.retry_job(job["id"], "INTERRUPTED", "asr-retry-idempotency-key-0001")
        self.assertTrue(service.run_once())
        completed = self.jobs.get_job(job["id"])
        self.assertEqual(completed["status"], "READY")
        self.assertEqual([attempt["attempt_no"] for attempt in completed["attempts"]], [1, 2])

    def test_segments_require_valid_order_and_times_in_artifact(self):
        invalid = self.root / "transcript.json"
        invalid.write_text(json.dumps({"schema_version": 1, "status": "READY", "segments": [{"position": 0, "start_us": 5, "end_us": 2, "source_text": "x"}]}), encoding="utf-8")
        with self.assertRaises(Exception):
            TranscriptArtifactValidator().validate(invalid)

    def test_asr_api_requires_session_csrf_and_exact_origin(self):
        api_root = self.root / "api-data"
        app = create_app("asr-api-secret", data_root=api_root)
        headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
        project_id = None
        body = {"language": "auto"}
        with TestClient(app, client=("127.0.0.1", 50401)) as client:
            anonymous = client.post("/api/v1/projects/00000000-0000-0000-0000-000000000000/asr/jobs", headers={**headers, "content-type": "application/json"}, json=body)
            self.assertEqual(anonymous.status_code, 401)
            boot = client.post("/api/v1/session/bootstrap", headers={**headers, "content-type": "application/json", "x-local-bootstrap": "asr-api-secret"}, json={})
            csrf = boot.json()["csrf_token"]
            created = client.post("/api/v1/projects", headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "asr-api-project-idem-key-01"}, json={"name": "ASR API"})
            project_id = created.json()["id"]
            self.assertEqual(client.get(f"/api/v1/projects/{project_id}/processing-source", headers=headers).json()["status"], "UNSELECTED")
            denied_source_change = client.put(f"/api/v1/projects/{project_id}/processing-source", headers={**headers, "content-type": "application/json"}, json={"media_asset_id": str(uuid.uuid4()), "expected_generation": 0})
            self.assertEqual(denied_source_change.status_code, 403)
            invalid_source = client.put(f"/api/v1/projects/{project_id}/processing-source", headers={**headers, "content-type": "application/json", "x-csrf-token": csrf}, json={"media_asset_id": str(uuid.uuid4()), "expected_generation": 0})
            self.assertEqual(invalid_source.status_code, 404)
            missing_csrf = client.post(f"/api/v1/projects/{project_id}/asr/jobs", headers={**headers, "content-type": "application/json", "idempotency-key": "asr-api-create-idem-key-001"}, json=body)
            self.assertEqual(missing_csrf.status_code, 403)
            denied_origin = client.get(f"/api/v1/projects/{project_id}/asr/runs", headers={**headers, "origin": "http://evil.invalid"})
            self.assertEqual(denied_origin.status_code, 403)
            missing_asset = client.post(f"/api/v1/projects/{project_id}/asr/jobs", headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "asr-api-create-idem-key-002"}, json=body)
            self.assertEqual(missing_asset.status_code, 422)
            technical = client.post(f"/api/v1/projects/{project_id}/asr/jobs", headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "asr-api-create-idem-key-003"}, json={"language": "auto", "device": "cuda", "beam_size": 5})
            self.assertEqual(technical.status_code, 422)
            invalid_language = client.post(f"/api/v1/projects/{project_id}/asr/jobs", headers={**headers, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "asr-api-create-idem-key-004"}, json={"language": "fr"})
            self.assertEqual(invalid_language.status_code, 422)
            runs = client.get(f"/api/v1/projects/{project_id}/asr/runs", headers=headers)
            self.assertEqual(runs.status_code, 200)
            self.assertEqual(runs.json()["items"], [])

    def test_api_uses_backend_defaults_without_technical_client_fields(self):
        app = create_app("asr-defaults-secret", data_root=self.root / "defaults-api-data")
        app.state.database.initialize()
        app.state.startup_recovery.reconcile()
        project, _ = app.state.database.create_project("Defaults", "asr-defaults-project-idem-01")
        token = uuid.uuid4().hex + uuid.uuid4().hex
        session_token = "asr-defaults-session"
        with closing(app.state.database.connect()) as connection:
            connection.execute(
                "INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')",
                (hashlib.sha256(token.encode()).hexdigest(), app.state.media_service.session_hash(session_token), project["id"], str(self.source), self.source.name, json.dumps(_path_identity(self.source))),
            )
        app.state.media_service.create_import_job(session_token, project["id"], token, "COPY", "asr-defaults-import-idem-01")
        self.assertTrue(app.state.media_service.import_once())
        with TestClient(app, client=("127.0.0.1", 50402)) as client:
            app.state.asr_service.adapter = FakeASRAdapter()
            headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
            boot = client.post("/api/v1/session/bootstrap", headers={**headers, "content-type": "application/json", "x-local-bootstrap": "asr-defaults-secret"}, json={})
            csrf = boot.json()["csrf_token"]
            source = client.get(f"/api/v1/projects/{project['id']}/processing-source", headers=headers).json()
            self.assertEqual(source["status"], "READY")
            created = client.post(
                f"/api/v1/projects/{project['id']}/asr/jobs",
                headers={**headers, "x-csrf-token": csrf, "idempotency-key": "asr-defaults-job-idem-key-01"},
                json={"language": "auto"},
            )
            self.assertEqual(created.status_code, 202, created.text)
            with closing(app.state.database.connect()) as connection:
                snapshot = json.loads(connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (created.json()["id"],)).fetchone()[0])
            self.assertEqual(snapshot["source_asset_id"], source["media_asset_id"])
            self.assertEqual(snapshot["model_id"], "Systran/faster-whisper-base")
            self.assertEqual(snapshot["device"], "cpu")
            self.assertEqual(snapshot["beam_size"], 3)
            self.assertTrue(snapshot["vad_enabled"])


if __name__ == "__main__":
    unittest.main()
