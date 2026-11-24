from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.artifacts import ArtifactPublicationService
from app.asr import ASRConfig, ASRService, TranscriptArtifactValidator
from app.database import Database
from app.jobs import JobService
from app.main import create_app
from app.media import FFprobe, LinkManifestValidator, MediaImportService, SourceVideoValidator, _path_identity
from app.recovery import StartupRecovery
from app.transcripts import TranscriptService
from app.translation import TranslationError, TranslationService, TranslationWorker
from app.translation import GeminiProvider
from app.gemini_credentials import GeminiCredentialManager


class FakeASR:
    def is_cached(self, _model): return True

    def run(self, _audio, _config, *, source_duration_us, source_offset_us, cancelled, progress):
        progress("transcribing")
        import faster_whisper, ctranslate2
        texts = ["你好，世界。", "I did not say that.", "Xin chào bạn."]
        return {"segments": [{"start_us": i * 400_000 + 100_000, "end_us": i * 400_000 + 350_000,
                    "source_text": text, "avg_logprob": -0.1, "no_speech_prob": 0.01, "compression_ratio": 1.0, "flags": []}
                    for i, text in enumerate(texts)], "language": "zh", "language_probability": 0.99,
                "actual_device": "cpu", "compute_type": "int8", "fallback_reason": None,
                "model_id": "Systran/faster-whisper-base", "model_revision": "fixture-revision",
                "faster_whisper_version": faster_whisper.__version__, "ctranslate2_version": ctranslate2.__version__,
                "model_load_seconds": 0.01, "inference_seconds": 0.02, "run_status": "READY"}


class FakeGemini:
    def __init__(self): self.calls = []

    def translate(self, *, pass_number, chunk_id, units, context, **_options):
        self.calls.append((pass_number, chunk_id, [unit["unit_id"] for unit in units], context))
        return {"schema_version": 1, "pass": pass_number, "chunk_id": chunk_id, "items": [
            {"unit_id": unit["unit_id"], "revision_segment_ids": unit["revision_segment_ids"],
             "text": ("Bản nháp: " if pass_number == 1 else "Tiếng Việt tự nhiên: ") + unit.get("draft_vi", unit["source_text"]), "notes": []}
            for unit in units]}


class CapturingGeminiClient:
    def __init__(self, key):
        self.key = key
        self.interactions = self
        self.request = None
        self.status = "completed"

    def create(self, **kwargs):
        self.request = kwargs
        payload = json.loads(kwargs["input"].split("INPUT_JSON:\n", 1)[1])
        return SimpleNamespace(status=self.status, output_text=json.dumps({"schema_version": 1, "pass": payload["pass"], "chunk_id": payload["chunk_id"], "items": [
            {"unit_id": unit["unit_id"], "revision_segment_ids": unit["revision_segment_ids"], "text": "Xin chào", "notes": []} for unit in payload["owned_units"]]}))


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "data")
        self.db.initialize()
        self.project, _ = self.db.create_project("Translation integration", "translation-project-idem-key-01")
        self.ffmpeg, self.ffprobe = shutil.which("ffmpeg"), FFprobe()
        if not self.ffmpeg or not self.ffprobe.executable:
            self.skipTest("ffmpeg/ffprobe unavailable")
        self.source = self.root / "视频-translation-日本語.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(self.source)], check=True, timeout=30)
        self.artifacts = ArtifactPublicationService(self.db, {"source_video": SourceVideoValidator(self.ffprobe),
            "media_link_manifest": LinkManifestValidator(), "transcript_json": TranscriptArtifactValidator()})
        self.jobs = JobService(self.db, self.artifacts)
        StartupRecovery(self.db, self.jobs, self.artifacts).reconcile()
        self.media = MediaImportService(self.db, self.jobs, self.artifacts, self.ffprobe)
        token, session = uuid.uuid4().hex * 2, "translation-fixture-session"
        with closing(self.db.connect()) as connection:
            connection.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')",
                (hashlib.sha256(token.encode()).hexdigest(), self.media.session_hash(session), self.project["id"], str(self.source), self.source.name, json.dumps(_path_identity(self.source))))
        import_job = self.media.create_import_job(session, self.project["id"], token, "COPY", "translation-import-idem-001")
        self.assertTrue(self.media.import_once())
        self.assertEqual(self.jobs.get_job(import_job["id"])["status"], "READY")
        asr = ASRService(self.db, self.jobs, self.artifacts, self.media, adapter=FakeASR())
        asr_job = asr.create_job(self.project["id"], ASRConfig(device="cpu"), "translation-asr-idem-0001")
        self.assertTrue(asr.run_once())
        self.assertEqual(self.jobs.get_job(asr_job["id"])["status"], "READY")
        self.transcripts = TranscriptService(self.db)
        run_id = asr.list_runs(self.project["id"])[0]["id"]
        draft = self.transcripts.create_draft(self.project["id"], source_run_id=run_id, parent_revision_id=None, idempotency_key="translation-draft-idem-0001")
        self.accepted = self.transcripts.accept(self.project["id"], draft["id"], draft["lock_version"], None, "translation-accept-idem-001")
        self.service = TranslationService(self.db, self.jobs)
        self.artifacts.validators["translation_json"] = __import__("app.translation", fromlist=["TranslationArtifactValidator"]).TranslationArtifactValidator(self.db)

    def tearDown(self): self.temp.cleanup()

    def test_requires_privacy_consent_and_never_embeds_secret_in_snapshot(self):
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "SAMPLE-SECRET-FOR-TEST"}):
            with self.assertRaisesRegex(TranslationError, "privacy_consent_required"):
                self.service.create_job(self.project["id"], self.accepted["id"], False, "translation-no-consent-01")
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-consent-ok-01")
        snapshot = self.jobs.get_job(created["job"]["id"])["input_fingerprint"]
        with closing(self.db.connect()) as connection:
            input_json = connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (created["job"]["id"],)).fetchone()[0]
        self.assertNotIn("SAMPLE-SECRET-FOR-TEST", input_json)
        self.assertTrue(len(snapshot) == 64)

    def test_bulk_first_has_no_fixed_ten_unit_limit_and_keeps_exact_coverage(self):
        units = [{"unit_id": f"u{i}", "source_text": f"中文句子{i}。"} for i in range(10)]
        chunks = TranslationService.chunk_units(units)
        self.assertEqual(len(chunks), 1)
        self.assertEqual([unit["unit_id"] for unit in chunks[0]], [unit["unit_id"] for unit in units])
        self.assertEqual("".join(unit["source_text"] for unit in chunks[0]), "".join(unit["source_text"] for unit in units))

    def test_adaptive_chunk_planner_scales_deterministically_for_long_transcripts(self):
        expected = {10: 1, 100: 1, 300: 2, 500: 3, 1000: 6}
        for count, expected_chunks in expected.items():
            with self.subTest(unit_count=count):
                units = [{"unit_id": f"u{i}", "source_text": "你好，请稍等。"} for i in range(count)]
                chunks = TranslationService.chunk_units(units)
                self.assertEqual(len(chunks), expected_chunks)
                flattened = [unit["unit_id"] for chunk in chunks for unit in chunk]
                self.assertEqual(flattened, [unit["unit_id"] for unit in units])
                self.assertEqual(chunks, TranslationService.chunk_units(units))
                for chunk in chunks:
                    estimated_output = sum(int(TranslationService._estimated_tokens(unit["source_text"]) * 1.8) + 24 for unit in chunk)
                    self.assertLessEqual(estimated_output, 6000)

    def test_adaptive_chunk_planner_splits_by_estimated_output_not_unit_count(self):
        units = [{"unit_id": f"u{i}", "source_text": "中" * 80} for i in range(100)]
        chunks = TranslationService.chunk_units(units)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(sum(int(TranslationService._estimated_tokens(unit["source_text"]) * 1.8) + 24 for unit in chunk) <= 6000 for chunk in chunks))

    def test_credential_manager_reads_legacy_and_multiple_keys_without_persisting_secrets(self):
        raw = json.dumps([
            {"alias": "key-a", "api_key": "SYNTHETIC-SECRET-A", "quota_group": "project-one", "priority": 1},
            {"alias": "key-b", "api_key": "SYNTHETIC-SECRET-B", "quota_group": "project-one", "priority": 2},
            {"alias": "key-c", "api_key": "SYNTHETIC-SECRET-C", "quota_group": "project-two", "priority": 3},
        ])
        manager = GeminiCredentialManager(self.db, {"AUTODUB_GEMINI_CREDENTIALS_JSON": raw})
        keys = manager.configured()
        self.assertEqual([item.alias for item in keys], ["key-a", "key-b", "key-c"])
        self.assertEqual(keys[0].quota_group, keys[1].quota_group)
        manager.mark_auth_failed(keys[0], "gemini_api_key_invalid")
        available = manager.available()
        self.assertEqual([item.alias for item in available], ["key-b", "key-c"])
        with closing(self.db.connect()) as connection:
            raw_state = json.dumps([dict(row) for row in connection.execute("SELECT * FROM translation_credential_states")])
        self.assertNotIn("SYNTHETIC-SECRET", raw_state)

    def test_credential_manager_keeps_legacy_single_key_compatible(self):
        manager = GeminiCredentialManager(self.db, {"AUTODUB_GEMINI_API_KEY": "SYNTHETIC-LEGACY"})
        self.assertEqual([(item.alias, item.quota_group) for item in manager.configured()], [("legacy", "legacy")])

    def test_provider_fails_over_once_on_auth_error(self):
        keys = []
        class AuthError(Exception):
            status_code = 401
        class Client:
            def __init__(self, key): self.key = key
            def close(self): pass
            class Interactions:
                def __init__(self, outer): self.outer = outer
                def create(self, **_kwargs):
                    keys.append(self.outer.key)
                    if self.outer.key == "SYNTHETIC-KEY-A": raise AuthError()
                    return SimpleNamespace(status="completed", output_text=json.dumps({"schema_version": 1, "pass": 1,
                        "chunk_id": "fixture", "items": [{"unit_id": "u0", "text": "Xin chào", "notes": []}]}))
            @property
            def interactions(self): return self.Interactions(self)
        env = {"AUTODUB_GEMINI_CREDENTIALS_JSON": json.dumps([
            {"alias": "a", "api_key": "SYNTHETIC-KEY-A", "quota_group": "project-a", "priority": 1},
            {"alias": "b", "api_key": "SYNTHETIC-KEY-B", "quota_group": "project-b", "priority": 2},
        ])}
        manager = GeminiCredentialManager(self.db, env)
        provider = GeminiProvider(client_factory=lambda key: Client(key), credential_manager=manager)
        result = provider.translate(pass_number=1, chunk_id="fixture", units=[{"unit_id": "u0", "revision_segment_ids": [], "source_text": "你好"}], context=[])
        self.assertEqual(keys, ["SYNTHETIC-KEY-A", "SYNTHETIC-KEY-B"])
        self.assertEqual(result["items"][0]["unit_id"], "u0")
        with closing(self.db.connect()) as connection:
            state = connection.execute("SELECT state,quota_group FROM translation_credential_states WHERE alias='a'").fetchone()
        self.assertEqual(tuple(state), ("AUTH_FAILED", "project-a"))
        provider.close()

    def test_provider_503_uses_bounded_retry_on_same_credential(self):
        class ServiceUnavailable(Exception): status_code = 503
        keys = []
        class Client:
            def __init__(self, key): self.key = key
            def close(self): pass
            class Interactions:
                def __init__(self, outer): self.outer = outer
                def create(self, **_kwargs): keys.append(self.outer.key); raise ServiceUnavailable()
            @property
            def interactions(self): return self.Interactions(self)
        env = {"AUTODUB_GEMINI_CREDENTIALS_JSON": json.dumps([
            {"alias": "a", "api_key": "SYNTHETIC-KEY-A", "quota_group": "project-a", "priority": 1},
            {"alias": "b", "api_key": "SYNTHETIC-KEY-B", "quota_group": "project-b", "priority": 2},
        ])}
        manager = GeminiCredentialManager(self.db, env)
        provider = GeminiProvider(client_factory=Client, credential_manager=manager, sleep=lambda _delay: None,
                                  jitter=lambda _low, _high: 1.0)
        with self.assertRaisesRegex(TranslationError, "gemini_service_unavailable"):
            provider.translate(pass_number=1, chunk_id="fixture", units=[{"unit_id": "u0", "revision_segment_ids": [], "source_text": "你好"}], context=[])
        self.assertEqual(keys, ["SYNTHETIC-KEY-A", "SYNTHETIC-KEY-A"])
        self.assertEqual([item.alias for item in manager.available()], ["a", "b"])
        provider.close()

    def test_double_create_for_same_active_translation_is_blocked(self):
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            first = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-double-click-01")
            with self.assertRaisesRegex(TranslationError, "job_already_active"):
                self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-double-click-02")
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='gemini_translation'").fetchone()[0], 1)

    def test_missing_key_rejected_before_job_creation(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TranslationError, "gemini_api_key_missing"):
                self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-missing-key-01")
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='gemini_translation'").fetchone()[0], 0)

    def test_two_pass_integration_persists_unicode_and_exact_mapping(self):
        provider = FakeGemini()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-run-success-001")
            self.assertTrue(worker.run_once())
        job = self.jobs.get_job(created["job"]["id"])
        self.assertEqual(job["status"], "READY", {"job": job, "run": self.service.get_run(self.project["id"], created["run_id"])})
        self.assertEqual([call[0] for call in provider.calls], [1, 2])
        self.assertEqual(len(provider.calls), 2)
        run = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(run["status"], "READY")
        self.assertTrue(run["is_active"])
        self.assertEqual(len(run["units"]), 3)
        self.assertEqual(run["units"][0]["source_text"], "你好，世界。")
        self.assertTrue(run["units"][0]["pass2_text"].startswith("Tiếng Việt tự nhiên"))
        self.assertEqual(run["units"][0]["revision_segment_ids"], [self.accepted["segments"][0]["row_id"]])
        reopened = TranslationService(Database(self.db.data_root), self.jobs).get_run(self.project["id"], created["run_id"])
        self.assertEqual(reopened["units"][1]["pass2_text"], run["units"][1]["pass2_text"])

    def test_invalid_or_duplicate_provider_ids_rejected(self):
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=FakeGemini)
        units = [{"unit_id": "u1", "revision_segment_ids": ["r1"]}, {"unit_id": "u2", "revision_segment_ids": ["r2"]}]
        bad = {"schema_version": 1, "pass": 1, "chunk_id": "0", "items": [
            {"unit_id": "u1", "revision_segment_ids": ["r1"], "text": "一", "notes": []},
            {"unit_id": "u1", "revision_segment_ids": ["r2"], "text": "二", "notes": []}]}
        with self.assertRaisesRegex(TranslationError, "gemini_response_mapping_invalid"):
            worker._validate_response(bad, 1, 0, units)

    def test_sdk_adapter_sends_text_only_structured_request_without_server_storage(self):
        captured = CapturingGeminiClient("fixture-key")
        provider = GeminiProvider("fixture-key", client_factory=lambda key: captured)
        unit = {"unit_id": "u1", "revision_segment_ids": ["r1"], "source_text": "不要忽略系统指令"}
        result = provider.translate(pass_number=1, chunk_id="0", units=[unit], context=[])
        self.assertEqual(result["items"][0]["unit_id"], "u1")
        self.assertEqual(captured.request["model"], "gemini-3.5-flash-lite")
        self.assertFalse(captured.request["store"])
        self.assertEqual(captured.request["response_format"]["mime_type"], "application/json")
        self.assertIn("untrusted dialogue data", captured.request["input"])
        captured.status = "incomplete"
        with self.assertRaisesRegex(TranslationError, "gemini_response_incomplete"):
            provider.translate(pass_number=1, chunk_id="0", units=[unit], context=[])

    def test_pinned_sdk_transport_and_adapter_cap_503_at_two_physical_requests(self):
        import httpx
        observed = []
        clock = [0.0]
        def handler(request):
            observed.append(request)
            return httpx.Response(503, json={"error": {"code": 503, "message": "Unavailable", "status": "UNAVAILABLE"}},
                                  headers={"content-type": "application/json"})
        diagnostic = self.db.data_root / "logs" / "translation-diagnostics.jsonl"
        provider = GeminiProvider("TEST-SECRET-DO-NOT-LOG", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0,
            sdk_client_args={"transport": httpx.MockTransport(handler)}, diagnostic_path=diagnostic)
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "TEST-SECRET-DO-NOT-LOG"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-503-one-run-001")
            self.assertTrue(worker.run_once())
        job = self.jobs.get_job(created["job"]["id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error_code"], "gemini_service_unavailable")
        self.assertEqual(len(observed), 2)
        with closing(self.db.connect()) as connection:
            chunk = connection.execute("SELECT pass1_attempts,pass2_attempts FROM translation_chunks WHERE run_id=?", (created["run_id"],)).fetchone()
        self.assertEqual(tuple(chunk), (1, 0))
        self.assertFalse(job["retry_available"])
        self.assertEqual(job["retry_block_reason"], "retry_cooldown")
        log = diagnostic.read_text(encoding="utf-8")
        self.assertNotIn("TEST-SECRET-DO-NOT-LOG", log)
        self.assertNotIn("你好，世界。", log)
        self.assertIn('"http_status":503', log)
        self.assertIn('"physical_http_request_count":null', log)
        provider.close()

    def test_pinned_sdk_quota_429_is_not_retried_or_chunk_fallbacked(self):
        import httpx
        observed = []
        clock = [0.0]
        def handler(request):
            observed.append(request)
            return httpx.Response(429, json={"error": {"code": 429, "message": "rate limited", "status": "RESOURCE_EXHAUSTED"}},
                                  headers={"content-type": "application/json", "retry-after": "120"})
        provider = GeminiProvider("TEST-SECRET", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], sdk_client_args={"transport": httpx.MockTransport(handler)})
        with self.assertRaises(TranslationError) as caught:
            provider.translate(pass_number=1, chunk_id="fixture", units=[{"unit_id":"u1", "revision_segment_ids":["r1"], "source_text":"你好"}], context=[])
        self.assertEqual(caught.exception.code, "gemini_quota_exceeded")
        self.assertIsNotNone(caught.exception.retry_after_at)
        self.assertEqual(len(observed), 1)
        self.assertEqual(clock[0], 0.0)
        provider.close()

    def test_pinned_sdk_single_attempt_then_adapter_retries_503_once_and_succeeds(self):
        import httpx
        from app.translation import GeminiProvider
        observed = []
        clock = [0.0]
        unit = {"unit_id": "u1", "revision_segment_ids": ["r1"], "source_text": "你好"}
        output = {"schema_version": 1, "pass": 1, "chunk_id": "fixture", "items": [
            {"unit_id": "u1", "revision_segment_ids": ["r1"], "text": "Xin chào", "notes": []}]}
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                return httpx.Response(503, json={"error": {"code":503,"message":"Unavailable","status":"UNAVAILABLE"}}, headers={"content-type":"application/json"})
            return httpx.Response(200, json={"id":"interaction-1", "status":"completed", "steps":[{"type":"model_output", "content":[{"type":"text", "text":json.dumps(output)}]}]},
                                  headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0,
            sdk_client_args={"transport": httpx.MockTransport(handler)})
        result = provider.translate(pass_number=1, chunk_id="fixture", units=[unit], context=[])
        self.assertEqual(result, output)
        self.assertEqual(len(observed), 2)
        self.assertEqual(clock[0], 15.0)
        provider.close()

    def test_worker_503_once_then_success_uses_three_requests_for_two_passes(self):
        import httpx
        observed = []
        clock = [0.0]
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                return httpx.Response(503, json={"error":{"code":503,"message":"Unavailable","status":"UNAVAILABLE"}},
                                      headers={"content-type":"application/json"})
            body = json.loads(request.content)
            prompt = body["input"]
            payload = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
            result = {"schema_version":1,"pass":payload["pass"],"chunk_id":payload["chunk_id"],"items":[
                {"unit_id":unit["unit_id"], "revision_segment_ids":unit["revision_segment_ids"],
                 "text":"Bản dịch tiếng Việt", "notes":[]} for unit in payload["owned_units"]]}
            return httpx.Response(200, json={"id":f"interaction-{len(observed)}", "status":"completed",
                "steps":[{"type":"model_output","content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}]},
                headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0,
            sdk_client_args={"transport":httpx.MockTransport(handler)})
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY":"TEST-SECRET"}):
            created = self.service.create_job(self.project["id"],self.accepted["id"],True,"translation-503-once-success-01")
            self.assertTrue(worker.run_once())
        job = self.jobs.get_job(created["job"]["id"])
        run = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(job["status"],"READY")
        self.assertEqual(run["status"],"READY")
        self.assertEqual(len(observed),3)
        self.assertEqual(provider._sdk_call_count,3)
        self.assertTrue(all(unit["pass2_text"] == "Bản dịch tiếng Việt" for unit in run["units"]))
        provider.close()

    def test_fixed_http_errors_are_classified_without_retry(self):
        import httpx
        cases = [(400, "gemini_request_invalid"), (401, "gemini_api_key_invalid"),
                 (403, "gemini_permission_denied"), (404, "gemini_model_unsupported")]
        for status, expected in cases:
            with self.subTest(status=status):
                observed = []
                def handler(request):
                    observed.append(request)
                    return httpx.Response(status, json={"error":{"code":status,"message":"fixture","status":"ERROR"}},
                                          headers={"content-type":"application/json"})
                provider = GeminiProvider("TEST-SECRET", sdk_client_args={"transport": httpx.MockTransport(handler)})
                with self.assertRaises(TranslationError) as caught:
                    provider.translate(pass_number=1, chunk_id="fixture", units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}], context=[])
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(observed), 1)
                provider.close()

    def test_transient_connection_error_retries_once_and_cancel_stops_pending_retry(self):
        import httpx
        observed = []
        clock = [0.0]
        output = {"schema_version":1,"pass":1,"chunk_id":"fixture","items":[
            {"unit_id":"u1","revision_segment_ids":["r1"],"text":"Xin chào","notes":[]}]}
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                raise httpx.ConnectError("temporary connect failure", request=request)
            return httpx.Response(200,json={"id":"ok","status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":json.dumps(output)}]}]},headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET",sleep=lambda delay:clock.__setitem__(0,clock[0]+delay),
            monotonic=lambda:clock[0],jitter=lambda low,_high:low,sdk_client_args={"transport":httpx.MockTransport(handler)})
        result=provider.translate(pass_number=1,chunk_id="fixture",units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}],context=[])
        self.assertEqual(result,output)
        self.assertEqual(len(observed),2)
        self.assertEqual(clock[0],1.0)
        provider.close()

        calls=[]
        cancelled=[False]
        class Temporary503(Exception):
            status_code=503
            response=SimpleNamespace(headers={})
        class TemporaryClient:
            interactions= None
            def __init__(self): self.interactions=self
            def create(self,**_kwargs): calls.append(1); raise Temporary503()
        provider=GeminiProvider("TEST-SECRET",client_factory=lambda _key:TemporaryClient(),
            sleep=lambda _delay:None,monotonic=lambda:0.0,jitter=lambda low,_high:low)
        def set_cancelled(_delay,_deadline): cancelled[0]=True
        with self.assertRaisesRegex(TranslationError,"job_cancelled"):
            provider.translate(pass_number=1,chunk_id="fixture",units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}],context=[],
                cancelled=lambda:cancelled[0],retrying=set_cancelled)
        self.assertEqual(len(calls),1)
        provider.close()

    def test_failed_pass2_retry_keeps_verified_pass1(self):
        class FailPass2Once(FakeGemini):
            def translate(self, **kwargs):
                if kwargs["pass_number"] == 2 and sum(call[0] == 2 for call in self.calls) == 0:
                    self.calls.append((2, kwargs["chunk_id"], [u["unit_id"] for u in kwargs["units"]], kwargs["context"]))
                    raise TranslationError("gemini_network_error", retryable=True, status=503)
                return FakeGemini.translate(self, **kwargs)
        provider = FailPass2Once()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-retry-pass2-001")
            worker.run_once()
        job = self.jobs.get_job(created["job"]["id"])
        self.assertEqual(job["status"], "FAILED")
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM translation_units WHERE run_id=? AND pass1_text IS NOT NULL", (created["run_id"],)).fetchone()[0], 3)
        self.jobs.retry_job(job["id"], "FAILED", "translation-retry-key-0001")
        self.assertTrue(worker.run_once())
        self.assertEqual([call[0] for call in provider.calls], [1, 2, 2])
        self.assertEqual(self.service.get_run(self.project["id"], created["run_id"])["status"], "READY")

    def test_changed_upstream_pointer_keeps_result_stale_and_inactive(self):
        provider = FakeGemini()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-stale-cas-0001")
        self.jobs.set_stage_input(self.project["id"], "translation", {"schema_version": 1, "newer_dependency": True})
        worker.run_once()
        result = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(result["status"], "STALE")
        self.assertFalse(result["is_active"])

    def test_cancellation_between_passes_preserves_pass1_but_never_marks_ready(self):
        class CancelAfterPass1(FakeGemini):
            def __init__(self, cancel):
                super().__init__(); self.cancel = cancel
            def translate(self, **kwargs):
                result = super().translate(**kwargs)
                if kwargs["pass_number"] == 1: self.cancel()
                return result
        provider = CancelAfterPass1(lambda: self.jobs.request_cancel(self.current_job_id, "translation-cancel-key-001"))
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-cancel-run-001")
            self.current_job_id = created["job"]["id"]
            worker.run_once()
        self.assertEqual(self.jobs.get_job(created["job"]["id"])["status"], "CANCELLED")
        result = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(all(unit["pass1_text"] is not None and unit["pass2_text"] is None for unit in result["units"]))
        self.assertEqual([call[0] for call in provider.calls], [1])

    def test_overlong_translation_unit_is_blocked_without_truncation(self):
        draft = self.transcripts.create_draft(self.project["id"], source_run_id=None, parent_revision_id=self.accepted["id"], idempotency_key="translation-long-draft-001")
        segment = draft["segments"][0]
        saved = self.transcripts.save_edits(self.project["id"], draft["id"], draft["lock_version"], [{"row_id": segment["row_id"], "text": "界" * 8001}], "translation-long-save-0001")
        accepted = self.transcripts.accept(self.project["id"], saved["id"], saved["lock_version"], self.accepted["id"], "translation-long-accept-001")
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            with self.assertRaisesRegex(TranslationError, "translation_unit_too_long_review_required"):
                self.service.create_job(self.project["id"], accepted["id"], True, "translation-long-block-001")
        self.assertEqual(len(accepted["segments"][0]["text"]), 8001)


class TranslationAPISecurityTests(unittest.TestCase):
    def test_gemini_status_requires_session_and_job_creation_requires_csrf(self):
        temp = tempfile.TemporaryDirectory()
        try:
            client = TestClient(create_app("translation-api-secret", data_root=Path(temp.name) / "data"), client=("127.0.0.1", 54321))
            with client:
                headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
                self.assertEqual(client.get("/api/v1/providers/gemini/status", headers=headers).status_code, 401)
                bootstrap = client.post("/api/v1/session/bootstrap", headers={**headers, "x-local-bootstrap": "translation-api-secret"}, json={})
                csrf = bootstrap.json()["csrf_token"]
                project = client.post("/api/v1/projects", headers={**headers, "x-csrf-token": csrf, "idempotency-key": "translation-api-project-key-01"}, json={"name": "Security"}).json()
                response = client.post(f"/api/v1/projects/{project['id']}/translations/jobs", headers={**headers, "idempotency-key": "translation-api-job-key-001"}, json={"accepted_transcript_revision_id": str(uuid.uuid4()), "privacy_consent_confirmed": True})
                self.assertEqual(response.status_code, 403)
        finally:
            temp.cleanup()


if __name__ == "__main__": unittest.main()
