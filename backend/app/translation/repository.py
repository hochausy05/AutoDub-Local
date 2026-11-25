"""Atomic persistence for local execution requests and committed Unit batches."""
import json
import uuid
from contextlib import closing
from app.database import utc_now

class LocalTranslationRepository:
    def __init__(self, database):
        self.database = database

    def begin_request(self, run_id, attempt_id, start, count):
        with closing(self.database.connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                no = c.execute("SELECT COALESCE(MAX(request_no),0)+1 FROM local_translation_requests WHERE run_id=?", (run_id,)).fetchone()[0]
                request_id = str(uuid.uuid4())
                c.execute("INSERT INTO local_translation_requests(request_id,run_id,job_attempt_id,request_no,start_position,unit_count,status,started_at) VALUES(?,?,?,?,?,?,'RUNNING',?)",
                          (request_id, run_id, attempt_id, no, start, count, utc_now()))
                c.commit()
                return request_id
            except BaseException:
                c.rollback(); raise

    def finish_request(self, request_id, status, *, error_code=None, input_tokens=None, output_tokens=None, finish_reason=None, elapsed_ms=None):
        with closing(self.database.connect()) as c:
            c.execute("UPDATE local_translation_requests SET status=?,error_code=?,input_tokens=?,output_tokens=?,finish_reason=?,elapsed_ms=?,completed_at=? WHERE request_id=? AND status='RUNNING'",
                      (status,error_code,input_tokens,output_tokens,finish_reason,elapsed_ms,utc_now(),request_id))

    def commit_batch(self, job_id, attempt_id, run_id, request_id, start, outputs, input_tokens, output_tokens, finish_reason, elapsed_ms):
        with closing(self.database.connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                guard=c.execute("SELECT 1 FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=? AND j.status='RUNNING' AND a.id=? AND a.status='RUNNING'",(job_id,attempt_id)).fetchone()
                state=c.execute("SELECT next_position FROM local_translation_state WHERE run_id=?",(run_id,)).fetchone()
                rows=c.execute("SELECT unit_id FROM translation_units WHERE run_id=? AND position>=? AND position<? ORDER BY position",(run_id,start,start+len(outputs))).fetchall()
                if guard is None or state is None or state[0]!=start or len(rows)!=len(outputs):
                    raise RuntimeError("local_write_lease_lost")
                for row,text in zip(rows,outputs,strict=True):
                    changed=c.execute("UPDATE translation_units SET final_text=?,status='READY' WHERE run_id=? AND unit_id=? AND status='PENDING' AND final_text IS NULL",(text,run_id,row[0])).rowcount
                    if changed!=1: raise RuntimeError("local_unit_commit_conflict")
                c.execute("UPDATE local_translation_requests SET status='READY',input_tokens=?,output_tokens=?,finish_reason=?,elapsed_ms=?,completed_at=? WHERE request_id=? AND status='RUNNING'",(input_tokens,output_tokens,finish_reason,elapsed_ms,utc_now(),request_id))
                c.execute("UPDATE local_translation_state SET next_position=?,timeout_restart_used=0,updated_at=? WHERE run_id=? AND next_position=?",(start+len(outputs),utc_now(),run_id,start))
                c.commit()
            except BaseException:
                c.rollback(); raise
