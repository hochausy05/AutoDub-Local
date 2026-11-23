# Backend — local development API

Python 3.14+ candidate, FastAPI, Uvicorn. Exact direct versions are pinned in `requirements.txt` and were smoke-tested on Python 3.14.2 in this workspace; this is not a compatibility matrix for other Python/Windows builds.

Routes include one-time development session bootstrap, CSRF token retrieval, logout, session-guarded liveness at `/api/v1/health`, Project create/list/read, and Job get/cancel/retry. Job mutations require the existing CSRF token and a durable `Idempotency-Key`; a `project_id` query can scope a Job request and mismatches return 404. Host, Origin and Fetch Metadata checks remain enforced, and CORS/OpenAPI are disabled. Sessions are in memory and disappear when the server restarts. Health means only that the API process responds; it does not imply worker or pipeline readiness.

SQLite defaults to `%LOCALAPPDATA%\AutoDub-Local\autodub.sqlite3` on Windows. `AUTODUB_DATA_ROOT` may select a local folder outside the source tree; UNC and mapped network drives are rejected because WAL requires local storage. The API never accepts a database path. Connections enable foreign keys and a 5-second busy timeout; startup applies numbered migrations transactionally. Migration 1 creates Projects and the shared idempotency table. Migration 2 adds `jobs` and `job_attempts`, immutable input snapshot guards, status-transition checks, unique Attempt numbering and a partial unique index for one active Attempt. Migration 2 preserves existing Projects.

`app.jobs.JobService` is internal; there is no public Job creation endpoint or pipeline executor. Claim and Attempt creation commit together under `BEGIN IMMEDIATE`. Same-scope idempotency reuses the shared idempotency records. Retries require a classified retryable failure and stop after three Attempts. The service records verified completion evidence before `READY`; the test-only `test_controlled` kind is not an ASR/TTS/render success. A `RUNNING` Job remains `RUNNING` after restart for CORE-01D reconciliation; no process termination or startup recovery is claimed.

Media, provider, Job and render routes are not implemented. Run backend checks from this directory with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Windows launcher at `../scripts/windows/dev.ps1` creates `backend/.venv` and installs `requirements.txt` when needed.
