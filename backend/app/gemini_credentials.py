"""Backend-only credential configuration and non-secret state tracking."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from contextlib import closing
from typing import Any

from app.database import Database, utc_now


@dataclass(frozen=True)
class GeminiCredential:
    alias: str
    api_key: str
    quota_group: str
    priority: int


class GeminiCredentialManager:
    """Resolves secrets from process environment; only aliases/state enter SQLite."""
    def __init__(self, database: Database, environ: dict[str, str] | None = None):
        self.database = database
        self.environ = os.environ if environ is None else environ

    def configured(self) -> list[GeminiCredential]:
        raw = self.environ.get("AUTODUB_GEMINI_CREDENTIALS_JSON", "").strip()
        credentials: list[GeminiCredential] = []
        if raw:
            try:
                values = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("AUTODUB_GEMINI_CREDENTIALS_JSON must be valid JSON.") from exc
            if not isinstance(values, list):
                raise ValueError("Gemini credential configuration must be a JSON list.")
            for index, value in enumerate(values):
                if not isinstance(value, dict) or value.get("enabled", True) is not True:
                    continue
                alias, key = value.get("alias"), value.get("api_key")
                group = value.get("quota_group")
                priority = value.get("priority", index)
                if (not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", alias)
                        or not isinstance(key, str) or not key.strip()
                        or not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", group)
                        or not isinstance(priority, int) or isinstance(priority, bool)):
                    raise ValueError("Each Gemini credential requires safe alias, api_key, quota_group, and integer priority.")
                credentials.append(GeminiCredential(alias, key.strip(), group, priority))
        legacy = self.environ.get("AUTODUB_GEMINI_API_KEY", "").strip()
        if legacy and not any(item.api_key == legacy for item in credentials):
            credentials.append(GeminiCredential("legacy", legacy, "legacy", max((item.priority for item in credentials), default=-1) + 1))
        aliases = [item.alias for item in credentials]
        if len(aliases) != len(set(aliases)):
            raise ValueError("Gemini credential aliases must be unique.")
        return sorted(credentials, key=lambda item: (item.priority, item.alias))

    def available(self, exclude: set[str] | None = None) -> list[GeminiCredential]:
        blocked = exclude or set()
        with closing(self.database.connect()) as connection:
            states = {row["alias"]: row["state"] for row in connection.execute("SELECT alias,state FROM translation_credential_states")}
        return [item for item in self.configured() if item.alias not in blocked and states.get(item.alias) not in {"AUTH_FAILED", "DISABLED", "QUOTA_EXHAUSTED"}]

    def mark_auth_failed(self, credential: GeminiCredential, code: str) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("""INSERT INTO translation_credential_states(alias,quota_group,state,failure_code,updated_at)
                VALUES(?,?,'AUTH_FAILED',?,?) ON CONFLICT(alias) DO UPDATE SET quota_group=excluded.quota_group,state='AUTH_FAILED',failure_code=excluded.failure_code,updated_at=excluded.updated_at""",
                (credential.alias, credential.quota_group, code, utc_now()))
