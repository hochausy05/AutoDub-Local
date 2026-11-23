"""Minimal loopback API skeleton with a development-only local session."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi.responses import JSONResponse

from app.database import Database, IdempotencyConflict
from app.artifacts import ArtifactPublicationService
from app.jobs import JobConflict, JobInputError, JobNotFound, JobService
from app.recovery import StartupRecovery


SESSION_COOKIE = "autodub_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
BOOTSTRAP_TTL_SECONDS = 60


@dataclass
class Session:
    csrf_token: str
    expires_at: float


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
        return value


class ProjectCreated(BaseModel):
    id: uuid.UUID
    lock_version: int


class ProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: str
    updated_at: str
    lock_version: int
    schema_version: int


class ProjectListResponse(BaseModel):
    items: list[ProjectRead]
    limit: int
    offset: int


class JobAttemptRead(BaseModel):
    id: uuid.UUID
    attempt_no: int
    schema_version: int
    status: str
    started_at: str
    ended_at: str | None
    error_code: str | None
    retryable: bool
    progress: dict[str, object] | None


class JobRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    input_fingerprint: str
    status: str
    cancel_requested: bool
    lock_version: int
    schema_version: int
    created_at: str
    updated_at: str
    attempt: JobAttemptRead | None
    attempts: list[JobAttemptRead]
    progress: dict[str, object] | None
    error_code: str | None


class EmptyJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["FAILED", "INTERRUPTED"]


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
    data_root: Path | str | None = None,
) -> FastAPI:
    """Create the app; a missing bootstrap secret leaves session bootstrap disabled."""
    if host != "127.0.0.1":
        raise ValueError("The development API must bind to IPv4 loopback only.")
    if port != 8000 or allowed_origin != "http://127.0.0.1:5173":
        raise ValueError("The development skeleton uses fixed backend and UI ports/origin.")
    database = Database(data_root)
    artifact_service = ArtifactPublicationService(database)
    job_service = JobService(database, artifact_service)
    startup_recovery = StartupRecovery(database, job_service, artifact_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        _app.state.recovery_report = startup_recovery.reconcile()
        yield

    app = FastAPI(
        title="AutoDub Local API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.artifact_service = artifact_service
    app.state.job_service = job_service
    app.state.startup_recovery = startup_recovery
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

    def api_error(status: int, code: str, message: str, *, details=None):
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "retryable": False, "details": details}, "request_id": str(uuid.uuid4())},
        )

    def require_session(request: Request) -> Session | JSONResponse:
        current = get_session(request)
        if current is None:
            return api_error(401, "session_required", "A local session is required.")
        return current[1]

    def require_mutation_session(request: Request) -> Session | JSONResponse:
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), current.csrf_token):
            return api_error(403, "csrf_invalid", "A valid CSRF token is required.")
        return current

    def job_exception(exc: Exception) -> JSONResponse:
        if isinstance(exc, JobNotFound):
            return api_error(404, "job_not_found", "Job was not found.")
        if isinstance(exc, IdempotencyConflict):
            return api_error(409, "idempotency_key_reused", "The idempotency key was already used with a different request.")
        if isinstance(exc, JobInputError):
            return api_error(422, str(exc), "The Job request is invalid.")
        if isinstance(exc, JobConflict):
            return api_error(409, exc.code, "The Job cannot make that state transition.")
        raise exc

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(part) for part in error["loc"] if part != "body"), "type": error["type"]} for error in exc.errors()]
        return api_error(422, "validation_error", "The request is invalid.", details=details)

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

    @app.post("/api/v1/projects", response_model=ProjectCreated, status_code=201)
    def create_project(body: ProjectCreate, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        supplied_csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied_csrf, current.csrf_token):
            return api_error(403, "csrf_invalid", "A valid CSRF token is required.")
        key = request.headers.get("idempotency-key", "")
        if not 16 <= len(key) <= 255 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            return api_error(422, "invalid_idempotency_key", "A valid Idempotency-Key header is required.")
        try:
            result, replay = database.create_project(body.name, key)
        except IdempotencyConflict:
            return api_error(409, "idempotency_key_reused", "The idempotency key was already used with a different request.")
        response = JSONResponse(status_code=201, content=result)
        response.headers["Idempotency-Replayed"] = "true" if replay else "false"
        return response

    @app.get("/api/v1/projects", response_model=ProjectListResponse)
    def list_projects(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        return {"items": database.list_projects(limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectRead)
    def get_project(project_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed_id = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return api_error(422, "invalid_project_id", "Project ID must be a UUID.")
        if str(parsed_id) != project_id.lower():
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        item = database.get_project(str(parsed_id))
        if item is None:
            return api_error(404, "project_not_found", "Project was not found.")
        return item

    @app.get("/api/v1/jobs/{job_id}", response_model=JobRead)
    def get_job(job_id: str, request: Request, project_id: str | None = Query(default=None)):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            return job_service.get_job(job_id, project_id)
        except (JobNotFound, JobInputError) as exc:
            return job_exception(exc)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobRead)
    def cancel_job(job_id: str, body: EmptyJobRequest, request: Request, project_id: str | None = Query(default=None)):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        key = request.headers.get("idempotency-key", "")
        try:
            job_service.get_job(job_id, project_id)
            result = job_service.request_cancel(job_id, key)
        except (JobNotFound, JobInputError, JobConflict, IdempotencyConflict) as exc:
            return job_exception(exc)
        response = JSONResponse(status_code=202 if result["status"] == "CANCEL_REQUESTED" else 200, content=result)
        return response

    @app.post("/api/v1/jobs/{job_id}/retry", response_model=JobRead)
    def retry_job(job_id: str, body: RetryJobRequest, request: Request, project_id: str | None = Query(default=None)):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        key = request.headers.get("idempotency-key", "")
        try:
            job_service.get_job(job_id, project_id)
            result = job_service.retry_job(job_id, body.expected_status, key)
        except (JobNotFound, JobInputError, JobConflict, IdempotencyConflict) as exc:
            return job_exception(exc)
        return JSONResponse(status_code=202, content=result)

    return app


app = create_app(
    os.environ.get("AUTODUB_BOOTSTRAP_SECRET"),
    host=os.environ.get("APP_HOST", "127.0.0.1"),
    port=int(os.environ.get("APP_PORT", "8000")),
    allowed_origin=os.environ.get("APP_ALLOWED_ORIGIN", "http://127.0.0.1:5173"),
)
