# Backend — development skeleton

Python 3.14+ candidate, FastAPI, Uvicorn. Exact direct versions are pinned in `requirements.txt` and were smoke-tested on Python 3.14.2 in this workspace; this is not a compatibility matrix for other Python/Windows builds.

The only routes implemented are one-time development session bootstrap, CSRF token retrieval, logout and session-guarded API liveness at `/api/v1/health`. The app binds to `127.0.0.1`; Host, Origin and Fetch Metadata checks are enforced, CORS/OpenAPI are disabled, and the bootstrap secret is injected by the Windows development launcher. Sessions are in memory and disappear when the server restarts. Health means only that the API process responds; no database/worker readiness is implied.

All project, media, provider, job, render and file-access routes are absent (404). No arbitrary path endpoint exists. Run backend checks from this directory with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Windows launcher at `../scripts/windows/dev.ps1` creates `backend/.venv` and installs `requirements.txt` when needed.
