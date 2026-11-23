# AutoDub Local vNext — Product Updates

No dubbing pipeline feature has been released. The local application now supports persistent Project create/list/read through the session- and CSRF-protected API. Projects are stored in SQLite under the Windows local application data folder by default; the UI reads real API data and does not seed sample Projects. Backend tests, frontend build and Windows launcher integration smoke passed for this slice.

ARCH-00D verification: the Windows development launcher starts the local FastAPI and React/Vite skeleton. Session bootstrap, backend liveness and the same-origin development proxy passed local smoke tests. The UI includes a backend connection status panel; visual browser acceptance is **BLOCKED** because the desktop automation native pipe was unavailable. G0 is not accepted.
