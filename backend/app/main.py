"""Minimal loopback API skeleton with a development-only local session."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


SESSION_COOKIE = "autodub_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
BOOTSTRAP_TTL_SECONDS = 60


@dataclass
class Session:
    csrf_token: str
    expires_at: float


def _is_loopback(address: str | None) -> bool:
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def create_app(
    bootstrap_secret: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    allowed_origin: str = "http://127.0.0.1:5173",
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Create the app; a missing bootstrap secret leaves session bootstrap disabled."""
    if host != "127.0.0.1":
        raise ValueError("The development API must bind to IPv4 loopback only.")
    if port != 8000 or allowed_origin != "http://127.0.0.1:5173":
        raise ValueError("The development skeleton uses fixed backend and UI ports/origin.")
    app = FastAPI(
        title="AutoDub Local API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    expected_host = f"{host}:{port}"
    sessions: dict[str, Session] = {}
    secret_deadline = clock() + BOOTSTRAP_TTL_SECONDS
    bootstrap_consumed = False
    bootstrap_attempts = 0

    @app.middleware("http")
    async def local_security_boundary(request: Request, call_next):
        peer = request.client.host if request.client else None
        if not _is_loopback(peer):
            return JSONResponse(status_code=403, content={"error": "loopback_required"})

        host_headers = request.headers.getlist("host")
        if host_headers != [expected_host]:
            return JSONResponse(status_code=400, content={"error": "invalid_host"})

        origin = request.headers.get("origin")
        if (origin is not None and origin != allowed_origin) or (request.method not in {"GET", "HEAD"} and origin != allowed_origin):
            return JSONResponse(status_code=403, content={"error": "invalid_origin"})

        if request.headers.get("sec-fetch-site") != "same-origin":
            return JSONResponse(status_code=403, content={"error": "invalid_fetch_site"})

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def get_session(request: Request) -> tuple[str, Session] | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        current = sessions.get(token)
        if current is None:
            return None
        if current.expires_at <= clock():
            sessions.pop(token, None)
            return None
        return token, current

    @app.post("/api/v1/session/bootstrap")
    async def bootstrap(request: Request):
        nonlocal bootstrap_consumed, bootstrap_attempts
        if bootstrap_secret is None:
            return JSONResponse(status_code=503, content={"error": "bootstrap_disabled"})
        if bootstrap_consumed or clock() > secret_deadline:
            return JSONResponse(status_code=410, content={"error": "bootstrap_expired"})
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse(status_code=415, content={"error": "json_required"})
        if bootstrap_attempts >= 5:
            return JSONResponse(status_code=429, content={"error": "bootstrap_rate_limited"})

        bootstrap_attempts += 1
        supplied = request.headers.get("x-local-bootstrap", "")
        if not hmac.compare_digest(supplied, bootstrap_secret):
            return JSONResponse(status_code=401, content={"error": "bootstrap_invalid"})
        bootstrap_consumed = True

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        sessions[session_token] = Session(
            csrf_token=csrf_token,
            expires_at=clock() + SESSION_TTL_SECONDS,
        )
        response = JSONResponse(content={"csrf_token": csrf_token})
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            secure=False,  # HTTP is limited to IPv4 loopback in this development skeleton.
            samesite="strict",
            path="/",
            max_age=SESSION_TTL_SECONDS,
        )
        return response

    @app.get("/api/v1/session/csrf")
    async def csrf(request: Request):
        current = get_session(request)
        if current is None:
            return JSONResponse(status_code=401, content={"error": "session_required"})
        return {"csrf_token": current[1].csrf_token}

    @app.post("/api/v1/session/logout")
    async def logout(request: Request):
        current = get_session(request)
        if current is None:
            return JSONResponse(status_code=401, content={"error": "session_required"})
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), current[1].csrf_token):
            return JSONResponse(status_code=403, content={"error": "csrf_invalid"})
        sessions.pop(current[0], None)
        response = JSONResponse(status_code=204, content=None)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/health")
    async def health(request: Request):
        if get_session(request) is None:
            return JSONResponse(status_code=401, content={"error": "session_required"})
        # This reports API liveness only. No database/worker readiness is implied.
        return {"status": "ok", "service": "autodub-local-backend"}

    return app


app = create_app(
    os.environ.get("AUTODUB_BOOTSTRAP_SECRET"),
    host=os.environ.get("APP_HOST", "127.0.0.1"),
    port=int(os.environ.get("APP_PORT", "8000")),
    allowed_origin=os.environ.get("APP_ALLOWED_ORIGIN", "http://127.0.0.1:5173"),
)
