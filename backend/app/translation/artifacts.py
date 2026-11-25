"""Local translation artifact contract helpers."""
def manifest_for(run, units, snapshot):
    return {"schema_version":1,"run_id":run["id"],"input_fingerprint":run["input_fingerprint"],
            "source_asset_id":run["source_asset_id"],"transcript_revision_id":run["transcript_revision_id"],
            "provider":"local","model_id":run["model_id"],"model_sha256":snapshot["model_sha256"],
            "runtime_identity":snapshot["runtime_identity"],"context_size":snapshot["context_size"],
            "gpu_layers":snapshot["gpu_layers"],"parallel_slots":snapshot["parallel_slots"],
            "prompt_version":run["prompt_version"],"normalization_version":"translation-normalize-v1",
            "prompt_sha256":snapshot["prompt_sha256"],"execution_policy":"local-batch-80-60-v1",
            "batch_policy":snapshot["batch_policy"],"timeout_policy":snapshot["timeout_policy"],"pass_strategy":"SINGLE_PASS",
            "units":[{"unit_id":u["unit_id"],"logical_unit_key":u["logical_unit_key"],"position":u["position"],
                      "revision_segment_ids":u["revision_segment_ids"],"source_segment_ids":u["source_segment_ids"],
                      "source_text":u["source_text"],"start_us":u["start_us"],"end_us":u["end_us"],
                      "final_text":u["final_text"],"final_notes":[]} for u in units]}
