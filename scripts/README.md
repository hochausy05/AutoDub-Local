# Scripts — Windows development launcher

From the repository root, run:

```powershell
.\scripts\windows\dev.ps1
```

The script checks Python 3.14 and Node 22.12+, creates `backend/.venv` and installs pinned backend requirements if missing, runs `npm ci` if frontend dependencies are missing, then starts FastAPI and Vite bound to IPv4 loopback. It generates a one-time random bootstrap secret, passes it only to the backend process, and opens the UI with the secret in a URL fragment. The UI removes the fragment before sending the secret to the same-origin proxied bootstrap route. Stop with Ctrl+C; processes are stopped by the launcher. This is a development launcher, not a packaged installer or production service supervisor.

Run API and frontend integration smoke tests with:

```powershell
.\scripts\windows\dev.ps1 -SmokeTest
```

Development logs are stored under ignored `.dev-run/`. Do not copy legacy secrets or binaries. Same-user local processes and browser extensions are outside this skeleton's protection boundary.
