from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
from app.translation import TranslationArtifactValidator, TranslationError, TranslationService, TranslationWorker, normalize_translation_unit
from app.translation import GeminiProvider
from app.gemini_credentials import GeminiCredentialManager
from app.translation_revisions import TranslationRevisionError, TranslationRevisionService


class TranslationMigrationTests(unittest.TestCase):
    def test_v17_adds_translation_revision_contract_repeatably_without_losing_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data")
            database.initialize(target_version=15)
            project, _ = database.create_project("migration fixture", "translation-v16-project-001")
            database.initialize()
            database.initialize()
            with closing(database.connect()) as connection:
                run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(translation_runs)")}
                unit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(translation_units)")}
                revision_tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                applied = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                preserved = connection.execute("SELECT id FROM projects WHERE id=?", (project["id"],)).fetchone()
            self.assertTrue({"pass_strategy", "normalization_policy_version"}.issubset(run_columns))
            self.assertTrue({"final_text", "final_notes_json"}.issubset(unit_columns))
            self.assertTrue({"translation_revisions", "translation_revision_units", "project_active_translation_revisions"}.issubset(revision_tables))
            self.assertEqual(applied, 17)
            self.assertIsNotNone(preserved)


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
    def __init__(self): self.calls = []; self.call_history = []

    def translate(self, *, pass_number, chunk_id, units, context, **_options):
        self.calls.append((pass_number, chunk_id, [unit["unit_id"] for unit in units], context))
        self.call_history.append({"model_id": "synthetic-fake", "api_type": "test", "api_version": "local",
            "sdk_version": "test", "wire_protocol_version": "test", "chunk_id": chunk_id,
            "pass_number": pass_number, "sdk_call_count": 0, "physical_http_request_count": 0,
            "unit_count_requested": len(units), "unit_count_received": len(units), "latency_ms": 0,
            "status": "COMPLETED", "error_code": None})
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
        return SimpleNamespace(status=self.status, output_text=json.dumps({"items": [
            {"id": unit["id"], "text": "Xin chào"} for unit in payload["units"]]}))


class GeminiWireContractTests(unittest.TestCase):
    def test_legacy_two_pass_artifact_without_v16_fields_remains_readable(self):
        manifest = {"schema_version": 1, "run_id": str(uuid.uuid4()), "input_fingerprint": "a" * 64,
            "units": [{"unit_id": "legacy-unit", "position": 0, "revision_segment_ids": ["revision"],
                "source_segment_ids": ["source"], "source_text": "source", "pass1_text": "draft",
                "pass2_text": "final"}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-translation.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = TranslationArtifactValidator().validate(path)
        self.assertEqual(result["unit_count"], 1)

    def test_terminal_punctuation_normalization_preserves_content_and_closers(self):
        cases = {
            "你好。": "你好", "Really?!」": "Really」", "他说：“你好！”": "他说：“你好”",
            "值是 3.14。": "值是 3.14", "增长 12.5%！": "增长 12.5%", "版本 v1.2.3。": "版本 v1.2.3",
            "约 1,000。": "约 1,000", "(继续……)": "(继续)", "Hello?   ": "Hello",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_translation_unit(source), expected)
        with self.assertRaisesRegex(TranslationError, "gemini_response_empty_translation"):
            normalize_translation_unit("？！")

    class Client:
        def __init__(self, response):
            self.interactions = self
            self.response = response
        def create(self, **_kwargs):
            return SimpleNamespace(status="completed", output_text=self.response)

    class ContentModels:
        def __init__(self): self.request = None
        def generate_content(self, **kwargs):
            self.request = kwargs
            payload = json.loads(kwargs["contents"].split("INPUT_JSON:\n", 1)[1])
            return SimpleNamespace(text=json.dumps({"items": [{"id": unit["id"], "text": "Xin chào"} for unit in payload["units"]]}),
                candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
                usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=4, total_token_count=14),
                sdk_http_response=SimpleNamespace(headers={"x-goog-request-id": "synthetic-request"}))

    class ContentClient:
        def __init__(self): self.models = GeminiWireContractTests.ContentModels()

    def translate_wire(self, value):
        provider = GeminiProvider("fixture-key", client_factory=lambda _key: self.Client(value), api_type="interactions")
        units = [
            {"unit_id": "durable-a", "revision_segment_ids": ["rev-a"], "source_text": "你好"},
            {"unit_id": "durable-b", "revision_segment_ids": ["rev-b"], "source_text": "谢谢"},
        ]
        return provider.translate(pass_number=1, chunk_id="internal", units=units, context=[])

    def test_reordered_short_wire_ids_reconcile_to_durable_units(self):
        output = self.translate_wire(json.dumps({"items": [{"id": 2, "text": "Cảm ơn"}, {"id": 1, "text": "Xin chào"}]}))
        self.assertEqual([item["unit_id"] for item in output["items"]], ["durable-a", "durable-b"])
        self.assertEqual([item["text"] for item in output["items"]], ["Xin chào", "Cảm ơn"])
        self.assertEqual([item["revision_segment_ids"] for item in output["items"]], [["rev-a"], ["rev-b"]])

    def test_generate_content_uses_supported_json_schema_and_records_route(self):
        captured = self.ContentClient()
        provider = GeminiProvider("fixture-key", api_type="generate_content", client_factory=lambda _key: captured)
        output = provider.translate(pass_number=1, pass_strategy="SINGLE_PASS", chunk_id="internal", units=[
            {"unit_id": "durable-a", "revision_segment_ids": ["rev-a"], "source_text": "你好", "start_us": 100_000, "end_us": 1_234_500}], context=[])
        config = captured.models.request["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_json_schema)
        self.assertEqual(output["items"][0]["unit_id"], "durable-a")
        self.assertEqual(provider.last_call_metadata["provider_request_id"], "synthetic-request")
        self.assertEqual(provider.last_call_metadata["input_tokens"], 10)
        request_payload = json.loads(captured.models.request["contents"].split("INPUT_JSON:\n", 1)[1])
        self.assertEqual(request_payload["units"][0]["duration_ms"], 1134)
        self.assertIn("maximize brevity", captured.models.request["contents"])
        self.assertIn("Never use terminal punctuation", captured.models.request["contents"])

    def test_single_pass_requires_valid_timing_before_request(self):
        captured = self.ContentClient()
        provider = GeminiProvider("fixture-key", api_type="generate_content", client_factory=lambda _key: captured)
        with self.assertRaisesRegex(TranslationError, "translation_invalid_unit_timing"):
            provider.translate(pass_number=1, pass_strategy="SINGLE_PASS", chunk_id="internal", units=[
                {"unit_id": "durable-a", "revision_segment_ids": ["rev-a"], "source_text": "你好", "start_us": 5, "end_us": 5}], context=[])
        self.assertIsNone(captured.models.request)

    def test_legacy_two_pass_pass_two_keeps_draft_and_duration_contract(self):
        client = CapturingGeminiClient("fixture-key")
        provider = GeminiProvider("fixture-key", api_type="interactions", client_factory=lambda _key: client)
        provider.translate(pass_number=2, chunk_id="legacy", units=[{"unit_id": "durable-a", "revision_segment_ids": ["rev-a"],
            "source_text": "你好", "draft_vi": "Chào bạn", "start_us": 100, "end_us": 1_000_100}], context=[],
            pass_strategy="TWO_PASS", wire_protocol_version="translation-wire-v2")
        payload = json.loads(client.request["input"].split("INPUT_JSON:\n", 1)[1])
        self.assertEqual(payload["units"][0]["draft_vi"], "Chào bạn")
        self.assertEqual(payload["units"][0]["duration_ms"], 1000)
        self.assertEqual(provider.call_history[0]["wire_protocol_version"], "translation-wire-v2")

    def test_multigenre_multilingual_fixture_keeps_exact_wire_mapping(self):
        fixture = [
            "我不是说这个。", "Is that really funny?", "Use the 12 V adapter at 5.2 A.",
            "The lesson begins at nine tomorrow.", "Revenue rose 12.5% in Q3.",
            "请先断开电源，再打开外壳。", "She said, ‘I will come back.’", "Nguyễn An reviewed the device.",
        ]
        client = self.ContentClient()
        provider = GeminiProvider("fixture-key", api_type="generate_content", client_factory=lambda _key: client)
        units = [{"unit_id": f"genre-{i}", "revision_segment_ids": [f"revision-{i}"],
                  "source_text": text, "start_us": i * 1_000_000, "end_us": (i + 1) * 1_000_000,
                  "position": i} for i, text in enumerate(fixture)]
        result = provider.translate(pass_number=1, pass_strategy="SINGLE_PASS", chunk_id="genres", units=units, context=[])
        sent_prompt = client.models.request["contents"]
        self.assertTrue(all(text in sent_prompt for text in fixture))
        self.assertEqual([item["unit_id"] for item in result["items"]], [item["unit_id"] for item in units])
        # This fixture verifies payload completeness and ID reconciliation only, not translation quality.

    def test_wire_validator_distinguishes_id_and_payload_failures(self):
        cases = [
            ("{\"items\":[", "gemini_response_truncated"),
            ("{broken}", "gemini_response_malformed_json"),
            (json.dumps({"items": [{"id": 1, "text": "一"}]}), "gemini_response_missing_ids"),
            (json.dumps({"items": [{"id": 1, "text": "一"}, {"id": 1, "text": "二"}]}), "gemini_response_duplicate_ids"),
            (json.dumps({"items": [{"id": 1, "text": "一"}, {"id": 3, "text": "三"}]}), "gemini_response_unexpected_ids"),
            (json.dumps({"items": [{"id": True, "text": "一"}, {"id": 2, "text": "二"}]}), "gemini_response_invalid_id_type"),
            (json.dumps({"items": [{"id": 1, "text": " "}, {"id": 2, "text": "二"}]}), "gemini_response_empty_translation"),
            ('{"items":[{"id":1,"text":"\\ud800"},{"id":2,"text":"二"}]}', "gemini_response_invalid_unicode"),
            (json.dumps({"items": [{"id": 1, "text": "一", "extra": True}, {"id": 2, "text": "二"}]}), "gemini_response_schema_mismatch"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(TranslationError, expected):
                self.translate_wire(raw)

    def test_pass2_batch_planner_uses_actual_draft_and_keeps_every_unit_once(self):
        units = [{"unit_id": f"u{i}", "source_text": "短句", "draft_vi": "越" * 4000} for i in range(3)]
        batches = TranslationService.chunk_pass2_units(units, [])
        self.assertEqual([unit["unit_id"] for batch in batches for unit in batch], ["u0", "u1", "u2"])
        self.assertEqual(len(batches), 3)
        with self.assertRaisesRegex(TranslationError, "translation_unit_too_long_review_required"):
            TranslationService.chunk_pass2_units([{"unit_id": "long", "source_text": "x", "draft_vi": "越" * 14000}], [])
        fitted = TranslationService.fit_pass2_context(
            [{"unit_id": "owned", "source_text": "x", "draft_vi": "Xin chào", "position": 50}],
            [{"position": 0, "source_text": "遠" * 40000, "draft_vi": "Xa"},
             {"position": 49, "source_text": "附近的句子", "draft_vi": "Câu liền trước"}],
        )
        self.assertEqual([item["position"] for item in fitted], [49])


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
        # Existing tests exercise backward-compatible two-pass execution explicitly.
        self.service.pass_strategy = "TWO_PASS"
        self.artifacts.validators["translation_json"] = __import__("app.translation", fromlist=["TranslationArtifactValidator"]).TranslationArtifactValidator(self.db)

    def tearDown(self): self.temp.cleanup()

    def _ready_single_pass_translation(self):
        self.service.pass_strategy = "SINGLE_PASS"
        provider = FakeGemini()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, f"translation-editor-{uuid.uuid4().hex[:20]}")
            self.assertTrue(worker.run_once())
        self.assertEqual(self.service.get_run(self.project["id"], created["run_id"])["status"], "READY")
        return created["run_id"], provider

    def test_translation_bulk_draft_approval_restart_and_unit_identity(self):
        run_id, provider = self._ready_single_pass_translation()
        revisions = TranslationRevisionService(self.db)
        editor = revisions.editor_state(self.project["id"], run_id)
        self.assertTrue(editor["run_is_current"])
        original = editor["original_units"]
        draft = revisions.create_draft(self.project["id"], run_id)
        self.assertEqual(draft["source_kind"], "PROVIDER_OUTPUT")
        edits = [{"unit_id": original[0]["unit_id"], "text": "  Bản dịch đã sửa.  "},
                 {"unit_id": original[2]["unit_id"], "text": "Câu cuối mới?!"}]
        saved = revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], edits, "translation-bulk-save-key-0001")
        self.assertEqual(saved["lock_version"], 2)
        self.assertEqual(saved["units"][0]["final_text"], "Bản dịch đã sửa")
        self.assertEqual(saved["units"][2]["final_text"], "Câu cuối mới")
        self.assertEqual(saved["units"][1]["final_text"], original[1]["final_text"])
        with closing(self.db.connect()) as connection:
            provider_output = connection.execute("SELECT final_text FROM translation_units WHERE run_id=? AND unit_id=?", (run_id, original[0]["unit_id"])).fetchone()[0]
        self.assertEqual(provider_output, original[0]["final_text"])
        restarted = TranslationRevisionService(Database(self.db.data_root)).editor_state(self.project["id"], run_id)
        self.assertEqual(restarted["draft"]["units"][0]["final_text"], "Bản dịch đã sửa")
        tts_pointer = self.jobs.set_stage_input(self.project["id"], "tts", {"schema_version": 1, "voice": "fixture"})
        accepted = revisions.accept_draft(self.project["id"], run_id, draft["id"], saved["lock_version"], None, "translation-accept-draft-key-01")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertTrue(accepted["is_active"])
        self.assertEqual(accepted["units"][0]["logical_unit_key"], original[0]["logical_unit_key"])
        self.assertEqual(accepted["units"][1]["unit_fingerprint"], original[1]["unit_fingerprint"])
        with closing(self.db.connect()) as connection:
            after = connection.execute("SELECT input_generation,input_fingerprint FROM stage_pointers WHERE project_id=? AND stage='tts'", (self.project["id"],)).fetchone()
            accepted_count = connection.execute("SELECT COUNT(*) FROM translation_revisions WHERE run_id=? AND status='ACCEPTED'", (run_id,)).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE translation_revision_units SET final_text='Tampered' WHERE revision_id=? AND unit_id=?", (accepted["id"], original[0]["unit_id"]))
        self.assertEqual(after["input_generation"], tts_pointer["input_generation"] + 1)
        self.assertEqual(accepted_count, 1)
        draft2 = revisions.create_draft(self.project["id"], run_id, accepted["id"])
        self.assertEqual(draft2["source_kind"], "ACCEPTED_REVISION")
        self.assertEqual(draft2["units"][0]["final_text"], accepted["units"][0]["final_text"])
        self.assertEqual(revisions.editor_state(self.project["id"], run_id)["accepted"]["id"], accepted["id"])
        saved2 = revisions.save_draft(self.project["id"], run_id, draft2["id"], draft2["lock_version"],
            [{"unit_id": original[1]["unit_id"], "text": "Thay đổi câu hai."}], "translation-bulk-save-key-0002")
        accepted2 = revisions.accept_draft(self.project["id"], run_id, draft2["id"], saved2["lock_version"], accepted["id"], "translation-accept-draft-key-02")
        self.assertNotEqual(accepted2["id"], accepted["id"])
        self.assertEqual(revisions.editor_state(self.project["id"], run_id)["accepted"]["id"], accepted2["id"])
        self.assertEqual(provider.calls.__len__(), 1)

    def test_translation_bulk_save_rolls_back_invalid_batch_and_rejects_stale_or_duplicate_units(self):
        run_id, _provider = self._ready_single_pass_translation()
        revisions = TranslationRevisionService(self.db)
        draft = revisions.create_draft(self.project["id"], run_id)
        before = [unit["final_text"] for unit in draft["units"]]
        with self.assertRaisesRegex(TranslationRevisionError, "gemini_response_empty_translation"):
            revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], [
                {"unit_id": draft["units"][0]["unit_id"], "text": "Không được commit"},
                {"unit_id": draft["units"][1]["unit_id"], "text": "   "}], "translation-bulk-rollback-key-01")
        unchanged = revisions.editor_state(self.project["id"], run_id)["draft"]
        self.assertEqual([unit["final_text"] for unit in unchanged["units"]], before)
        self.assertEqual(unchanged["lock_version"], draft["lock_version"])
        with self.assertRaisesRegex(TranslationRevisionError, "duplicate_translation_unit"):
            revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], [
                {"unit_id": draft["units"][0]["unit_id"], "text": "Một"}, {"unit_id": draft["units"][0]["unit_id"], "text": "Hai"}], "translation-bulk-duplicate-key-01")
        saved = revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], [
            {"unit_id": draft["units"][0]["unit_id"], "text": "Đã lưu"}], "translation-bulk-cas-first-key-01")
        with self.assertRaisesRegex(TranslationRevisionError, "translation_revision_conflict"):
            revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], [
                {"unit_id": draft["units"][0]["unit_id"], "text": "Ghi đè"}], "translation-bulk-cas-stale-key-01")
        self.assertEqual(saved["units"][0]["final_text"], "Đã lưu")
        no_change = revisions.save_draft(self.project["id"], run_id, draft["id"], saved["lock_version"], [], "translation-bulk-nochange-key-01")
        self.assertEqual(no_change["lock_version"], saved["lock_version"])

    def test_bulk_save_handles_100_500_and_1000_unit_fixtures(self):
        run_id, _provider = self._ready_single_pass_translation()
        with closing(self.db.connect()) as connection:
            for position in range(3, 1000):
                unit_id = str(uuid.uuid4())
                connection.execute("""INSERT INTO translation_units(run_id,project_id,unit_id,logical_unit_key,position,chunk_no,
                    revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us,final_text,status)
                    VALUES(?,?,?,?,?,0,?,?,?,?,?,?, 'READY')""", (run_id, self.project["id"], unit_id, str(uuid.uuid4()), position,
                    json.dumps([str(uuid.uuid4())]), json.dumps([str(uuid.uuid4())]), f"fixture source {position}", position * 1000 + 1,
                    position * 1000 + 2, f"Fixture {position}"))
        revisions = TranslationRevisionService(self.db)
        draft = revisions.create_draft(self.project["id"], run_id)
        self.assertEqual(len(draft["units"]), 1000)
        for batch_size in (100, 500, 1000):
            edits = [{"unit_id": unit["unit_id"], "text": f"Fixture edit {index}"} for index, unit in enumerate(draft["units"][:batch_size])]
            draft = revisions.save_draft(self.project["id"], run_id, draft["id"], draft["lock_version"], edits,
                f"translation-scale-batch-{batch_size:04d}-key")
            self.assertEqual(sum(unit["text_origin"] == "USER_EDIT" for unit in draft["units"]), batch_size)

    def test_translation_revision_api_requires_session_csrf_and_bulk_saves_once(self):
        run_id, _provider = self._ready_single_pass_translation()
        app = create_app("translation-revision-api-secret", data_root=self.db.data_root)
        with TestClient(app, client=("127.0.0.1", 54322)) as client:
            headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:5173", "sec-fetch-site": "same-origin"}
            base_url = f"/api/v1/projects/{self.project['id']}/translations/runs/{run_id}"
            self.assertEqual(client.get(base_url + "/editor", headers=headers).status_code, 401)
            boot = client.post("/api/v1/session/bootstrap", headers={**headers, "x-local-bootstrap": "translation-revision-api-secret"}, json={})
            csrf = boot.json()["csrf_token"]
            denied = client.post(base_url + "/drafts", headers=headers, json={"base_accepted_revision_id": None})
            self.assertEqual(denied.status_code, 403)
            write_headers = {**headers, "x-csrf-token": csrf, "idempotency-key": "translation-api-create-draft-key-01"}
            created = client.post(base_url + "/drafts", headers=write_headers, json={"base_accepted_revision_id": None})
            self.assertEqual(created.status_code, 201)
            draft = created.json()
            patch_headers = {**headers, "x-csrf-token": csrf, "idempotency-key": "translation-api-bulk-save-key-0001"}
            saved = client.patch(base_url + f"/drafts/{draft['id']}/units", headers=patch_headers, json={
                "expected_lock_version": draft["lock_version"], "edits": [
                    {"unit_id": draft["units"][0]["unit_id"], "text": "API sửa câu một."},
                    {"unit_id": draft["units"][2]["unit_id"], "text": "API sửa câu ba."}]})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["lock_version"], draft["lock_version"] + 1)
            self.assertEqual(saved.json()["units"][0]["final_text"], "API sửa câu một")

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

    def test_credential_quota_cooldown_is_group_scoped_and_same_alias_can_revalidate(self):
        env = {"AUTODUB_GEMINI_CREDENTIALS_JSON": json.dumps([
            {"alias": "key-a", "api_key": "SYNTHETIC-KEY-A", "quota_group": "same-project", "priority": 1},
            {"alias": "key-b", "api_key": "SYNTHETIC-KEY-B", "quota_group": "same-project", "priority": 2},
        ])}
        manager = GeminiCredentialManager(self.db, env)
        manager.mark_quota_limited(manager.configured()[0], "2999-01-01T00:00:00Z")
        self.assertEqual(manager.available(), [])
        manager.mark_auth_failed(manager.configured()[0], "gemini_api_key_invalid")
        env["AUTODUB_GEMINI_CREDENTIALS_JSON"] = json.dumps([
            {"alias": "key-a", "api_key": "SYNTHETIC-KEY-A-ROTATED", "quota_group": "same-project", "priority": 1},
            {"alias": "key-b", "api_key": "SYNTHETIC-KEY-B", "quota_group": "same-project", "priority": 2},
        ])
        env["AUTODUB_GEMINI_REVALIDATE_ALIASES"] = "key-a"
        available = manager.available()
        self.assertEqual([item.alias for item in available], ["key-a"])
        self.assertEqual(env["AUTODUB_GEMINI_REVALIDATE_ALIASES"], "")

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
                    return SimpleNamespace(status="completed", output_text=json.dumps({"items": [{"id": 1, "text": "Xin chào"}]}))
            @property
            def interactions(self): return self.Interactions(self)
        env = {"AUTODUB_GEMINI_CREDENTIALS_JSON": json.dumps([
            {"alias": "a", "api_key": "SYNTHETIC-KEY-A", "quota_group": "project-a", "priority": 1},
            {"alias": "b", "api_key": "SYNTHETIC-KEY-B", "quota_group": "project-b", "priority": 2},
        ])}
        manager = GeminiCredentialManager(self.db, env)
        provider = GeminiProvider(client_factory=lambda key: Client(key), credential_manager=manager, api_type="interactions")
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
        provider = GeminiProvider(client_factory=Client, credential_manager=manager, api_type="interactions", sleep=lambda _delay: None,
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
        with closing(self.db.connect()) as connection:
            calls = connection.execute("SELECT pass_number,model_id,status FROM translation_provider_calls WHERE run_id=? ORDER BY pass_number", (created["run_id"],)).fetchall()
        self.assertEqual([(item["pass_number"], item["model_id"], item["status"]) for item in calls],
                         [(1, "gemini-3.1-flash-lite", "COMPLETED"), (2, "gemini-3.1-flash-lite", "COMPLETED")])

    def test_single_pass_persists_only_final_output_and_uses_one_generation(self):
        self.service.pass_strategy = "SINGLE_PASS"
        provider = FakeGemini()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-single-pass-001")
            self.assertTrue(worker.run_once())
        run = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(run["status"], "READY")
        self.assertEqual(run["pass_strategy"], "SINGLE_PASS")
        with closing(self.db.connect()) as connection:
            snapshot = json.loads(connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (created["job"]["id"],)).fetchone()[0])
        self.assertEqual(snapshot["pass_strategy"], "SINGLE_PASS")
        self.assertEqual(snapshot["normalization_policy_version"], "terminal-punctuation-v1")
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(all(unit["pass1_text"] is None and unit["pass2_text"] is None for unit in run["units"]))
        self.assertTrue(all(unit["final_text"] for unit in run["units"]))

    def test_incompatible_unfinished_run_fails_before_provider_factory(self):
        provider_called = []
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider_called.append(True))
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-incompatible-contract-001")
        with closing(self.db.connect()) as connection:
            connection.execute("DROP TRIGGER translation_run_input_immutable")
            connection.execute("UPDATE translation_runs SET provider_sdk_version='2.34.0' WHERE id=?", (created["run_id"],))
        self.assertTrue(worker.run_once())
        self.assertEqual(provider_called, [])
        self.assertEqual(self.service.get_run(self.project["id"], created["run_id"])["error_code"], "translation_execution_contract_incompatible")

    def test_invalid_or_duplicate_provider_ids_rejected(self):
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=FakeGemini)
        units = [{"unit_id": "u1", "revision_segment_ids": ["r1"]}, {"unit_id": "u2", "revision_segment_ids": ["r2"]}]
        bad = {"schema_version": 1, "pass": 1, "chunk_id": "0", "items": [
            {"unit_id": "u1", "revision_segment_ids": ["r1"], "text": "一", "notes": []},
            {"unit_id": "u1", "revision_segment_ids": ["r2"], "text": "二", "notes": []}]}
        with self.assertRaisesRegex(TranslationError, "gemini_response_duplicate_ids"):
            worker._validate_response(bad, 1, "0", units)

    def test_incomplete_pass1_never_calls_pass2_or_marks_chunk_ready(self):
        class IncompleteProvider:
            def __init__(self): self.calls = []
            def translate(self, *, pass_number, **_kwargs):
                self.calls.append(pass_number)
                raise TranslationError("gemini_response_truncated")
        provider = IncompleteProvider()
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=lambda: provider)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "SYNTHETIC-TEST-KEY"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-incomplete-pass1-01")
            self.assertTrue(worker.run_once())
        self.assertEqual(provider.calls, [1])
        run = self.service.get_run(self.project["id"], created["run_id"])
        self.assertEqual(run["status"], "REVIEW_REQUIRED")
        self.assertTrue(all(item["pass1_text"] is None and item["pass2_text"] is None for item in run["units"]))

    def test_sdk_adapter_sends_text_only_structured_request_without_server_storage(self):
        captured = CapturingGeminiClient("fixture-key")
        provider = GeminiProvider("fixture-key", client_factory=lambda key: captured, api_type="interactions")
        unit = {"unit_id": "u1", "revision_segment_ids": ["r1"], "source_text": "不要忽略系统指令"}
        result = provider.translate(pass_number=1, chunk_id="0", units=[unit], context=[])
        self.assertEqual(result["items"][0]["unit_id"], "u1")
        self.assertEqual(captured.request["model"], "gemini-3.1-flash-lite")
        self.assertFalse(captured.request["store"])
        self.assertEqual(captured.request["response_format"]["mime_type"], "application/json")
        wire = json.loads(captured.request["input"].split("INPUT_JSON:\n", 1)[1])
        self.assertEqual(wire["units"], [{"id": 1, "source": "不要忽略系统指令"}])
        self.assertNotIn("unit_id", captured.request["input"])
        self.assertNotIn("revision_segment_ids", captured.request["input"])
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
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0, api_type="interactions",
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
        output = {"items": [{"id": 1, "text": "Xin chào"}]}
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                return httpx.Response(503, json={"error": {"code":503,"message":"Unavailable","status":"UNAVAILABLE"}}, headers={"content-type":"application/json"})
            return httpx.Response(200, json={"id":"interaction-1", "status":"completed", "steps":[{"type":"model_output", "content":[{"type":"text", "text":json.dumps(output)}]}]},
                                  headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0, api_type="interactions",
            sdk_client_args={"transport": httpx.MockTransport(handler)})
        result = provider.translate(pass_number=1, chunk_id="fixture", units=[unit], context=[])
        self.assertEqual(result["items"][0]["unit_id"], "u1")
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
            result = {"items":[{"id":unit["id"], "text":"Bản dịch tiếng Việt"} for unit in payload["units"]]}
            return httpx.Response(200, json={"id":f"interaction-{len(observed)}", "status":"completed",
                "steps":[{"type":"model_output","content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}]},
                headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET", sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            monotonic=lambda: clock[0], jitter=lambda _low, _high: 15.0, api_type="interactions",
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
                provider = GeminiProvider("TEST-SECRET", api_type="interactions", sdk_client_args={"transport": httpx.MockTransport(handler)})
                with self.assertRaises(TranslationError) as caught:
                    provider.translate(pass_number=1, chunk_id="fixture", units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}], context=[])
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(observed), 1)
                provider.close()

    def test_transient_connection_error_retries_once_and_cancel_stops_pending_retry(self):
        import httpx
        observed = []
        clock = [0.0]
        output = {"items":[{"id":1,"text":"Xin chào"}]}
        def handler(request):
            observed.append(request)
            if len(observed) == 1:
                raise httpx.ConnectError("temporary connect failure", request=request)
            return httpx.Response(200,json={"id":"ok","status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":json.dumps(output)}]}]},headers={"content-type":"application/json"})
        provider = GeminiProvider("TEST-SECRET",api_type="interactions",sleep=lambda delay:clock.__setitem__(0,clock[0]+delay),
            monotonic=lambda:clock[0],jitter=lambda low,_high:low,sdk_client_args={"transport":httpx.MockTransport(handler)})
        result=provider.translate(pass_number=1,chunk_id="fixture",units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}],context=[])
        self.assertEqual(result["items"][0]["unit_id"], "u1")
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
        provider=GeminiProvider("TEST-SECRET",api_type="interactions",client_factory=lambda _key:TemporaryClient(),
            sleep=lambda _delay:None,monotonic=lambda:0.0,jitter=lambda low,_high:low)
        def set_cancelled(_delay,_deadline): cancelled[0]=True
        with self.assertRaisesRegex(TranslationError,"job_cancelled"):
            provider.translate(pass_number=1,chunk_id="fixture",units=[{"unit_id":"u1","revision_segment_ids":["r1"],"source_text":"你好"}],context=[],
                cancelled=lambda:cancelled[0],retrying=set_cancelled)
        self.assertEqual(len(calls),1)
        provider.close()

    def test_failed_pass2_retry_keeps_verified_pass1(self):
        failed_once = [False]
        class FailPass2Once(FakeGemini):
            def translate(self, **kwargs):
                if kwargs["pass_number"] == 2 and not failed_once[0]:
                    failed_once[0] = True
                    self.calls.append((2, kwargs["chunk_id"], [u["unit_id"] for u in kwargs["units"]], kwargs["context"]))
                    self.call_history.append({"model_id": "synthetic-fake", "api_type": "test", "api_version": "local",
                        "sdk_version": "test", "wire_protocol_version": "test", "chunk_id": kwargs["chunk_id"],
                        "pass_number": 2, "sdk_call_count": 0, "physical_http_request_count": 0,
                        "unit_count_requested": len(kwargs["units"]), "unit_count_received": 0, "latency_ms": 0,
                        "status": "FAILED", "error_code": "gemini_network_error"})
                    raise TranslationError("gemini_network_error", retryable=True, status=503)
                return FakeGemini.translate(self, **kwargs)
        providers = []
        def factory():
            provider = FailPass2Once()
            providers.append(provider)
            return provider
        worker = TranslationWorker(self.service, self.artifacts, provider_factory=factory)
        with patch.dict(os.environ, {"AUTODUB_GEMINI_API_KEY": "NOT-LOGGED"}):
            created = self.service.create_job(self.project["id"], self.accepted["id"], True, "translation-retry-pass2-001")
            worker.run_once()
        job = self.jobs.get_job(created["job"]["id"])
        self.assertEqual(job["status"], "FAILED")
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM translation_units WHERE run_id=? AND pass1_text IS NOT NULL", (created["run_id"],)).fetchone()[0], 3)
        self.jobs.retry_job(job["id"], "FAILED", "translation-retry-key-0001")
        self.assertTrue(worker.run_once())
        self.assertEqual([call[0] for provider in providers for call in provider.calls], [1, 2, 2])
        self.assertEqual(self.service.get_run(self.project["id"], created["run_id"])["status"], "READY")
        with closing(self.db.connect()) as connection:
            calls = connection.execute("SELECT * FROM translation_provider_calls WHERE run_id=? ORDER BY rowid", (created["run_id"],)).fetchall()
            artifact = connection.execute("SELECT managed_relative_path FROM artifacts WHERE id=(SELECT artifact_id FROM translation_runs WHERE id=?)", (created["run_id"],)).fetchone()
        self.assertEqual(len(calls), 3)
        self.assertEqual([call["pass_number"] for call in calls], [1, 2, 2])
        self.assertEqual(len({call["job_attempt_id"] for call in calls}), 2)
        self.assertTrue(all(call["model_id"] == "gemini-3.1-flash-lite" and call["api_type"] == "generate_content"
            and call["api_version"] == "v1beta" and call["sdk_version"] == "3.6.1"
            and call["prompt_version"] == "translation-vi-two-pass-v2" and call["wire_protocol_version"] == "translation-wire-v2" for call in calls))
        manifest = json.loads((self.db.data_root / artifact["managed_relative_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["provider_calls"], [dict(call) for call in calls])

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
