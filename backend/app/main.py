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
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.database import Database, IdempotencyConflict
from app.artifacts import ArtifactPublicationService
from app.jobs import JobConflict, JobInputError, JobNotFound, JobService
from app.recovery import StartupRecovery
from app.media import FFprobe, LinkManifestValidator, MediaError, MediaImportService, MediaImportWorker, SourceVideoValidator, parse_single_byte_range
from app.asr import ASRConfig, ASRError, ASRService, ASRWorker, TranscriptArtifactValidator
from app.transcripts import TranscriptError, TranscriptService
from app.translation import TranslationError, TranslationService, TranslationWorker, TranslationArtifactValidator


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
    retry_after_at: str | None = None
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
    retry_available: bool = False
    retry_block_reason: str | None = None


class EmptyJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["FAILED", "INTERRUPTED"]


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: str = Field(min_length=32, max_length=128)
    mode: Literal["COPY", "LINK"]


class RelinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: str = Field(min_length=32, max_length=128)
    expected_lock_version: int = Field(ge=1)


class ASRJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["auto", "en", "zh"] = "auto"
    allow_model_download: bool = False


class ProcessingSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_asset_id: uuid.UUID
    expected_generation: int = Field(ge=0)


class TranscriptRevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: uuid.UUID | None = None
    parent_revision_id: uuid.UUID | None = None


class TranscriptSegmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: uuid.UUID
    text: str | None = None
    start_us: int | None = None
    end_us: int | None = None


class TranscriptRevisionSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_lock_version: int = Field(ge=1)
    edits: list[TranscriptSegmentEdit] = Field(min_length=1, max_length=10000)


class TranscriptSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_lock_version: int = Field(ge=1)
    split_at_codepoint: int
    split_at_us: int


class TranscriptMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_lock_version: int = Field(ge=1)
    merged_text: str


class TranscriptReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_lock_version: int = Field(ge=1)


class TranscriptAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_lock_version: int = Field(ge=1)
    expected_active_revision_id: uuid.UUID | None = None


class TranslationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted_transcript_revision_id: uuid.UUID
    target_language: Literal["vi"] = "vi"
    privacy_consent_confirmed: bool


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
    ffprobe = FFprobe(os.environ.get("FFPROBE_PATH"))
    artifact_service.validators.update({"source_video": SourceVideoValidator(ffprobe), "media_link_manifest": LinkManifestValidator()})
    media_service = MediaImportService(database, job_service, artifact_service, ffprobe)
    media_worker = MediaImportWorker(media_service)
    artifact_service.validators["transcript_json"] = TranscriptArtifactValidator()
    asr_service = ASRService(database, job_service, artifact_service, media_service)
    asr_worker = ASRWorker(asr_service)
    transcript_service = TranscriptService(database)
    artifact_service.validators["translation_json"] = TranslationArtifactValidator(database)
    translation_service = TranslationService(database, job_service)
    translation_worker = TranslationWorker(translation_service, artifact_service)
    startup_recovery = StartupRecovery(database, job_service, artifact_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        _app.state.recovery_report = startup_recovery.reconcile()
        _app.state.recovery_report["media_assets_recovered"] = media_service.reconcile_pending_assets()
        media_worker.start()
        asr_worker.start()
        translation_worker.start()
        try:
            yield
        finally:
            translation_worker.stop()
            asr_worker.stop()
            media_worker.stop()

    app = FastAPI(
        title="AutoDub Local API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.transcript_service = transcript_service
    app.state.translation_service = translation_service
    app.state.database = database
    app.state.artifact_service = artifact_service
    app.state.job_service = job_service
    app.state.startup_recovery = startup_recovery
    app.state.media_service = media_service
    app.state.asr_service = asr_service
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
            messages = {"retry_cooldown": "Hãy chờ hết thời gian tạm dừng trước khi thử lại.",
                        "job_already_active": "Job dịch cho phiên bản lời thoại này đang chạy.",
                        "job_not_retryable": "Job này không thể tự động thử lại.",
                        "retry_limit_reached": "Job đã hết số lần thử được phép."}
            message = messages.get(exc.code, "The Job cannot make that state transition.")
            return api_error(409, exc.code, message)
        raise exc

    def transcript_exception(exc: TranscriptError | IdempotencyConflict) -> JSONResponse:
        code = exc.code if isinstance(exc, TranscriptError) else "idempotency_key_reused"
        if code in {"transcript_revision_not_found", "transcription_run_not_found", "transcript_segment_not_found"}:
            status = 404
        elif code in {"transcript_revision_conflict", "transcript_active_revision_conflict", "transcript_input_stale", "transcript_revision_immutable", "transcript_parent_not_accepted", "transcript_review_required", "transcript_coverage_group_not_pending", "transcript_merge_incompatible", "transcript_source_coverage_duplicate"} or isinstance(exc, IdempotencyConflict):
            status = 409
        else:
            status = 422
        return api_error(status, code, "Không thể cập nhật lời thoại. Bản nháp đã lưu trước đó vẫn được giữ.")

    def translation_exception(exc: TranslationError | IdempotencyConflict) -> JSONResponse:
        if isinstance(exc, IdempotencyConflict):
            return api_error(409, "idempotency_key_reused", "Khóa yêu cầu đã được dùng cho nội dung khác.")
        return api_error(exc.status, exc.code, {
            "accepted_transcript_required": "Hãy duyệt lời thoại trước khi dịch.",
            "accepted_transcript_stale": "Video hoặc lời thoại đã thay đổi. Hãy tải lại phiên bản hiện hành.",
            "transcript_review_required": "Hãy hoàn tất việc kiểm tra lời thoại trước khi dịch.",
            "translation_unit_coverage_invalid": "Không thể xác minh đầy đủ các câu lời thoại.",
            "translation_no_units": "Không có câu lời thoại cần dịch.",
            "privacy_consent_required": "Cần xác nhận việc gửi lời thoại tới Gemini.",
            "gemini_api_key_missing": "Chưa cấu hình khóa Gemini trong môi trường backend.",
            "gemini_api_key_invalid": "Gemini không chấp nhận khóa API đã cấu hình.",
            "gemini_permission_denied": "Tài khoản hoặc dự án chưa có quyền sử dụng Gemini API.",
            "gemini_quota_exceeded": "Đã đạt giới hạn sử dụng Gemini. Hãy thử lại khi hạn mức được khôi phục.",
            "gemini_service_unavailable": "Dịch vụ Gemini đang tạm thời không khả dụng. Vui lòng thử lại sau.",
            "gemini_network_error": "Không thể kết nối tới Gemini. Hãy kiểm tra kết nối Internet.",
            "gemini_transient_provider_error": "Dịch vụ Gemini đang tạm thời gặp sự cố. Vui lòng thử lại sau.",
            "gemini_model_unsupported": "Model Gemini đã cấu hình không khả dụng.",
            "gemini_request_invalid": "Cấu hình hoặc schema yêu cầu Gemini không hợp lệ.",
            "gemini_proxy_error": "Proxy mạng được cấu hình không thể kết nối tới Gemini.",
            "gemini_tls_error": "Không thể xác minh chứng chỉ kết nối Gemini. Kiểm tra cấu hình chứng chỉ hệ thống.",
            "job_already_active": "Job dịch cho phiên bản lời thoại này đang chạy.",
            "gemini_network_error": "Không kết nối được Gemini. Hãy kiểm tra mạng rồi thử lại.",
            "gemini_model_unsupported": "Model Gemini hiện không hỗ trợ yêu cầu này.",
        }.get(exc.code, "Không thể tạo hoặc tải kết quả dịch."))

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

    @app.post("/api/v1/projects/{project_id}/selections", status_code=201)
    def create_source_selection(project_id: str, body: EmptyJobRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        session = get_session(request)
        try:
            return media_service.choose_source(session[0], str(parsed))
        except MediaError as exc:
            status = 404 if exc.code == "project_not_found" else 503 if exc.code.startswith("picker_") else 409
            return api_error(status, exc.code, "Không thể chọn video nguồn.")

    @app.post("/api/v1/projects/{project_id}/media/{asset_id}/relink-selections", status_code=201)
    def create_relink_selection(project_id: str, asset_id: str, body: EmptyJobRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            asset = uuid.UUID(asset_id)
            if str(project) != project_id.lower() or str(asset) != asset_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_media_id", "Project và Asset ID phải là UUID chuẩn.")
        session = get_session(request)
        try:
            return media_service.choose_source(session[0], str(project), purpose="relink", asset_id=str(asset))
        except MediaError as exc:
            status = 404 if exc.code in {"project_not_found", "relink_not_allowed"} else 503 if exc.code.startswith("picker_") else 409
            return api_error(status, exc.code, "Không thể chọn lại video nguồn.")

    @app.post("/api/v1/projects/{project_id}/media/{asset_id}/relink", status_code=200)
    def relink_media(project_id: str, asset_id: str, body: RelinkRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        key = request.headers.get("idempotency-key", "")
        session = get_session(request)
        try:
            return media_service.relink_asset(session[0], project_id, asset_id, body.selection_id, body.expected_lock_version, key)
        except IdempotencyConflict:
            return api_error(409, "idempotency_key_reused", "Idempotency-Key đã được dùng cho nội dung khác.")
        except MediaError as exc:
            code = exc.code
            status = 410 if code == "selection_expired" else 404 if code == "asset_not_found" else 409 if code in {"selection_invalid", "selection_consumed", "asset_revision_conflict", "relink_content_mismatch", "relink_not_required"} else 422
            return api_error(status, code, "Không thể liên kết lại video.")

    @app.post("/api/v1/projects/{project_id}/media/imports", status_code=202)
    def create_media_import(project_id: str, body: ImportRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        key = request.headers.get("idempotency-key", "")
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        session = get_session(request)
        try:
            job = media_service.create_import_job(session[0], str(parsed), body.selection_id, body.mode, key)
            return job
        except IdempotencyConflict:
            return api_error(409, "idempotency_key_reused", "Idempotency-Key đã được dùng cho nội dung khác.")
        except (MediaError, JobInputError) as exc:
            code = exc.code if isinstance(exc, MediaError) else str(exc)
            status = 410 if code == "selection_expired" else 409 if code in {"selection_consumed", "source_changed"} else 404 if code in {"project_not_found", "selection_invalid"} else 422
            return api_error(status, code, "Không thể tạo Import Job.")

    @app.get("/api/v1/projects/{project_id}/media")
    def list_project_media(project_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        if database.get_project(str(parsed)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        return {"items": media_service.list_assets(str(parsed))}

    @app.get("/api/v1/asr/models")
    def list_asr_models(request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        return asr_service.models()

    @app.get("/api/v1/projects/{project_id}/processing-source")
    def get_processing_source(project_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        if database.get_project(str(parsed)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        return asr_service.get_processing_source(str(parsed))

    @app.put("/api/v1/projects/{project_id}/processing-source")
    def select_processing_source(project_id: str, body: ProcessingSourceRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID must be a canonical UUID.")
        if database.get_project(str(parsed)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        try:
            return asr_service.select_processing_source(str(parsed), str(body.media_asset_id), body.expected_generation)
        except MediaError as exc:
            status = 409 if exc.code in {"source_selection_conflict", "asset_changed"} else 422 if exc.code == "invalid_source_generation" else 404
            return api_error(status, exc.code, "Không thể chọn video xử lý. Kiểm tra trạng thái video rồi thử lại.")

    @app.post("/api/v1/projects/{project_id}/asr/jobs", status_code=202)
    def create_asr_job(project_id: str, body: ASRJobRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        key = request.headers.get("idempotency-key", "")
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID phải là UUID chuẩn.")
        try:
            result = asr_service.create_job(
                str(parsed),
                ASRConfig(model="base", language=None if body.language == "auto" else body.language,
                          device="cpu", vad_enabled=True, beam_size=3, allow_cpu_fallback=False,
                          allow_model_download=body.allow_model_download), key,
            )
            return result
        except IdempotencyConflict:
            return api_error(409, "idempotency_key_reused", "Idempotency-Key đã được dùng cho cấu hình khác.")
        except ASRError as exc:
            code = exc.code
            status = 404 if code == "asset_unavailable" else 409 if code in {"model_download_consent_required", "source_changed", "asset_changed"} else 422
            return api_error(status, code, "Không thể tạo ASR Job với đầu vào hiện tại.")
        except (JobInputError, JobConflict) as exc:
            code = exc.code if isinstance(exc, JobConflict) else str(exc)
            return api_error(409 if isinstance(exc, JobConflict) else 422, code, "Không thể tạo ASR Job.")

    @app.get("/api/v1/projects/{project_id}/asr/runs")
    def list_asr_runs(project_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            parsed = uuid.UUID(project_id)
            if str(parsed) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID phải là UUID chuẩn.")
        if database.get_project(str(parsed)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        return {"items": asr_service.list_runs(str(parsed))}

    @app.get("/api/v1/projects/{project_id}/asr/runs/{run_id}")
    def get_asr_run(project_id: str, run_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            run = uuid.UUID(run_id)
            if str(project) != project_id.lower() or str(run) != run_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_run_id", "Project và Run ID phải là UUID chuẩn.")
        result = asr_service.get_run(str(project), str(run))
        if result is None:
            return api_error(404, "transcription_run_not_found", "Transcription Run không tồn tại.")
        return result

    @app.get("/api/v1/projects/{project_id}/transcripts")
    def list_transcript_revisions(project_id: str, source_asset_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, asset = uuid.UUID(project_id), uuid.UUID(source_asset_id)
            if str(project) != project_id.lower() or str(asset) != source_asset_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_transcript_query", "Project và Asset ID phải là UUID chuẩn.")
        if database.get_project(str(project)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        return {"items": transcript_service.list_revisions(str(project), str(asset))}

    @app.post("/api/v1/projects/{project_id}/transcripts/revisions", status_code=201)
    def create_transcript_draft(project_id: str, body: TranscriptRevisionCreate, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            if str(project) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID phải là UUID chuẩn.")
        if database.get_project(str(project)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        try:
            result = transcript_service.create_draft(
                str(project), source_run_id=str(body.source_run_id) if body.source_run_id else None,
                parent_revision_id=str(body.parent_revision_id) if body.parent_revision_id else None,
                idempotency_key=request.headers.get("idempotency-key", ""),
            )
            return JSONResponse(status_code=201, content=result)
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.get("/api/v1/providers/gemini/status")
    def gemini_provider_status(request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        return {"configured": translation_service.api_key_configured()}

    @app.post("/api/v1/projects/{project_id}/translations/jobs", status_code=202)
    def create_translation_job(project_id: str, body: TranslationJobRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            revision = uuid.UUID(str(body.accepted_transcript_revision_id))
            if str(project) != project_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_translation_id", "Project hoặc phiên bản lời thoại không hợp lệ.")
        if database.get_project(str(project)) is None:
            return api_error(404, "project_not_found", "Project không tồn tại.")
        try:
            result = translation_service.create_job(str(project), str(revision), body.privacy_consent_confirmed, request.headers.get("idempotency-key", ""))
            return JSONResponse(status_code=202, content=result)
        except (TranslationError, IdempotencyConflict) as exc:
            return translation_exception(exc)

    @app.get("/api/v1/projects/{project_id}/translations/runs")
    def list_translation_runs(project_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            if str(project) != project_id.lower(): raise ValueError
        except ValueError:
            return api_error(422, "invalid_project_id", "Project ID không hợp lệ.")
        if database.get_project(str(project)) is None:
            return api_error(404, "project_not_found", "Project không tồn tại.")
        return {"items": translation_service.list_runs(str(project))}

    @app.get("/api/v1/projects/{project_id}/translations/runs/{run_id}")
    def get_translation_run(project_id: str, run_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, run = uuid.UUID(project_id), uuid.UUID(run_id)
            if str(project) != project_id.lower() or str(run) != run_id.lower(): raise ValueError
        except ValueError:
            return api_error(422, "invalid_translation_id", "Project hoặc kết quả dịch không hợp lệ.")
        result = translation_service.get_run(str(project), str(run))
        if result is None:
            return api_error(404, "translation_run_not_found", "Không tìm thấy kết quả dịch.")
        return result

    @app.get("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}")
    def get_transcript_revision(project_id: str, revision_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision = uuid.UUID(project_id), uuid.UUID(revision_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower():
                raise ValueError
            return transcript_service.get_revision(str(project), str(revision))
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project và Revision ID phải là UUID chuẩn.")
        except TranscriptError as exc:
            return transcript_exception(exc)

    @app.patch("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}")
    def save_transcript_draft(project_id: str, revision_id: str, body: TranscriptRevisionSave, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision = uuid.UUID(project_id), uuid.UUID(revision_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project và Revision ID phải là UUID chuẩn.")
        try:
            return transcript_service.save_edits(
                str(project), str(revision), body.expected_lock_version,
                [edit.model_dump(mode="json", exclude_unset=True) for edit in body.edits], request.headers.get("idempotency-key", ""),
            )
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.post("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}/segments/{row_id}/split")
    def split_transcript_segment(project_id: str, revision_id: str, row_id: str, body: TranscriptSplitRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision, row = uuid.UUID(project_id), uuid.UUID(revision_id), uuid.UUID(row_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower() or str(row) != row_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project, Revision và Segment ID phải là UUID chuẩn.")
        try:
            return transcript_service.split_segment(str(project), str(revision), str(row), body.expected_lock_version, body.split_at_codepoint, body.split_at_us, request.headers.get("idempotency-key", ""))
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.post("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}/segments/{row_id}/merge-next")
    def merge_transcript_segment(project_id: str, revision_id: str, row_id: str, body: TranscriptMergeRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision, row = uuid.UUID(project_id), uuid.UUID(revision_id), uuid.UUID(row_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower() or str(row) != row_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project, Revision và Segment ID phải là UUID chuẩn.")
        try:
            return transcript_service.merge_next(str(project), str(revision), str(row), body.expected_lock_version, body.merged_text, request.headers.get("idempotency-key", ""))
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.post("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}/coverage-groups/{group_id}/review")
    def review_transcript_coverage_group(project_id: str, revision_id: str, group_id: str, body: TranscriptReviewRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision, group = uuid.UUID(project_id), uuid.UUID(revision_id), uuid.UUID(group_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower() or str(group) != group_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project, Revision và Coverage ID phải là UUID chuẩn.")
        try:
            return transcript_service.review_group(str(project), str(revision), str(group), body.expected_lock_version, request.headers.get("idempotency-key", ""))
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.post("/api/v1/projects/{project_id}/transcripts/revisions/{revision_id}/accept")
    def accept_transcript_revision(project_id: str, revision_id: str, body: TranscriptAcceptRequest, request: Request):
        current = require_mutation_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project, revision = uuid.UUID(project_id), uuid.UUID(revision_id)
            if str(project) != project_id.lower() or str(revision) != revision_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_revision_id", "Project và Revision ID phải là UUID chuẩn.")
        try:
            return transcript_service.accept(str(project), str(revision), body.expected_lock_version, str(body.expected_active_revision_id) if body.expected_active_revision_id else None, request.headers.get("idempotency-key", ""))
        except (TranscriptError, IdempotencyConflict) as exc:
            return transcript_exception(exc)

    @app.api_route("/api/v1/projects/{project_id}/media/{asset_id}/stream", methods=["GET", "HEAD"])
    def stream_project_media(project_id: str, asset_id: str, request: Request):
        current = require_session(request)
        if isinstance(current, JSONResponse):
            return current
        try:
            project = uuid.UUID(project_id)
            asset = uuid.UUID(asset_id)
            if str(project) != project_id.lower() or str(asset) != asset_id.lower():
                raise ValueError
        except ValueError:
            return api_error(422, "invalid_media_id", "Project và Asset ID phải là UUID chuẩn.")
        if database.get_project(str(project)) is None:
            return api_error(404, "project_not_found", "Project was not found.")
        try:
            opened = media_service.open_media_stream(str(project), str(asset))
        except MediaError as exc:
            status = 404 if exc.code == "asset_not_found" else 415 if exc.code == "media_type_unsupported" else 409
            return api_error(status, exc.code, "Video hiện không sẵn sàng để phát.")
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": opened.etag,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if_none_match = request.headers.get("if-none-match", "")
        if any(tag.strip() == "*" or tag.strip().removeprefix("W/") == opened.etag for tag in if_none_match.split(",")):
            opened.handle.close()
            return Response(status_code=304, headers=headers)
        start, end = 0, opened.size - 1
        status = 200
        range_header = request.headers.get("range")
        if range_header and request.headers.get("if-range", opened.etag) == opened.etag:
            try:
                start, end = parse_single_byte_range(range_header, opened.size)
            except MediaError as exc:
                opened.handle.close()
                if exc.code == "range_not_satisfiable":
                    headers["Content-Range"] = f"bytes */{opened.size}"
                    return Response(status_code=416, headers=headers)
                return api_error(400, "invalid_range", "Chỉ hỗ trợ một byte range hợp lệ.")
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{opened.size}"
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        headers["Content-Type"] = opened.content_type
        if request.method == "HEAD":
            opened.handle.close()
            return Response(status_code=status, headers=headers, media_type=None)
        return StreamingResponse(media_service.stream_chunks(opened, start, length), status_code=status, headers=headers, media_type=None)

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
