"""Single-job Local Translation worker with durable cursor and batch commits."""
import json
import threading
import time
import uuid
from contextlib import closing

from app.artifacts import ArtifactError
from app.database import utc_now
from app.jobs import JobConflict
from .artifacts import manifest_for
from .batch import next_batch
from .client import LocalClient, LocalClientError
from .prompt import build_prompt
from .repository import LocalTranslationRepository
from .runtime import RuntimeManager, RuntimeErrorCode
from .validation import parse_labeled_response

class LocalTranslationService:
    def __init__(self, database, jobs, artifacts, runtime_factory=RuntimeManager, client_factory=LocalClient):
        self.database, self.jobs, self.artifacts = database, jobs, artifacts
        self.runtime_factory, self.client_factory = runtime_factory, client_factory
        self.repository = LocalTranslationRepository(database)
        self.active_lock = threading.Lock()
        self.active_job_id = None
        self.runtime = None
        self.client = None
        self.cleanup_blocked = False

    def _cancelled(self, job_id):
        with closing(self.database.connect()) as c:
            row=c.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
        return row is None or row[0] != "RUNNING"

    def _update_progress(self, job, attempt, completed, total):
        self.jobs.update_attempt_progress(job["id"],attempt["id"],{"phase":"translating","completed_units":completed,"total_units":total,
            "fraction":completed/total if total else 1.0})

    def _finish_run(self, run_id, status, code=None):
        with closing(self.database.connect()) as c:
            c.execute("UPDATE translation_runs SET status=?,error_code=?,completed_at=? WHERE id=? AND status NOT IN ('READY','STALE')",(status,code,utc_now(),run_id))

    def _publish(self, job, attempt, run):
        with closing(self.database.connect()) as c:
            rows=c.execute("SELECT * FROM translation_units WHERE run_id=? ORDER BY position",(run["id"],)).fetchall()
            state=c.execute("SELECT next_position FROM local_translation_state WHERE run_id=?",(run["id"],)).fetchone()
            snapshot=json.loads(c.execute("SELECT input_snapshot FROM jobs WHERE id=?",(job["id"],)).fetchone()[0])
        if state is None or state[0] != len(rows) or any(r["status"]!="READY" or not r["final_text"] for r in rows):
            raise RuntimeError("local_translation_coverage_invalid")
        units=[]
        for r in rows:
            u=dict(r); u["revision_segment_ids"]=json.loads(u.pop("revision_segment_ids_json")); u["source_segment_ids"]=json.loads(u.pop("source_segment_ids_json")); units.append(u)
        artifact_value=manifest_for(run,units,snapshot)
        raw=json.dumps(artifact_value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
        artifact=self.artifacts.publish(project_id=job["project_id"],job_id=job["id"],attempt_id=attempt["id"],kind="translation_json",
            input_fingerprint=job["input_fingerprint"],fingerprint_version=1,source=raw)
        evidence=self.artifacts.completion_evidence(job["id"],attempt["id"],[artifact["id"]])
        def finish(connection):
            connection.execute("UPDATE translation_runs SET artifact_id=?,status='READY',completed_at=?,error_code=NULL WHERE id=? AND status='RUNNING'",(artifact["id"],utc_now(),run["id"]))
        # Completion hook runs in the same transaction as stage CAS and Job success.
        result=self.jobs.complete_attempt(job["id"],attempt["id"],evidence,completion_hook=finish)
        if result["status"]=="CANCELLED": self._finish_run(run["id"],"CANCELLED","job_cancelled")
        elif result.get("attempt",{}).get("activation_outcome")=="STALE": self._finish_run(run["id"],"STALE","translation_input_stale")
        return result["status"]

    def run_once(self):
        if self.cleanup_blocked:
            return False
        job=self.jobs.claim_next_job("local_translation")
        if job is None: return False
        with closing(self.database.connect()) as c:
            row=c.execute("SELECT * FROM translation_runs WHERE producer_job_id=?",(job["id"],)).fetchone()
            snapshot=json.loads(c.execute("SELECT input_snapshot FROM jobs WHERE id=?",(job["id"],)).fetchone()[0])
        if row is None:
            self._fail(job,"local_translation_run_missing"); return True
        run=dict(row)
        attempt=job["attempt"]
        with self.active_lock: self.active_job_id=job["id"]
        try:
            with closing(self.database.connect()) as c:
                c.execute("UPDATE translation_runs SET status='RUNNING' WHERE id=? AND provider='local' AND status IN ('QUEUED','FAILED')",(run["id"],))
            self.runtime=self.runtime_factory()
            base=self.runtime.start(expected_model_sha256=snapshot["model_sha256"],expected_runtime_identity=snapshot["runtime_identity"])
            self.client=self.client_factory(base,self.runtime.api_key)
            with closing(self.database.connect()) as c:
                units=[dict(r) for r in c.execute("SELECT * FROM translation_units WHERE run_id=? ORDER BY position",(run["id"],)).fetchall()]
                state=c.execute("SELECT next_position FROM local_translation_state WHERE run_id=?",(run["id"],)).fetchone()
            cursor=state["next_position"] if state else -1
            if (cursor < 0 or sum(u["status"]=="READY" for u in units)!=cursor
                    or any((u["status"]=="READY") != (u["position"]<cursor) for u in units)):
                raise RuntimeError("local_recovery_state_invalid")
            while True:
                if self._cancelled(job["id"]):
                    self._finish_run(run["id"],"CANCELLED","job_cancelled"); return self._cancel(job,attempt)
                with closing(self.database.connect()) as c:
                    state=dict(c.execute("SELECT * FROM local_translation_state WHERE run_id=?",(run["id"],)).fetchone())
                cursor,limit=state["next_position"],state["batch_limit"]
                if cursor>=len(units): break
                batch=next_batch(units,cursor,limit); prompt=build_prompt(batch); request=self.repository.begin_request(run["id"],attempt["id"],cursor,len(batch)); start=time.monotonic()
                try:
                    input_tokens=self.client.input_tokens(prompt,lambda:self._cancelled(job["id"]))
                    budget=4096-input_tokens-32
                    if budget<=len(batch)*2 or input_tokens+budget>4096:
                        raise LocalClientError("local_context_limit")
                    response,output_tokens,finish=self.client.translate(prompt,budget,lambda:self._cancelled(job["id"]))
                    elapsed=round((time.monotonic()-start)*1000)
                    if finish=="length": raise LocalClientError("local_response_truncated")
                    if finish not in ("stop",): raise LocalClientError("local_response_invalid")
                    outputs=parse_labeled_response(response,len(batch))
                    if self._cancelled(job["id"]):
                        self.repository.finish_request(request,"CANCELLED",error_code="job_cancelled",elapsed_ms=elapsed)
                        self._finish_run(run["id"],"CANCELLED","job_cancelled"); return self._cancel(job,attempt)
                    self.repository.commit_batch(job["id"],attempt["id"],run["id"],request,cursor,outputs,input_tokens,output_tokens,finish,elapsed)
                    self._update_progress(job,attempt,cursor+len(batch),len(units))
                except LocalClientError as exc:
                    code=exc.code
                    if code=="local_job_cancelled":
                        self.repository.finish_request(request,"CANCELLED",error_code="job_cancelled",elapsed_ms=round((time.monotonic()-start)*1000))
                        self._finish_run(run["id"],"CANCELLED","job_cancelled")
                        return self._cancel(job,attempt)
                    if code=="local_context_limit" and limit==80 and len(batch)>60:
                        self.repository.finish_request(request,"CONTEXT_LIMIT",error_code=code,elapsed_ms=round((time.monotonic()-start)*1000))
                        with closing(self.database.connect()) as c: c.execute("UPDATE local_translation_state SET batch_limit=60,updated_at=? WHERE run_id=?",(utc_now(),run["id"]))
                        continue
                    if code=="local_inference_timeout" and not state["timeout_restart_used"]:
                        self.repository.finish_request(request,"TIMEOUT",error_code=code,elapsed_ms=round((time.monotonic()-start)*1000))
                        with closing(self.database.connect()) as c: c.execute("UPDATE local_translation_state SET timeout_restart_used=1,updated_at=? WHERE run_id=? AND next_position=?",(utc_now(),run["id"],cursor))
                        self.client.close(); self.client=None
                        self.runtime.close(); self.runtime=None
                        self.runtime=self.runtime_factory(); base=self.runtime.start(expected_model_sha256=snapshot["model_sha256"],expected_runtime_identity=snapshot["runtime_identity"])
                        self.client=self.client_factory(base,self.runtime.api_key)
                        continue
                    self.repository.finish_request(request,"TIMEOUT" if "timeout" in code else "INVALID" if "invalid" in code or "truncated" in code else "CONTEXT_LIMIT" if code=="local_context_limit" else "FAILED",error_code=code,elapsed_ms=round((time.monotonic()-start)*1000))
                    raise
                except Exception:
                    self.repository.finish_request(request,"INVALID",error_code="local_response_invalid",elapsed_ms=round((time.monotonic()-start)*1000)); raise
            self._publish(job,attempt,run)
            return True
        except (RuntimeErrorCode,LocalClientError,ArtifactError,JobConflict,Exception) as exc:
            code=getattr(exc,"code",None) or (str(exc) if str(exc).startswith("local_") else "local_translation_failed")
            try:
                current=self.jobs.get_job(job["id"])
                if current["status"] in {"CANCEL_REQUESTED","CANCELLED"}:
                    self._finish_run(run["id"],"CANCELLED","job_cancelled")
                    self._cancel(job,attempt)
                    return True
            except Exception:
                pass
            self._finish_run(run["id"],"FAILED",code)
            self._fail(job,code,attempt)
            return True
        finally:
            if self.client:
                self.client.close(); self.client=None
            if self.runtime:
                try:
                    self.runtime.close()
                except RuntimeErrorCode:
                    self.cleanup_blocked=True
                finally:
                    self.runtime=None
            with self.active_lock: self.active_job_id=None

    def _fail(self, job, code, attempt=None):
        try:
            attempt=attempt or job.get("attempt")
            if attempt and attempt.get("status")=="RUNNING": self.jobs.fail_attempt(job["id"],attempt["id"],code,retryable=code in {"local_runtime_start_timeout","local_inference_timeout","local_runtime_request_failed"})
        except Exception: pass

    def _cancel(self, job, attempt):
        try: return self.jobs.fail_attempt(job["id"],attempt["id"],"job_cancelled",retryable=False)
        except Exception: return None

class LocalTranslationWorker:
    def __init__(self, service):
        self.service=service; self.stop_event=threading.Event(); self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self._run,name="autodub-local-translation",daemon=True); self.thread.start()
    def stop(self):
        self.stop_event.set()
        with self.service.active_lock: active=self.service.active_job_id
        if active:
            try: self.service.jobs.request_cancel(active,f"shutdown-{uuid.uuid4().hex}")
            except Exception: pass
        if self.thread: self.thread.join(timeout=12)
    def _run(self):
        while not self.stop_event.is_set():
            try: worked=self.service.run_once()
            except Exception: worked=False
            if not worked: self.stop_event.wait(.25)
