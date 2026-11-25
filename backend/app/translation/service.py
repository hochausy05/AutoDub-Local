"""Translation domain contracts and read-only access to historical Runs."""
from __future__ import annotations
import json
import uuid
from contextlib import closing, nullcontext
import hashlib
import os
import time
from pathlib import Path
from typing import Any
from app.database import Database, utc_now
from app.jobs import ActivationStage

class TranslationError(Exception):
    def __init__(self, code: str, *, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)

def normalize_translation_unit(text: str) -> str:
    if not isinstance(text, str) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise TranslationError("translation_invalid_unicode")
    value = text.strip()
    closing = "".join(map(chr, [34, 39, 8221, 8217, 187, 41, 93, 125, 12301, 12303, 12305, 65289]))
    punctuation = "".join(map(chr, [46, 12290, 65294, 65377, 33, 65281, 63, 65311, 44, 65292, 12289, 58, 65306, 59, 65307, 8230]))
    while value:
        suffix = ""
        while value and value[-1] in closing:
            suffix = value[-1] + suffix
            value = value[:-1]
        if value and value[-1] in punctuation:
            value = value[:-1].rstrip() + suffix
        else:
            value = value.rstrip() + suffix
            break
    value = value.rstrip()
    if not value:
        raise TranslationError("translation_empty_after_normalization")
    return value

class TranslationArtifactValidator:
    version = "translation-json-v1"

    def __init__(self, database: Database | None = None):
        self.database = database

    def validate(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationError("translation_artifact_invalid") from exc
        if (not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("run_id"), str)
                or not isinstance(value.get("input_fingerprint"), str) or len(value["input_fingerprint"]) != 64
                or not isinstance(value.get("units"), list)):
            raise TranslationError("translation_artifact_invalid")
        strategy = value.get("pass_strategy", "TWO_PASS")
        if strategy not in {"TWO_PASS", "SINGLE_PASS"}:
            raise TranslationError("translation_artifact_invalid")
        ids = [unit.get("unit_id") for unit in value["units"] if isinstance(unit, dict)]
        def unit_invalid(unit: dict[str, Any]) -> bool:
            result_missing = (not isinstance(unit.get("final_text"), str) or not unit["final_text"].strip()) if strategy == "SINGLE_PASS" else (
                not isinstance(unit.get("pass1_text"), str) or not unit["pass1_text"].strip()
                or not isinstance(unit.get("pass2_text"), str) or not unit["pass2_text"].strip())
            return result_missing or not isinstance(unit.get("revision_segment_ids"), list) or not unit["revision_segment_ids"] or not isinstance(unit.get("source_segment_ids"), list) or not unit["source_segment_ids"]
        if (len(ids) != len(value["units"]) or len(ids) != len(set(ids)) or any(unit_invalid(unit) for unit in value["units"])):
            raise TranslationError("translation_artifact_invalid")
        if self.database is not None:
            with closing(self.database.connect()) as connection:
                run = connection.execute("SELECT input_fingerprint,pass_strategy,provider,model_id,prompt_version,source_asset_id,transcript_revision_id,producer_job_id,normalization_policy_version FROM translation_runs WHERE id=?", (value["run_id"],)).fetchone()
                job = connection.execute("SELECT input_snapshot FROM jobs WHERE id=(SELECT producer_job_id FROM translation_runs WHERE id=?)", (value["run_id"],)).fetchone()
                expected = connection.execute("SELECT unit_id,position,revision_segment_ids_json,source_segment_ids_json,source_text,pass1_text,pass2_text,final_text,final_notes_json FROM translation_units WHERE run_id=? ORDER BY position", (value["run_id"],)).fetchall()
            if run is None or run["input_fingerprint"] != value["input_fingerprint"] or run["pass_strategy"] != strategy or len(expected) != len(value["units"]):
                raise TranslationError("translation_artifact_input_mismatch")
            if run["provider"] == "local":
                try: snapshot=json.loads(job["input_snapshot"]) if job else None
                except (TypeError, json.JSONDecodeError): snapshot=None
                fields=("model_sha256","runtime_identity","context_size","gpu_layers","parallel_slots","prompt_sha256","batch_policy","timeout_policy")
                if (snapshot is None or value.get("provider")!="local" or value.get("model_id")!=run["model_id"]
                    or value.get("prompt_version")!=run["prompt_version"] or value.get("source_asset_id")!=run["source_asset_id"]
                    or value.get("transcript_revision_id")!=run["transcript_revision_id"]
                    or value.get("normalization_version")!=run["normalization_policy_version"]
                    or any(value.get(field)!=snapshot.get(field) for field in fields)):
                    raise TranslationError("translation_artifact_input_mismatch")
            for stored, published in zip(expected, value["units"], strict=True):
                if (stored["unit_id"] != published.get("unit_id") or stored["position"] != published.get("position")
                        or json.loads(stored["revision_segment_ids_json"]) != published.get("revision_segment_ids")
                        or json.loads(stored["source_segment_ids_json"]) != published.get("source_segment_ids")
                        or stored["source_text"] != published.get("source_text")
                        or stored["pass1_text"] != published.get("pass1_text") or stored["pass2_text"] != published.get("pass2_text")
                        or stored["final_text"] != published.get("final_text")
                        or json.loads(stored["final_notes_json"]) != published.get("final_notes", [])):
                    raise TranslationError("translation_artifact_mapping_mismatch")
        return {"run_id": value["run_id"], "unit_count": len(ids), "schema_version": 1}



class TranslationService:
    def __init__(self, database: Database, jobs: Any):
        self.database, self.jobs = database, jobs

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try: parsed=uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc: raise TranslationError(f"invalid_{field}") from exc
        if str(parsed) != value.lower(): raise TranslationError(f"invalid_{field}")
        return str(parsed)

    def _build_input(self, project_id: str, revision_id: str, connection=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from app.translation.units import CanonicalUnitBuilder
        return CanonicalUnitBuilder(self.database).build(project_id, revision_id, connection)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def local_status(self) -> dict[str, Any]:
        from app.translation.runtime import PORT_VALID, RUNTIME, MODEL
        if not PORT_VALID:
            return {"configured": False, "model_id": "Hy-MT3-7B-Q4_K_M", "error_code": "local_runtime_config_invalid"}
        configured = RUNTIME.is_absolute() and RUNTIME.is_file() and MODEL.is_absolute() and MODEL.is_file()
        return {"configured": configured, "model_id": "Hy-MT3-7B-Q4_K_M", "error_code": None if configured else "local_model_or_runtime_missing"}

    def create_local_job(self, project_id: str, revision_id: str, idempotency_key: str) -> dict[str, Any]:
        from app.translation.prompt import PROMPT_FINGERPRINT, PROMPT_VERSION
        from app.translation.runtime import MODEL, RUNTIME, PORT
        if not self.local_status()["configured"]:
            raise TranslationError("local_model_or_runtime_missing", status=503)
        model_fingerprint = self._hash_file(MODEL)
        # Hash prompt and runtime policy, but never include transcript contents or units in the Job snapshot.
        with closing(self.database.connect()) as c:
            input_data, units = self._build_input(project_id, revision_id, c)
            unit_digest = hashlib.sha256(self._json(units).encode("utf-8")).hexdigest()
            snapshot = {**input_data, "canonical_unit_count": len(units), "canonical_units_sha256": unit_digest,
                "model_id": "Hy-MT3-7B-Q4_K_M", "model_sha256": model_fingerprint,
                "runtime_identity": f"llama.cpp/b12676:{self._hash_file(RUNTIME)}", "runtime_port": PORT,
                "prompt_version": PROMPT_VERSION, "prompt_sha256": PROMPT_FINGERPRINT,
                "context_size": 4096, "gpu_layers": 99, "parallel_slots": 1,
                "batch_policy": {"default": 80, "fallback": 60},
                "timeout_policy": {"startup_seconds":120,"inference_seconds":300,"restart_once":True},
                "normalization_version": "translation-normalize-v1"}
        run_id = str(uuid.uuid4())
        def create_rows(connection, job_id):
            # Rebuild under this write lock to bind accepted transcript and source generations atomically.
            current, current_units = self._build_input(project_id, revision_id, connection)
            if current != input_data or hashlib.sha256(self._json(current_units).encode("utf-8")).hexdigest() != unit_digest:
                raise TranslationError("accepted_transcript_stale")
            job = connection.execute("SELECT input_fingerprint FROM jobs WHERE id=?", (job_id,)).fetchone()
            stamp = utc_now()
            connection.execute("""INSERT INTO translation_runs(id,project_id,producer_job_id,transcript_revision_id,source_asset_id,input_fingerprint,
                provider,model_id,provider_sdk_version,prompt_version,status,created_at,api_type,api_version,wire_protocol_version,pass_strategy,
                normalization_policy_version,adapter_version,provider_strategy_version)
                VALUES(?,?,?,?,?,?,'local','Hy-MT3-7B-Q4_K_M',NULL,?,'QUEUED',?,'chat_completions','v1','local-labeled-lines-v1','SINGLE_PASS',?,'llama.cpp/b12676','local-batch-80-60-v1')""",
                (run_id,project_id,job_id,revision_id,current["source_asset_id"],job["input_fingerprint"],PROMPT_VERSION,stamp,"translation-normalize-v1"))
            for unit in current_units:
                connection.execute("""INSERT INTO translation_units(run_id,project_id,unit_id,logical_unit_key,position,chunk_no,
                    revision_segment_ids_json,source_segment_ids_json,source_text,start_us,end_us,status)
                    VALUES(?,?,?,?,?,NULL,?,?,?,?,?,'PENDING')""",(run_id,project_id,unit["unit_id"],unit["logical_unit_key"],unit["position"],
                    self._json(unit["revision_segment_ids"]),self._json(unit["source_segment_ids"]),unit["source_text"],unit["start_us"],unit["end_us"]))
            connection.execute("INSERT INTO local_translation_state(run_id,next_position,batch_limit,timeout_restart_used,updated_at) VALUES(?,0,80,0,?)",(run_id,stamp))
        request_digest = {"project_id":project_id,"kind":"local_translation","accepted_transcript_revision_id":revision_id,
                          "target_language":"vi","snapshot":snapshot}
        job = self.jobs.create_job(project_id,"local_translation",snapshot,idempotency_key,activation_stage=ActivationStage.TRANSLATION,
                                   creation_hook=create_rows,update_stage_input=True,request_digest_value=request_digest)
        with closing(self.database.connect()) as c:
            run = c.execute("SELECT id FROM translation_runs WHERE producer_job_id=?",(job["id"],)).fetchone()
        return {"job":job,"run_id":run["id"] if run else None}


    def get_run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        with closing(self.database.connect()) as connection:
            run = connection.execute("SELECT * FROM translation_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if run is None:
                return None
            value = dict(run)
            active = connection.execute("SELECT active_artifact_id,input_fingerprint FROM stage_pointers WHERE project_id=? AND stage='translation'", (project_id,)).fetchone()
            value["is_active"] = bool(active and active["active_artifact_id"] == run["artifact_id"] and active["input_fingerprint"] == self.jobs.get_job(run["producer_job_id"])["input_fingerprint"])
            value["chunks"] = [dict(row) for row in connection.execute("SELECT * FROM translation_chunks WHERE run_id=? ORDER BY chunk_no", (run_id,)).fetchall()]
            value["units"] = []
            for row in connection.execute("SELECT * FROM translation_units WHERE run_id=? ORDER BY position", (run_id,)).fetchall():
                item = dict(row)
                item["final_text"] = item["final_text"] if item["final_text"] is not None else item["pass2_text"]
                item["revision_segment_ids"] = json.loads(item.pop("revision_segment_ids_json"))
                item["source_segment_ids"] = json.loads(item.pop("source_segment_ids_json"))
                item["pass1_notes"] = json.loads(item.pop("pass1_notes_json"))
                item["final_notes"] = json.loads(item.pop("final_notes_json"))
                item["review_flags"] = json.loads(item.pop("review_flags_json"))
                value["units"].append(item)
            return value

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM translation_runs WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()]
        return [self.get_run(project_id, run_id) for run_id in ids]
