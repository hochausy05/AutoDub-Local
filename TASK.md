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
- [x] **G1:** Product Owner accepted after Windows restart/recovery verification (2026-11-23); this does not imply G2 acceptance.

### MEDIA-01 — Local media
- [x] **MEDIA-01A implementation + automated checks:** additive migration v5, Windows helper selection, durable COPY/LINK Import Job, FFprobe metadata and UI; 58 backend tests and frontend build pass. Manual desktop acceptance is BLOCKED; see CHANGELOG.
- [x] **MEDIA-01A-FIX:** FastAPI body model scope corrected; COPY/LINK API integration plus atomic Job/MediaAsset READY verified (60 backend tests); G2 remains unaccepted.
- [ ] **MEDIA-01A Windows manual acceptance:** native picker, real-user-video COPY/LINK, visible metadata and restart persistence — BLOCKED (desktop automation unavailable; launcher ports occupied).
- [ ] Copy/link source, choose local data/export paths; keep browser path selection safe.
- [ ] FFprobe metadata, HTTP Range player, Unicode paths, missing-source relink.
- [x] **MEDIA-01B automated implementation slice:** migration v6 relink contracts, guarded GET/HEAD Range playback, verified COPY/LINK source checks and same-content relink CAS; backend suite (64 tests), frontend build and helper parser pass. Browser/native picker/manual G2 acceptance remains NOT RUN/BLOCKED; see CHANGELOG.
- [ ] **G2:** Play/source-relink and safe delete verified.

### ASR-01 — Faster-Whisper
- [ ] Source extraction, language/VAD/model/device controls and measured hardware fallback.
- [ ] Segment timestamps and correction review; no fake transcript.
- [ ] **G3:** Real media/no-audio/VFR handling tested.

### TRANS-01 — Gemini
- [ ] Two-pass context-aware, lossless source-unit mappings and explicit missing-content review.
- [ ] Edit multiple translations, save once, preserve drafts and revisions.
- [ ] **G4:** Output coverage, retries/quota errors, edit protection checked.

### TTS-01 — Edge-TTS
- [ ] Voice/rate preview, bounded concurrency and error diagnostics.
- [ ] Publish genuine verified audio per Unit immediately, allow play while other Units run.
- [ ] Reuse unchanged Unit artifacts, never silently truncate or insert fake silence.
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
