"""Persistent 80/60 batch policy helpers."""
DEFAULT_BATCH = 80
FALLBACK_BATCH = 60

def next_batch(units: list[dict], cursor: int, batch_limit: int) -> list[dict]:
    if batch_limit not in (DEFAULT_BATCH, FALLBACK_BATCH) or cursor < 0:
        raise ValueError("invalid_local_batch_state")
    return units[cursor:cursor + batch_limit]

def is_context_limit(exc: Exception) -> bool:
    code = getattr(exc, "code", "")
    return code == "local_context_limit"
