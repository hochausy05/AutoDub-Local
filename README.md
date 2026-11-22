# AutoDub Local vNext — Project Root (Design + Repository Skeleton)

**Status:** DESIGN / SCAFFOLD ONLY — no runnable implementation has been delivered, and no runtime tests have passed for this new repository.
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
├── backend/             # FastAPI, domain/storage/worker adapters (empty skeleton)
├── frontend/            # React + TypeScript + Vite (empty skeleton)
├── scripts/             # Windows bootstrap and diagnostics (not implemented)
├── tests/               # Fixtures / e2e test skeleton
├── docs/                # Detailed specifications and architecture reports
│   ├── 01_PRODUCT_REQUIREMENTS.md ... 14_SCOPE_BASELINE.md
│   └── reports/          # Editable Word and PDF design reports
├── adr/                 # Architecture decisions
└── assets/              # Proposed diagrams
```

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

This ZIP contains specifications and a **real project-root directory skeleton**, not app code. Begin at `ARCH-00` after owner approval. Do not invent runtime completion, benchmarks, or hardware capability.
