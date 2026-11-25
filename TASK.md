# TASK — AutoDub Local vNext Implementation Queue

**Status:** Planned tasks may contain individually verified implementation slices; no stage or gate is owner-accepted unless stated. Consult `docs/14_SCOPE_BASELINE.md` before selecting scope. Tests and owner verification are separate.

## MVP

### ARCH-00 — Architecture and scaffold
- [ ] Create clean code repository on this skeleton; pin verified dependency versions and `.env.example`.
- [ ] Define Project, Run, Artifact, Unit, Config and Job schemas and file ownership.
- [ ] Establish Windows CPU/GPU probe plan and no-Supabase boundary.
- [ ] **G0:** Owner approves architecture and pared-down MVP.

### CORE-01 — SQLite + FastAPI
- [x] **CORE-01A Project persistence slice:** SQLite WAL/migration foundation, Project create/list/read API and UI; ten backend tests, frontend build and local launcher smoke pass (see CHANGELOG).
- [x] **CORE-01B durable Job/Attempt slice:** migration v2, internal lifecycle service, session-protected get/cancel/retry API; tested by 14 Job tests plus CORE-01A regression suite (see CHANGELOG).
- [x] **CORE-01C atomic artifact publication slice:** migration v3 manifest, validator-gated streaming publication, no-overwrite immutable paths, integrity recheck and sealed Job completion evidence; backend suite and crash-state tests pass (see CHANGELOG).
- [x] **CORE-01D startup recovery slice:** migration v4, idempotent Job/Artifact reconciliation, durable orphan inventory, typed stage activation CAS and Windows subprocess crash verification; G1 remains pending PO acceptance (see CHANGELOG).
- [ ] SQLite WAL/foreign keys/migrations, API errors, loopback-only API and local settings.
- [ ] Minimal single-machine durable Job/Attempt status, cancellation and startup reconciliation.
- [ ] Atomic file staging → validation → publish, no orphan files misreported as READY.
- [x] **G1:** Product Owner accepted after Windows restart/recovery verification (2026-11-23); G2 acceptance is recorded separately below.

### MEDIA-01 — Local media
- [x] **MEDIA-01A implementation + automated checks:** additive migration v5, Windows helper selection, durable COPY/LINK Import Job, FFprobe metadata and UI; 58 backend tests and frontend build pass. Manual desktop acceptance is BLOCKED; see CHANGELOG.
- [x] **MEDIA-01A-FIX:** FastAPI body model scope corrected; COPY/LINK API integration plus atomic Job/MediaAsset READY verified (60 backend tests); G2 was still pending at this slice and was accepted later as recorded below.
- [ ] **MEDIA-01A Windows manual acceptance:** native picker, real-user-video COPY/LINK, visible metadata and restart persistence — BLOCKED (desktop automation unavailable; launcher ports occupied).
- [ ] Copy/link source, choose local data/export paths; keep browser path selection safe.
- [ ] FFprobe metadata, HTTP Range player, Unicode paths, missing-source relink.
- [x] **MEDIA-01B automated implementation slice:** migration v6 relink contracts, guarded GET/HEAD Range playback, verified COPY/LINK source checks and same-content relink CAS; backend suite (64 tests), frontend build and helper parser pass. Browser/native picker manual acceptance was NOT RUN/BLOCKED at this slice; PO G2 acceptance was subsequently recorded below.
- [x] **G2:** Product Owner accepted (2026-11-23; per explicit ASR-01A handoff).

### ASR-01 — Faster-Whisper
- [x] **ASR-01A implementation slice:** real FFmpeg extraction, pinned Faster-Whisper adapter, model/device/VAD controls, durable immutable runs/segments and API/UI; 77 backend tests, frontend build and Windows launcher smoke pass. Real speech inference is NOT RUN; G3 remains pending PO acceptance (see CHANGELOG).
- [x] **ASR-01A-UX-REFINE automated slice:** persistent Project processing-source selection (migration v8), source/generation snapshots and stale activation guard, separated Play/source actions, simplified language/model-consent UI and backend CPU defaults; covered by backend tests and frontend build. Browser visual acceptance BLOCKED by unavailable native pipe; see CHANGELOG.
- [x] **ASR-01A-INFERENCE-FIX:** reproduced Faster-Whisper/PyAV incompatibility, pinned compatible PyAV, verified real CPU inference and SQLite restart readback; retry availability now comes from backend and terminal UI states hide active phase. Backend suite (84 tests) and frontend build pass; launcher/browser interaction blocked, G3 remains pending PO acceptance (see CHANGELOG).
- [x] FFmpeg source extraction, language/VAD/model/device controls and explicit CPU fallback policy covered by automated tests; real device inference measurements remain pending G3.
- [x] **ASR-01B transcript revision automated slice:** immutable ASR source, editable text drafts, split/merge lineage contracts, transactional acceptance and history; backend tests and frontend build pass. UX correction keeps timing read-only and hides split/merge controls; browser interaction acceptance remains NOT RUN (see CHANGELOG).
- [ ] **ASR-01B-UX-SIMPLIFY manual acceptance:** exercise edit/cancel/save/approve/re-edit/history/restart in Windows browser. Automated backend regression and frontend build pass; interaction test NOT RUN (see CHANGELOG).
- [x] **G3:** PO accepted (2026-11-23; explicitly specified as accepted by TRANS-01A task).

### TRANS-01 — Gemini
- [x] **TRANS-01A local automated slice:** persistent two-pass orchestration, strict source-ID mapping, immutable provider outputs, bounded retry/cancel handling, stage CAS, session/CSRF API and Vietnamese review table; see latest CHANGELOG for retry fix and validation.
- [x] **TRANS-01A-RETRY-FIX automated correction:** pinned SDK 3.6.1, transport regression confirms one physical HTTP request per SDK call and bounded adapter retry, 429 stop, cooldown, resume, redaction, and Bulk-First contracts; backend 120 tests, frontend build and Windows smoke PASS. Synthetic real Gemini two-pass smoke: 2 generation SDK calls, structured output/mapping PASS. Demo2 UI, browser, and restart after real provider run remain NOT RUN.
- TRANS-01A-ENGINE-OPTIMIZATION / ENGINE-COMPLETION (partial): compact wire IDs, order-independent strict mapping, pass-2 draft-aware subdivision, persisted provider-call provenance, quota-group cooldown, and explicit same-alias credential revalidation are implemented. Backend regression suite 132 tests and frontend build PASS. Free-Tier A/B: Generate Content completed an 8-Unit synthetic request with exact IDs and correct sampled meaning; Interactions returned HTTP 503. A 100-Unit synthetic Pass 1 completed after one 503; Pass 2 returned 503. The 100-Unit two-pass run, persisted real-provider restart, and UI/browser checks remain incomplete. No unverified model fallback or quota-remaining claim is enabled.
- TRANS-01A-UX-REFINE (partial): fixed the ASR/translation completion state race, consolidated transcript review into original/approved tabs, added revocable inline Gemini consent, and implemented provisional versioned Single-Pass with separate final output and terminal punctuation normalization. Backend suite **PASS** (136 tests), focused legacy compatibility test **PASS**, and frontend build **PASS**; Windows browser acceptance and live quality comparison are **NOT RUN**. The ADR remains proposed.
- TRANS-01B translation editor implementation slice: immutable provider output and accepted revisions, separate CAS draft, transactional Save All, current-run/active-revision checks, and two-tab UI. Focused revision/migration/session-CSRF tests pass; backend suite and frontend build results are recorded in the CHANGELOG. Browser acceptance NOT RUN.
- TTS-01A implementation slice: Edge-TTS adapter, accepted-revision batch snapshots, per-Unit verified MP3 artifacts, content-fingerprint reuse, bounded retry/concurrency, preview, cancellation, project-scoped Range playback and minimal consent UI. Synthetic integration tests pass; one real synthetic-text Edge-TTS adapter smoke produced verified non-silent MP3. Browser UI and 50-Unit mixed-outcome acceptance are NOT RUN; G5 remains unaccepted.

### TRANS-03A — Remove Gemini translation engine (implementation; owner acceptance unchecked)
- Removed Gemini execution while preserving Canonical Unit identity/mapping and historical Run/Unit reads. Retired unfinished Gemini/Cloudflare Jobs cannot retry; history and completed Jobs remain intact.
- [x] Implementation and focused history/recovery checks are recorded in CHANGELOG; Local Translation was implemented in the dependent LOCAL-01A slice below.
- [ ] Owner acceptance.

### LOCAL-01A — Implement Local Translation Engine (implementation; owner acceptance unchecked)
- [x] Added schema v24 Local Run support, durable 80/60 cursor and per-request history; migration verified with integrity and foreign-key checks.
- [x] Added atomic Local Job/Run/Unit creation and stage input update; Job idempotency and transaction rollback are covered.
- [x] Added pinned Vietnamese labeled-line prompt, response validation, stateless llama.cpp HTTP client, context fallback, timeout restart policy, cancellation checks and verified artifact publication path.
- [x] Added Local Translation status/create API, progress/cancel/retry UI and non-secret launcher settings; backend API and frontend build checks pass.
- [x] LOCAL-01B verified the installed Windows llama-server/model startup, inference and shutdown; details are in the LOCAL-01B CHANGELOG entry.
- [x] Replaced tests that only exercised retired Gemini execution; retained editor revision/CAS, migration and TTS fixture coverage.
- [ ] Owner acceptance.

### LOCAL-01B — Local Translation real-world validation (implementation verification; owner acceptance unchecked)
- [x] Real Windows inference through the application API with 160 Unicode-verified synthetic Chinese Units, two sequential default batches of 80 and verified artifact publication.
- [x] Unit identity, source mapping, positions, timestamps, output completeness and Han-character residuals checked; Editor, multi-Unit Save All, Approval and restart readback verified.
- [x] Runtime cancellation checked during inference; Job/Run/Request became CANCELLED, cursor stayed at zero and the owned runtime listener closed.
- [x] A runtime version probe defect was fixed: this installed llama.cpp build reports its version on stderr. Focused regression tests pass; measured results are in CHANGELOG.
- [ ] Owner acceptance.

### TRANS-02C - Gemini-only cleanup (pending PO review)
- PO retired Cloudflare; new Translation Runs use Gemini only. Historical provenance remains readable and unfinished Cloudflare Jobs are retired during recovery. TRANS-02A/B were not accepted. G4 was previously accepted; G5 remains unaccepted. Local AI is a separate future task.

### TRANS-02A — Cloudflare default and manual provider selection (in progress; not accepted)
- Implemented provider selection and REST adapter for GLM-4.7-Flash with separate provider consent/status, immutable Run metadata, provenance migration and legacy Gemini compatibility. TRANS-02A's original smoke was NOT RUN; later TRANS-02A diagnostic and TRANS-02B qualification activity is recorded in the newest CHANGELOG. Live translation qualification did not pass exact mapping/parse; no optimization is enabled. TRANS-02A remains unaccepted.
- TRANS-02B partial implementation: added provider-specific execution identity/adapter dispatch, corrected the observed Cloudflare REST response parser, and added additive migration v23 for immutable strategy identity plus redacted per-request timeout/payload diagnostics. Gemini request behavior and common downstream pipeline remain unchanged. Focused translation tests PASS (51); full backend regression and additional integration review are pending. Synthetic Cloudflare qualification: Compact Text 40/48 coverage with extra duration fields; prompt-only JSON parse failed. Thinking effect and production-equivalent 6,000-token behavior remain unverified. No G5/TRANS-02B acceptance is claimed.
- [ ] Two-pass context-aware, lossless source-unit mappings and explicit missing-content review.
- [ ] Edit multiple translations, save once, preserve drafts and revisions.
- [x] **G4:** PO accepted (2026-11-24; explicitly confirmed by the TTS-01A task).

### TTS-01 — Edge-TTS
- [ ] Voice/rate preview, bounded concurrency and error diagnostics.
- [ ] Publish genuine verified audio per Unit immediately, allow play while other Units run.
- [ ] Reuse unchanged Unit artifacts, never silently truncate or insert fake silence.
- [x] **TTS-01B provider qualification run:** real direct/adapter calls, bounded concurrency comparison, selective delayed recovery and isolated Preview API pipeline are recorded in CHANGELOG. Matrix stopped safely after three consecutive `NoAudioReceived`; Tests E/F remain NOT RUN. No production code or dependency changes.
- [x] **TTS-01C reliability/control slice:** migration v25 adds content-free per-call diagnostics; batch Job/stage/Run/Unit creation is transactional with compact hashed snapshots; synthesis uses global cancellable pacing and up to three separated rounds, preserves ready Units, records recovery, and gates retry eligibility. Preview shares pacing/retry policy; UI syncs active preview and locks settings during work. Focused backend regressions and frontend build recorded in CHANGELOG; G5 remains pending.
- [ ] **G5:** Mixed 50-Unit case, retry/cancel/replay and audio identity tests.

### SYNC-01 — Simple sync table
- [ ] Measure each audio Unit, detect overlap/overflow/conflict, show numeric start/offset/gap.
- [ ] Limited rate/translation edit/gap adjustment; preserve source video duration.
- [ ] No multitrack drag/drop; no video stretch; create verified immutable sync plan.
- [ ] **G6:** Conflicts explicit and no drift in unchanged video timeline.

### RENDER-01 — Local MP4
- [ ] Render config including source mute/gain/ducking, TTS mix, subtitle options and encoder.
- [ ] FFmpeg temp → FFprobe validate → new immutable output file; safe GPU/CPU fallback.
- [ ] Short preview range after basic full render works.
- [ ] **G7:** Real 9:16/16:9 MP4 output and failure recovery checked.

### MVP-QA-01 — Final MVP acceptance
- [ ] Real end-to-end dubbing on Windows, reopen Project and preserve edits/artifacts.
- [ ] Confirm never deletes user source/final MP4 automatically; truthful logs and privacy note.
- [ ] **G8:** Owner accepts working MVP; compilation only is not acceptance.

## V1 — Quality & reliability (not an MVP blocker)
- [ ] **V1-SEP-01:** Evaluate optional Vocal Separation model on dialogue+music/SFX and hardware constraints.
- [ ] **V1-OPS-01:** Portable checksum-based Project backup/restore and restore-in-different-folder test.
- [ ] **V1-OPS-02:** More robust Job recovery, conditional lease/heartbeat, batch scheduling and profiling.

## V2 — Advanced editing (not an MVP blocker)
- [ ] **V2-TIMELINE-01:** Multitrack drag/drop timeline with waveform and virtualized tracks.
- [ ] **V2-RETIME-01:** Explicit video stretch with full time-map propagation, VFR and drift tests.
