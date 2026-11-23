# Tests — current skeleton checks

- Backend: `python -m unittest discover -s tests -v` from `backend/`. Uses FastAPI TestClient to check session bootstrap, health, Host/Origin/Fetch Metadata, CSRF logout, and that OpenAPI/domain routes are not exposed.
- Frontend/API development integration: `scripts/windows/dev.ps1 -SmokeTest`. Node's built-in test runner checks the Vite page and calls session/health through its `/api` proxy against the actual local FastAPI process.
- Frontend build/type check: `npm run build` from `frontend/`.

No ASR, Gemini, TTS, SQLite, media, hardware, FFmpeg or render acceptance tests exist yet. The TRANS-01 handoff must keep CoverageGroup source Segment coverage distinct from TranslationUnit mapping of included RevisionSegments.
