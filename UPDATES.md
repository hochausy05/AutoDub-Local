Gemini Translation Engine has been removed from execution. New translation is temporarily unavailable while Local Translation is prepared; saved translation history remains available in the editor and TTS. Local Translation is not implemented. Backend focused API/recovery checks and frontend build/smoke pass; translation/TTS regression fixtures still require replacement.

# AutoDub Local vNext — Product Updates

TRANS-02C retires Cloudflare and restores Gemini as the sole provider for new translation Runs. Prior Runs retain provenance; retired active Jobs are reconciled and cannot retry. Focused backend tests and frontend build pass with mocks; live providers and Windows browser are NOT RUN. G4 remains previously accepted; G5 remains unaccepted.

TTS-01A adds a working Edge-TTS voice panel for an active accepted translation: live Vietnamese voice catalog, explicit Edge-specific consent, actual voice preview, one-action per-Unit batch, measured MP3 duration, selective retry, cancellation, reuse of unchanged verified Unit artifacts and project-scoped audio playback. Automated synthetic integration tests passed, and one real synthetic-text provider smoke produced a verified non-silent MP3. Windows browser visual acceptance and the 50-Unit mixed-outcome G5 gate remain NOT RUN; G5 is not accepted.

The local application supports persistent Projects through the session- and CSRF-protected API. Projects are stored in SQLite under the Windows local application data folder by default; the UI reads real API data and does not seed sample Projects. Prior backend tests, frontend build and Windows launcher integration smoke passed for their respective slices.

ARCH-00D verification: the Windows development launcher starts the local FastAPI and React/Vite skeleton. Session bootstrap, backend liveness and the same-origin development proxy passed local smoke tests. The UI includes a backend connection status panel; visual browser acceptance is **BLOCKED** because the desktop automation native pipe was unavailable. G0 is not accepted.
