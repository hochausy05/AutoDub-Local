# AutoDub Local vNext — Project Root (Design + Repository Skeleton)

**Status:** Runnable local development app — Project persistence, durable Jobs/Attempts, artifact recovery, source import/playback, ASR/transcript, Gemini translation/editor and the TTS-01A Edge-TTS slice are implemented. G1 was accepted by the Product Owner; G2 and G5 remain unaccepted. G4 was previously accepted by the PO. TRANS-02A/B were not accepted; Cloudflare was retired by PO decision.
**Target:** Windows 10/11, single-user webapp on `127.0.0.1`, all persistent Project data on local disk; **NO Supabase**.
**Purpose:** A user-controlled workflow to dub permitted video sources into Vietnamese: source → ASR → edit → translation (Gemini) → edit → Edge-TTS → measured sync → FFmpeg render.

## Product boundaries (approved direction)

- **MVP — Core Dubbing:** single Project at a time, selectable source/output paths, Faster-Whisper ASR, Gemini translation + bulk edit, Edge-TTS voice/rate + per-Unit retry/playback, simple sync **table** with warnings, original-video-duration-preserving render, FFprobe-verified MP4. A small durable SQLite Job model is needed so edits, results and failures survive restart; **distributed-style lease/heartbeat is not required in MVP**.
- **V1 — Quality & Reliability:** optional vocal/source separation after evaluation, full Project backup/restore with checksum, enhanced crash recovery, batch queue and performance profiling.
- **V2 — Advanced Editing:** multitrack drag/drop timeline, local **video stretch/retime**, advanced video/audio/subtitle mapping with dedicated drift tests.
- Optional vocal separation is **not** a gate for first successful dubbing. Preserve original audio through MVP mute/gain/ducking options. Demucs or other separation models are candidates requiring quality/maintenance/hardware evaluation, not selected dependencies yet.
- Videos, translations, audio and renders stay local; Google Gemini and Edge-TTS send only their consented request text over Internet.
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

Configure `AUTODUB_GEMINI_API_KEY` or `AUTODUB_GEMINI_CREDENTIALS_JSON` in the ignored root `.env`. Gemini consent is required for every new translation Job. Existing Cloudflare Runs remain readable; unfinished Jobs are retired without sending requests.

Edge-TTS uses the pinned `edge-tts==7.3.1` adapter and requires an Internet connection; it does not require a user account or API key. Open an accepted Translation's **Bản đã duyệt** tab, review the separate Edge-TTS consent, choose an available Vietnamese voice/rate, and explicitly click **Nghe thử** or **Tạo giọng đọc**. One batch action handles all eligible Units; each published Unit can be played before the rest finish. Audio and source media remain in local DataRoot. The implementation uses 24 kHz mono MP3 and a bounded provider concurrency of 3. Voice preview and synthesis send only the selected sample text or accepted Vietnamese text to Edge-TTS. Revoke consent from the same panel to disable future sends.

Project API/UI support local create/list/read, source-video import, ASR, transcript and Gemini translation editing. New Runs use Single-Pass with compact IDs and duration-aware payloads. SQLite defaults to `%LOCALAPPDATA%` on Windows or a configured local `AUTODUB_DATA_ROOT`; WAL and localhost session protections apply.

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

The source import and ASR/transcript slices have automated coverage. TRANS-01A orchestration is locally tested; Gemini semantic quality and browser acceptance remain unverified. TRANS-01B provides the translation editor. TTS-01A now has mocked integration coverage plus one real synthetic Edge-TTS adapter smoke; its 50-Unit G5 mixed-outcome acceptance and Windows browser interaction remain unverified. SyncPlan and render are later stages.
