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
from app.translation import TranslationArtifactValidator, TranslationError, TranslationService, normalize_translation_unit
from app.translation.artifacts import manifest_for
from app.translation.batch import next_batch
from app.translation.client import LocalClientError
from app.translation.repository import LocalTranslationRepository
from app.translation.validation import parse_labeled_response
from app.translation.worker import LocalTranslationService
from app.translation_revisions import TranslationRevisionError, TranslationRevisionService


class TranslationMigrationTests(unittest.TestCase):
    def test_v23_then_v24_preserves_schema_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            db=Database(Path(temporary)/"data"); db.initialize(target_version=22)
            project,_=db.create_project("migration fixture","translation-migration-key-0001")
            db.initialize(target_version=23); db.initialize(target_version=24)
            with closing(db.connect()) as c:
                self.assertEqual(c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],24)
                self.assertIsNotNone(c.execute("SELECT id FROM projects WHERE id=?",(project["id"],)).fetchone())
                self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0],"ok")
                self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])
                self.assertIn("local",c.execute("SELECT sql FROM sqlite_schema WHERE name='translation_runs'").fetchone()[0])
                chunk=c.execute("PRAGMA table_info(translation_units)").fetchall()
                self.assertEqual(next(row["notnull"] for row in chunk if row["name"]=="chunk_no"),0)


class FakeASR:
    def is_cached(self,_model): return True
    def run(self,_audio,_config,*,source_duration_us,source_offset_us,cancelled,progress):
        import faster_whisper, ctranslate2
        texts=["你好，世界。","I did not say that.","Xin chào bạn."]
        return {"segments":[{"start_us":i*400000+100000,"end_us":i*400000+350000,"source_text":text,
            "avg_logprob":-.1,"no_speech_prob":.01,"compression_ratio":1.0,"flags":[]} for i,text in enumerate(texts)],
            "language":"zh","language_probability":.99,"actual_device":"cpu","compute_type":"int8","fallback_reason":None,
            "model_id":"Systran/faster-whisper-base","model_revision":"fixture-revision",
            "faster_whisper_version":faster_whisper.__version__,"ctranslate2_version":ctranslate2.__version__,
            "model_load_seconds":.01,"inference_seconds":.02,"run_status":"READY"}


class TranslationTests(unittest.TestCase):
    """Shared synthetic approved-transcript fixture for Translation Editor and TTS tests."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db=Database(self.root/"data"); self.db.initialize()
        self.project,_=self.db.create_project("Translation integration","translation-project-idem-key-01")
        self.ffmpeg,self.ffprobe=shutil.which("ffmpeg"),FFprobe()
        if not self.ffmpeg or not self.ffprobe.executable: self.skipTest("ffmpeg/ffprobe unavailable")
        self.source=self.root/"视频-translation-日本語.mp4"
        subprocess.run([self.ffmpeg,"-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=blue:s=96x64:r=10",
            "-f","lavfi","-i","sine=frequency=440:sample_rate=48000","-t","2","-shortest","-c:v","mpeg4","-c:a","aac","-y",str(self.source)],check=True,timeout=30)
        self.artifacts=ArtifactPublicationService(self.db,{"source_video":SourceVideoValidator(self.ffprobe),"media_link_manifest":LinkManifestValidator(),"transcript_json":TranscriptArtifactValidator()})
        self.jobs=JobService(self.db,self.artifacts); StartupRecovery(self.db,self.jobs,self.artifacts).reconcile()
        self.media=MediaImportService(self.db,self.jobs,self.artifacts,self.ffprobe)
        token,session=uuid.uuid4().hex*2,"translation-fixture-session"
        with closing(self.db.connect()) as c:
            c.execute("INSERT INTO selections(id_hash,session_hash,project_id,purpose,private_path,display_name,file_identity_json,expires_at,state,created_at) VALUES(?,?,?,'source',?,?,?,strftime('%s','now')+600,'ISSUED','now')",
                (hashlib.sha256(token.encode()).hexdigest(),self.media.session_hash(session),self.project["id"],str(self.source),self.source.name,json.dumps(_path_identity(self.source))))
        import_job=self.media.create_import_job(session,self.project["id"],token,"COPY","translation-import-idem-001")
        self.media.import_once(); self.assertEqual(self.jobs.get_job(import_job["id"])["status"],"READY")
        asr=ASRService(self.db,self.jobs,self.artifacts,self.media,adapter=FakeASR())
        asr_job=asr.create_job(self.project["id"],ASRConfig(device="cpu"),"translation-asr-idem-0001")
        self.assertTrue(asr.run_once()); self.assertEqual(self.jobs.get_job(asr_job["id"])["status"],"READY")
        self.transcripts=TranscriptService(self.db); run_id=asr.list_runs(self.project["id"])[0]["id"]
        draft=self.transcripts.create_draft(self.project["id"],source_run_id=run_id,parent_revision_id=None,idempotency_key="translation-draft-idem-0001")
        self.accepted=self.transcripts.accept(self.project["id"],draft["id"],draft["lock_version"],None,"translation-accept-idem-001")
        self.service=TranslationService(self.db,self.jobs)
        self.artifacts.validators["translation_json"]=TranslationArtifactValidator(self.db)
        self.jobs.set_stage_input(self.project["id"],"translation",{"schema_version":1,"pending":"local-fixture"})

    def tearDown(self): self.temp.cleanup()

    def _ready_single_pass_translation(self):
        snapshot={"schema_version":1,"fixture":"local","revision_id":self.accepted["id"],"model_sha256":"a"*64,
            "runtime_identity":"llama.cpp/test","context_size":4096,"gpu_layers":99,"parallel_slots":1,
            "prompt_sha256":"b"*64,"batch_policy":{"default":80,"fallback":60},"timeout_policy":{"restart_once":True}}
        self.jobs.set_stage_input(self.project["id"],"translation",snapshot)
        job=self.jobs.create_job(self.project["id"],"local_translation",snapshot,f"local-translation-fixture-{uuid.uuid4().hex[:22]}",activation_stage="translation")
        job=self.jobs.claim_next_job("local_translation"); attempt=job["attempt"]
        input_data,units=self.service._build_input(self.project["id"],self.accepted["id"])
        run_id=str(uuid.uuid4()); stamp="2026-01-01T00:00:00Z"
        with closing(self.db.connect()) as c:
            c.execute("INSERT INTO translation_runs(id,project_id,producer_job_id,transcript_revision_id,source_asset_id,input_fingerprint,provider,model_id,provider_sdk_version,prompt_version,status,created_at,api_type,api_version,wire_protocol_version,pass_strategy,normalization_policy_version,adapter_version,provider_strategy_version) VALUES(?,?,?,?,?,?,'local','fixture-model',NULL,'local-vi-tts-v1','RUNNING',?,'chat_completions','v1','local-labeled-lines-v1','SINGLE_PASS','translation-normalize-v1','llama.cpp/test','local-batch-80-60-v1')",
                (run_id,self.project["id"],job["id"],self.accepted["id"],input_data["source_asset_id"],job["input_fingerprint"],stamp))
            c.execute("INSERT INTO local_translation_state(run_id,next_position,batch_limit,timeout_restart_used,updated_at) VALUES(?,0,80,0,?)",(run_id,stamp))
            for unit in units:
                c.execute("INSERT INTO translation_units(run_id,project_id,unit_id,logical_unit_key,position,chunk_no,revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us,final_text,status) VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,'READY')",
                    (run_id,self.project["id"],unit["unit_id"],unit["logical_unit_key"],unit["position"],json.dumps(unit["revision_segment_ids"]),json.dumps(unit["source_segment_ids"]),unit["source_text"],unit["start_us"],unit["end_us"],f"Bản dịch {unit['position']+1}"))
            c.execute("UPDATE local_translation_state SET next_position=? WHERE run_id=?",(len(units),run_id))
            rows=c.execute("SELECT * FROM translation_units WHERE run_id=? ORDER BY position",(run_id,)).fetchall()
            run=dict(c.execute("SELECT * FROM translation_runs WHERE id=?",(run_id,)).fetchone())
        prepared=[]
        for r in rows:
            item=dict(r); item["revision_segment_ids"]=json.loads(item.pop("revision_segment_ids_json")); item["source_segment_ids"]=json.loads(item.pop("source_segment_ids_json")); prepared.append(item)
        payload=json.dumps(manifest_for(run,prepared,snapshot),ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        artifact=self.artifacts.publish(project_id=self.project["id"],job_id=job["id"],attempt_id=attempt["id"],kind="translation_json",input_fingerprint=job["input_fingerprint"],fingerprint_version=1,source=payload)
        evidence=self.artifacts.completion_evidence(job["id"],attempt["id"],[artifact["id"]])
        def complete(c): c.execute("UPDATE translation_runs SET artifact_id=?,status='READY',completed_at=? WHERE id=?",(artifact["id"],stamp,run_id))
        self.jobs.complete_attempt(job["id"],attempt["id"],evidence,completion_hook=complete)
        return run_id, None

    def test_editor_save_approval_and_unit_identity(self):
        run_id,_=self._ready_single_pass_translation(); revisions=TranslationRevisionService(self.db)
        editor=revisions.editor_state(self.project["id"],run_id); original=editor["original_units"]
        draft=revisions.create_draft(self.project["id"],run_id)
        saved=revisions.save_draft(self.project["id"],run_id,draft["id"],draft["lock_version"],[{"unit_id":original[0]["unit_id"],"text":"Bản dịch đã sửa."}],"local-fixture-save-key-0001")
        self.assertEqual(saved["units"][0]["final_text"],"Bản dịch đã sửa")
        accepted=revisions.accept_draft(self.project["id"],run_id,draft["id"],saved["lock_version"],None,"local-fixture-accept-key-0001")
        self.assertEqual(accepted["units"][0]["logical_unit_key"],original[0]["logical_unit_key"])
        with closing(self.db.connect()) as c:
            with self.assertRaises(Exception): c.execute("UPDATE translation_units SET source_text='tampered' WHERE run_id=?",(run_id,))

    def test_bulk_save_cas_rolls_back_invalid_and_stale_edits(self):
        run_id,_=self._ready_single_pass_translation(); revisions=TranslationRevisionService(self.db); draft=revisions.create_draft(self.project["id"],run_id)
        with self.assertRaises(TranslationRevisionError): revisions.save_draft(self.project["id"],run_id,draft["id"],draft["lock_version"],[{"unit_id":draft["units"][0]["unit_id"],"text":""}],"local-fixture-invalid-key-01")
        saved=revisions.save_draft(self.project["id"],run_id,draft["id"],draft["lock_version"],[{"unit_id":draft["units"][0]["unit_id"],"text":"Đã sửa"}],"local-fixture-cas-save-key-01")
        with self.assertRaises(TranslationRevisionError): revisions.save_draft(self.project["id"],run_id,draft["id"],draft["lock_version"],[{"unit_id":draft["units"][0]["unit_id"],"text":"Stale"}],"local-fixture-cas-stale-key-1")
        self.assertEqual(saved["lock_version"],2)

    def test_local_job_creation_is_atomic_and_idempotent_without_transcript_in_snapshot(self):
        runtime=self.root/"llama-server.exe"; model=self.root/"model.gguf"
        runtime.write_bytes(b"fixture runtime"); model.write_bytes(b"fixture model")
        with patch("app.translation.runtime.RUNTIME",runtime), patch("app.translation.runtime.MODEL",model):
            created=self.service.create_local_job(self.project["id"],self.accepted["id"],"local-atomic-create-key-001")
            with closing(self.db.connect()) as c:
                before=c.execute("SELECT input_generation,pointer_generation FROM stage_pointers WHERE project_id=? AND stage='translation'",(self.project["id"],)).fetchone()
                snapshot=c.execute("SELECT input_snapshot FROM jobs WHERE id=?",(created["job"]["id"],)).fetchone()[0]
                self.assertEqual(c.execute("SELECT COUNT(*) FROM translation_units WHERE run_id=?",(created["run_id"],)).fetchone()[0],3)
                self.assertEqual(c.execute("SELECT next_position,batch_limit FROM local_translation_state WHERE run_id=?",(created["run_id"],)).fetchone()[:],(0,80))
            replay=self.service.create_local_job(self.project["id"],self.accepted["id"],"local-atomic-create-key-001")
            with closing(self.db.connect()) as c:
                after=c.execute("SELECT input_generation,pointer_generation FROM stage_pointers WHERE project_id=? AND stage='translation'",(self.project["id"],)).fetchone()
                self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND kind='local_translation'",(self.project["id"],)).fetchone()[0],1)
            self.assertEqual(created["job"]["id"],replay["job"]["id"])
            self.assertEqual(tuple(before),tuple(after))
            self.assertNotIn("你好，世界。",snapshot)

    def test_failed_atomic_creation_rolls_back_stage_input(self):
        before=self.jobs.set_stage_input(self.project["id"],"translation",{"schema_version":1,"stable":"before"})
        with self.assertRaisesRegex(RuntimeError,"fixture_failure"):
            self.jobs.create_job(self.project["id"],"local_translation",{"schema_version":1,"new":"input"},
                "local-atomic-rollback-key-001",activation_stage="translation",update_stage_input=True,
                creation_hook=lambda _connection,_job_id: (_ for _ in ()).throw(RuntimeError("fixture_failure")))
        with closing(self.db.connect()) as c:
            after=c.execute("SELECT input_fingerprint,input_generation FROM stage_pointers WHERE project_id=? AND stage='translation'",(self.project["id"],)).fetchone()
            count=c.execute("SELECT COUNT(*) FROM jobs WHERE kind='local_translation' AND project_id=?",(self.project["id"],)).fetchone()[0]
        self.assertEqual(after["input_fingerprint"],before["input_fingerprint"])
        self.assertEqual(after["input_generation"],before["input_generation"])
        self.assertEqual(count,0)

    def _create_local_job(self):
        runtime=self.root/"llama-server.exe"; model=self.root/"model.gguf"
        runtime.write_bytes(b"fixture runtime"); model.write_bytes(b"fixture model")
        with patch("app.translation.runtime.RUNTIME",runtime), patch("app.translation.runtime.MODEL",model):
            return self.service.create_local_job(self.project["id"],self.accepted["id"],f"local-worker-job-{uuid.uuid4().hex[:20]}")

    def test_fake_local_worker_publishes_verified_run_without_inference_service(self):
        created=self._create_local_job(); instances=[]
        class Runtime:
            api_key="synthetic-internal-key"
            def start(self,**_policy): return "http://127.0.0.1:8080"
            def close(self): pass
        class Client:
            def __init__(self,_base,_key): instances.append(self)
            def input_tokens(self,_prompt,_cancelled=None): return 40
            def translate(self,_prompt,_budget,_cancelled=None): return "[1] Xin chào\n[2] Tôi không nói vậy\n[3] Chào bạn",12,"stop"
            def close(self): pass
        worker=LocalTranslationService(self.db,self.jobs,self.artifacts,runtime_factory=Runtime,client_factory=Client)
        self.assertTrue(worker.run_once())
        job=self.jobs.get_job(created["job"]["id"])
        run=self.service.get_run(self.project["id"],created["run_id"])
        self.assertEqual(job["status"],"READY"); self.assertEqual(run["status"],"READY")
        self.assertEqual([u["final_text"] for u in run["units"]],["Xin chào","Tôi không nói vậy","Chào bạn"])
        with closing(self.db.connect()) as c:
            state=c.execute("SELECT next_position,timeout_restart_used FROM local_translation_state WHERE run_id=?",(created["run_id"],)).fetchone()
            requests=c.execute("SELECT status FROM local_translation_requests WHERE run_id=?",(created["run_id"],)).fetchall()
        self.assertEqual(tuple(state),(3,0)); self.assertEqual([r["status"] for r in requests],["READY"])
        self.assertEqual(len(instances),1)

    def test_timeout_restarts_runtime_once_and_resumes_same_cursor(self):
        created=self._create_local_job(); runtimes=[]; clients=[]
        class Runtime:
            api_key="synthetic-internal-key"
            def __init__(self): runtimes.append(self)
            def start(self,**_policy): return "http://127.0.0.1:8080"
            def close(self): pass
        class Client:
            def __init__(self,_base,_key): clients.append(self)
            def input_tokens(self,_prompt,_cancelled=None): return 40
            def translate(self,_prompt,_budget,_cancelled=None):
                if len(clients)==1: raise LocalClientError("local_inference_timeout")
                return "[1] Xin chào\n[2] Tôi không nói vậy\n[3] Chào bạn",12,"stop"
            def close(self): pass
        worker=LocalTranslationService(self.db,self.jobs,self.artifacts,runtime_factory=Runtime,client_factory=Client)
        worker.run_once()
        with closing(self.db.connect()) as c:
            state=c.execute("SELECT next_position,timeout_restart_used FROM local_translation_state WHERE run_id=?",(created["run_id"],)).fetchone()
            requests=c.execute("SELECT status,start_position,unit_count FROM local_translation_requests WHERE run_id=? ORDER BY request_no",(created["run_id"],)).fetchall()
        self.assertEqual(self.jobs.get_job(created["job"]["id"])["status"],"READY")
        self.assertEqual(tuple(state),(3,0)); self.assertEqual([r["status"] for r in requests],["TIMEOUT","READY"])
        self.assertEqual((requests[0]["start_position"],requests[0]["unit_count"]),(requests[1]["start_position"],requests[1]["unit_count"]))
        self.assertEqual((len(runtimes),len(clients)),(2,2))

    def test_cancellation_during_response_prevents_batch_commit(self):
        created=self._create_local_job()
        class Runtime:
            api_key="synthetic-internal-key"
            def start(self,**_policy): return "http://127.0.0.1:8080"
            def close(self): pass
        class Client:
            def __init__(self,_base,_key): pass
            def input_tokens(self,_prompt,_cancelled=None): return 40
            def translate(self,_prompt,_budget,_cancelled=None):
                worker.jobs.request_cancel(created["job"]["id"],"local-worker-cancel-key-001")
                return "[1] Xin chào\n[2] Tôi không nói vậy\n[3] Chào bạn",12,"stop"
            def close(self): pass
        worker=LocalTranslationService(self.db,self.jobs,self.artifacts,runtime_factory=Runtime,client_factory=Client)
        worker.run_once()
        with closing(self.db.connect()) as c:
            state=c.execute("SELECT next_position FROM local_translation_state WHERE run_id=?",(created["run_id"],)).fetchone()[0]
            statuses=[r[0] for r in c.execute("SELECT status FROM translation_units WHERE run_id=? ORDER BY position",(created["run_id"],))]
            request=c.execute("SELECT status FROM local_translation_requests WHERE run_id=?",(created["run_id"],)).fetchone()[0]
        self.assertEqual(self.jobs.get_job(created["job"]["id"])["status"],"CANCELLED")
        self.assertEqual((state,statuses,request),(0,["PENDING"]*3,"CANCELLED"))

    def test_startup_interruption_keeps_cursor_and_request_history(self):
        created=self._create_local_job(); claimed=self.jobs.claim_next_job("local_translation")
        LocalTranslationRepository(self.db).begin_request(created["run_id"],claimed["attempt"]["id"],0,3)
        StartupRecovery(self.db,self.jobs,self.artifacts).reconcile()
        with closing(self.db.connect()) as c:
            request=c.execute("SELECT status,error_code FROM local_translation_requests WHERE run_id=?",(created["run_id"],)).fetchone()
            state=c.execute("SELECT next_position FROM local_translation_state WHERE run_id=?",(created["run_id"],)).fetchone()[0]
            run=c.execute("SELECT status,error_code FROM translation_runs WHERE id=?",(created["run_id"],)).fetchone()
        job=self.jobs.get_job(created["job"]["id"])
        self.assertEqual(job["status"],"INTERRUPTED"); self.assertTrue(job["retry_available"])
        self.assertEqual(tuple(request),("INTERRUPTED","process_interrupted")); self.assertEqual(state,0)
        self.assertEqual(tuple(run),("FAILED","process_interrupted"))

    def test_translation_revision_api_keeps_csrf_and_project_scope(self):
        run_id,_=self._ready_single_pass_translation(); app=create_app("local-translation-api-secret",data_root=self.db.data_root)
        with TestClient(app,client=("127.0.0.1",54322)) as client:
            headers={"host":"127.0.0.1:8000","origin":"http://127.0.0.1:5173","sec-fetch-site":"same-origin"}
            base=f"/api/v1/projects/{self.project['id']}/translations/runs/{run_id}"
            self.assertEqual(client.get(base+"/editor",headers=headers).status_code,401)
            boot=client.post("/api/v1/session/bootstrap",headers={**headers,"x-local-bootstrap":"local-translation-api-secret"},json={})
            self.assertEqual(boot.status_code,200)
            self.assertEqual(client.get(base+"/editor",headers={**headers,"x-csrf-token":boot.json()["csrf_token"]}).status_code,200)

    def test_local_translation_api_requires_session_csrf_and_replays_idempotently(self):
        runtime=self.root/"llama-server.exe"; model=self.root/"model.gguf"
        runtime.write_bytes(b"fixture runtime"); model.write_bytes(b"fixture model")
        app=create_app("local-translation-create-api-secret",data_root=self.db.data_root)
        with patch("app.translation.runtime.RUNTIME",runtime), patch("app.translation.runtime.MODEL",model), \
             patch("app.main.LocalTranslationWorker.start",lambda _worker:None), \
             TestClient(app,client=("127.0.0.1",54322)) as client:
            headers={"host":"127.0.0.1:8000","origin":"http://127.0.0.1:5173","sec-fetch-site":"same-origin"}
            self.assertEqual(client.get("/api/v1/translations/status",headers=headers).status_code,401)
            boot=client.post("/api/v1/session/bootstrap",headers={**headers,"x-local-bootstrap":"local-translation-create-api-secret"},json={})
            csrf=boot.json()["csrf_token"]
            self.assertEqual(client.get("/api/v1/translations/status",headers={**headers,"x-csrf-token":csrf}).json()["configured"],True)
            path=f"/api/v1/projects/{self.project['id']}/translations/jobs"
            denied=client.post(path,headers=headers,json={"accepted_transcript_revision_id":self.accepted["id"],"target_language":"vi"})
            self.assertEqual(denied.status_code,403)
            write={**headers,"x-csrf-token":csrf,"idempotency-key":"local-translation-api-create-key-01"}
            body={"accepted_transcript_revision_id":self.accepted["id"],"target_language":"vi"}
            first=client.post(path,headers=write,json=body); replay=client.post(path,headers=write,json=body)
            self.assertEqual(first.status_code,202,first.text); self.assertEqual(replay.status_code,202,replay.text)
            self.assertEqual(first.json()["job"]["id"],replay.json()["job"]["id"])


class LocalTranslationContracts(unittest.TestCase):
    def test_labeled_lines_reject_missing_extra_wrong_order_han_and_empty(self):
        self.assertEqual(parse_labeled_response("[1] Xin chào\n[2] Tạm biệt",2),["Xin chào","Tạm biệt"])
        for value,count in (("[1] Xin chào",2),("[2] Xin chào\n[1] Tạm biệt",2),("[1] hello\n[2] 你好",2),("[1] ",1)):
            with self.subTest(value=value), self.assertRaises(TranslationError): parse_labeled_response(value,count)

    def test_batch_cursor_is_contiguous_and_limited_to_80_or_60(self):
        values=[{"position":i} for i in range(300)]
        self.assertEqual([x["position"] for x in next_batch(values,80,80)],[80+i for i in range(80)])
        self.assertEqual([x["position"] for x in next_batch(values,80,60)],[80+i for i in range(60)])
        with self.assertRaises(ValueError): next_batch(values,0,40)

    def test_normalization_preserves_numbers_and_closers(self):
        self.assertEqual(normalize_translation_unit("Giá 3.14, đúng không?!」"),"Giá 3.14, đúng không」")
