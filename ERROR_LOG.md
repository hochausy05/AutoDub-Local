# ERROR_LOG — Reproducible Incidents in the New Project

# ERROR_LOG — Reproducible Incidents in the New Project

## MEDIA-01A-FIX-001 — Import POST rejected because body model was parsed as query
- **Date/time:** 2026-11-23 16:56 UTC+7
- **Task/environment:** MEDIA-01A-FIX; Windows development environment, exact Windows build not recorded.
- **Reproduction:** Open an existing Project, select COPY, choose a video, then let the UI POST JSON `{selection_id, mode}` to `/api/v1/projects/{project_id}/media/imports` with its existing session/CSRF/Host/Origin headers.
- **Observed before fix:** HTTP 422 `validation_error`, sanitized detail `[{"field":"query","type":"missing"}]`. Reproduced with TestClient using the same method, URL shape, body and security headers.
- **Expected:** FastAPI validates JSON fields `selection_id` and `mode`, then validates the supplied selection and creates the durable import Job only when the selection is valid.
- **Root cause:** `ImportRequest` was declared inside `create_app`, while `main.py` uses postponed annotations. FastAPI observed `body` as `ForwardRef('ImportRequest')` outside the local class scope and classified it as a required query parameter. Route inspection showed one required query field named `body` and no body fields.
- **Remediation:** Move `ImportRequest` to module scope. Keep both fields required and `extra="forbid"`; do not weaken session, CSRF, Origin/Host or selection validation. Also make the `MediaAsset READY` transition and Job READY commit atomic so the API/UI cannot observe a successful Job while its asset is still PENDING.
- **Regression/verification:** The regression POST with valid JSON now reaches selection validation and receives 404 `selection_invalid` for a deliberately invalid token; omitting `mode` still returns 422 with field `mode`. Real FFmpeg fixture integration through FastAPI completed COPY and LINK Jobs, verified FFprobe metadata, preserved the source hash and returned both MediaAssets after reopening the database. Full backend suite: PASS (60 tests); frontend build: PASS.
- **Windows manual status:** Native picker and UI verification NOT RUN/BLOCKED. Launcher smoke was blocked because ports 5173/8000 were occupied by existing Node/Python processes owned by the current Windows account; neither process was stopped.

For known failure patterns from the reference ZIP, use `docs/09_LEGACY_REUSE_AUDIT.md`. Do not import them as incidents in the new project.

Each future incident should record: ID, date/time UTC+7, Task ID, environment, reproducible steps, observed/expected result, sanitized trace, root cause, remediation, regression test, and verified state.
