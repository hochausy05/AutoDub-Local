# AutoDub Local vNext — Project Root (Design + Repository Skeleton)

**Status:** Runnable local development app — Project persistence, durable Jobs/Attempts, artifact recovery and a MEDIA-01A source-import slice are implemented. G1 was accepted by the Product Owner; G2 remains unaccepted and Windows native picker/manual acceptance is pending.
**Target:** Windows 10/11, single-user webapp on `127.0.0.1`, all persistent Project data on local disk; **NO Supabase**.
**Purpose:** A user-controlled workflow to dub permitted video sources into Vietnamese: source → ASR → edit → Gemini translation → edit → Edge-TTS → measured sync → FFmpeg render.

## Product boundaries (approved direction)

- **MVP — Core Dubbing:** single Project at a time, selectable source/output paths, Faster-Whisper ASR, Gemini contextual translation + bulk edit, Edge-TTS voice/rate + per-Unit retry/playback, simple sync **table** with warnings, original-video-duration-preserving render, FFprobe-verified MP4. A small durable SQLite Job model is needed so edits, results and failures survive restart; **distributed-style lease/heartbeat is not required in MVP**.
- **V1 — Quality & Reliability:** optional vocal/source separation after evaluation, full Project backup/restore with checksum, enhanced crash recovery, batch queue and performance profiling.
- **V2 — Advanced Editing:** multitrack drag/drop timeline, local **video stretch/retime**, advanced video/audio/subtitle mapping with dedicated drift tests.
- Optional vocal separation is **not** a gate for first successful dubbing. Preserve original audio through MVP mute/gain/ducking options. Demucs or other separation models are candidates requiring quality/maintenance/hardware evaluation, not selected dependencies yet.
- Videos, translations, audio and renders stay local; **only Gemini and Edge-TTS send request data over Internet**.
- The user can configure each stage (source/folder, ASR profile, translation/editing, TTS voice/rate, simple synchronization, and render). No external cloud database/storage.

## Repository layout

```text
AutoDub-Local-vNext/
├── README.md, AGENTS.md, RULES.md, PLAN.md, TASK.md
├── CHANGELOG.md, UPDATES.md, ERROR_LOG.md, FILE_INDEX.md
├── .gitignore, .env.example
├── backend/             # FastAPI local session, SQLite Project and Job APIs/services
├── frontend/            # React + TypeScript Project UI and Vite proxy
├── scripts/             # Windows development launcher
├── tests/               # Stage fixtures and test guidance
├── docs/                # Detailed specifications and architecture reports
│   ├── 01_PRODUCT_REQUIREMENTS.md ... 14_SCOPE_BASELINE.md
│   └── reports/          # Editable Word and PDF design reports
├── adr/                 # Architecture decisions
└── assets/              # Proposed diagrams
```

## Run the development skeleton (Windows)

Prerequisites: Python 3.14 and Node.js 22.12+ (Vite 8 requirement); npm is included with Node. From the repository root:

```powershell
.\scripts\windows\dev.ps1
```

The launcher binds both services to `127.0.0.1`, creates/uses `backend/.venv`, installs pinned backend packages and runs `npm ci` when dependencies are absent. It opens the UI with a one-time bootstrap token in the URL fragment; the frontend clears the fragment before establishing the session. Use Ctrl+C in the launcher to stop. Run the actual local integration smoke suite with `.\scripts\windows\dev.ps1 -SmokeTest`. Direct backend tests: `Push-Location backend; .\.venv\Scripts\python.exe -m unittest discover -s tests -v; Pop-Location`; frontend production build/type check: `npm --prefix frontend run build`.

For Gemini translation, copy `.env.example` to the ignored root `.env`, enter `AUTODUB_GEMINI_API_KEY`, then restart the launcher. The backend also accepts a JSON list in `AUTODUB_GEMINI_CREDENTIALS_JSON`, with `alias`, `api_key`, `quota_group`, `priority`, and optional `enabled`; keep it local in the ignored `.env`. Existing single-key configuration stays supported. Credential aliases and auth-failure state are stored in SQLite, but key values are read only from the backend process environment and are never stored in SQLite, a Job, diagnostics, or frontend assets. A 401/403 can use one configured alternate key within the same bounded request budget; 429 does not rotate keys or bypass quota. This is not a remaining-quota estimator and does not promise that keys in different projects are billable/free. The `.env` file is plain text protected by the current Windows account. The UI asks for confirmation before sending dialogue text to Gemini. Do not put private source text or credentials into logs.

Project API/UI support local create/list/read, source-video COPY/LINK import, Faster-Whisper ASR, editable transcript revisions, and the TRANS-01A Gemini two-pass translation slice. Translation requires the current accepted transcript and sends text only after explicit consent. The result is persisted with exact source mapping and shown beside the original dialogue. SDK `google-genai==3.6.1` is pinned with explicit `v1beta` API selection and a single application retry owner. The planner groups deterministic chunks against conservative local input/output estimates instead of a fixed 10-Unit cap; these estimates are not provider token counts or hard API limits. Auth failure can use one configured alternate credential; 503 retries stay on the same credential and 429 never rotates keys. A synthetic one-Unit real-provider smoke completed both structured passes with two generation SDK calls before this optimization; Demo2 UI translation, model/API comparison, database restart after an optimized real run, and browser acceptance remain NOT RUN. G4 is not accepted. The SQLite file defaults to `%LOCALAPPDATA%\AutoDub-Local\autodub.sqlite3` on Windows, or to `AUTODUB_DATA_ROOT` when set to a local folder outside the source tree. WAL is enabled; UNC and mapped network drives are rejected. The frontend cannot choose a database path. Backend session cookies remain in memory and are reset when the backend restarts; use the launcher's new bootstrap link to establish a session again.

CORE-01B adds internal durable Job creation/claim/failure/completion/progress/retry/cancel operations. CORE-01C/D add atomic Artifact publication, startup reconciliation, typed stage generations and activation CAS. MEDIA-01A adds a local media import worker and session/CSRF guarded selection/import/media endpoints. Job input snapshots keep private source references out of API responses; errors and logs do not include the path. Native Windows dialog visual/manual checks remain required. Retry stays explicit and capped at three Attempts.

Project persistence checks use an isolated temporary database:

```powershell
Push-Location backend; .\.venv\Scripts\python.exe -m unittest discover -s tests -v; Pop-Location
npm --prefix frontend run build
```

Project and Media panels use real API data; no sample Projects are embedded. No HTTP Range playback, missing-source relink, provider, ASR or render implementation is included. G2 remains pending Windows manual acceptance and PO review.

The working media directory is **outside the repository**, chosen by the owner. Do not create or commit actual user data under the source tree.

## Reading order

1. `AGENTS.md` and `RULES.md` — Codex and engineering discipline.
2. `docs/14_SCOPE_BASELINE.md` — **scope precedence** for MVP/V1/V2; supersedes older feature suggestions.
3. `docs/01_PRODUCT_REQUIREMENTS.md` and `docs/08_IMPLEMENTATION_ROADMAP.md` — deliverables/gates.
4. `docs/02_SYSTEM_ARCHITECTURE.md` through `docs/13_RENDER_TIMELINE_SPEC.md` — detailed design references. Later-stage details there are **not MVP obligations**.
5. `PLAN.md` and `TASK.md` — planned work, all unchecked until actually tested and owner-accepted.
6. `docs/reports/` — Word and PDF design documents; `PROJECT_BLUEPRINT.md` is the corresponding technical Markdown source.

## Source inheritance

`video-dubbing-automation.zip` was **reviewed as a legacy reference**. Do not copy its `.env`, binaries, caches or user data into this repository. Reuse algorithms through isolated adaptations/tests only; see `docs/09_LEGACY_REUSE_AUDIT.md`.

## Implementation status / next action

The source import and ASR/transcript slices have automated coverage. TRANS-01A local orchestration is implemented and tested with a fake provider; the real Gemini API and browser flow remain unverified. TRANS-01B editing, TTS, SyncPlan and render are later stages; do not infer G4 acceptance from a successful build.
