# Backend — local development API

Python 3.14+ candidate, FastAPI, Uvicorn. Exact direct versions are pinned in `requirements.txt` and were smoke-tested on Python 3.14.2 in this workspace; this is not a compatibility matrix for other Python/Windows builds.

Routes include one-time development session bootstrap, CSRF token retrieval, logout, session-guarded liveness at `/api/v1/health`, Project create/list/read, and Job get/cancel/retry. Job mutations require the existing CSRF token and a durable `Idempotency-Key`; a `project_id` query can scope a Job request and mismatches return 404. Host, Origin and Fetch Metadata checks remain enforced, and CORS/OpenAPI are disabled. Sessions are in memory and disappear when the server restarts. Health means only that the API process responds; it does not imply worker or pipeline readiness.

SQLite defaults to `%LOCALAPPDATA%\AutoDub-Local\autodub.sqlite3` on Windows. `AUTODUB_DATA_ROOT` may select a local folder outside the source tree; UNC and mapped network drives are rejected because WAL requires local storage. The API never accepts a database path. Connections enable foreign keys and a 5-second busy timeout; startup applies numbered migrations transactionally. Migration 1 creates Projects and the shared idempotency table. Migration 2 adds `jobs` and `job_attempts`, immutable input snapshot guards, status-transition checks, unique Attempt numbering and a partial unique index for one active Attempt. Migration 3 adds the internal `artifacts` manifest, Project/Job/Attempt ownership constraints, publication states and immutable READY content guards. Migration 4 adds typed stage pointers with input/pointer generations and durable recovery findings. All migrations preserve existing Project, Job and Attempt rows.

`app.jobs.JobService` is internal; there is no public Job creation endpoint or pipeline executor. Claim and Attempt creation commit together under `BEGIN IMMEDIATE`; claims stay disabled until startup recovery completes and a second active Job is not claimed by the same local supervisor. Same-scope idempotency reuses the shared idempotency records. Retries require a classified retryable failure and stop after three Attempts. Startup treats pre-restart RUNNING/CANCEL_REQUESTED work as INTERRUPTED because this build has no registered worker owner; retry stays explicit. This skeleton has no real worker process or process-tree cancellation implementation, so recovery never kills a process using a persisted PID.

`app.artifacts.ArtifactPublicationService` writes streamed candidates under the managed data root, flushes and validates them using a validator registered for the artifact kind, records SHA-256/size, then publishes to a unique immutable path without replacement before a short READY transaction. Startup recovery revalidates READY files, resumes manifest-backed STAGING/VERIFIED candidates only after validation, and inventories files without a matching manifest in `recovery_findings`; it never deletes candidates. A READY artifact that cannot be validated blocks startup and new claims. Missing/corrupt artifacts lose any active stage pointer but remain in history. Typed internal stage activation snapshots input and pointer generations; completion uses a SQLite CAS, and stale/cancelled results remain historical without replacing the active result. No production media validators are registered and there is no public artifact/activation API.

Media, provider and render routes are not implemented. Run backend checks from this directory with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Windows launcher at `../scripts/windows/dev.ps1` creates `backend/.venv` and installs `requirements.txt` when needed.
