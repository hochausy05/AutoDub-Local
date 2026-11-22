# PLAN — AutoDub Local vNext

**State:** New repository scaffold, design-only; implementation and owner acceptance pending.  
**Canonical scope:** `docs/14_SCOPE_BASELINE.md` (MVP/V1/V2).  
**Detailed roadmap:** `docs/08_IMPLEMENTATION_ROADMAP.md`.

## Product outcome

A Windows loopback webapp for creator-controlled video dubbing using local SQLite/media; ASR Faster-Whisper; Gemini two-pass translation; Edge-TTS; reliable per-Unit preview; a simple conflict-table synchronizer; FFmpeg render and FFprobe verification. The user chooses source/output paths, model, translation edits, voice/rate and render parameters. No Supabase.

## MVP stages

| Stage | Deliverable | Gate |
|---|---|---|
| ARCH-00 | Repo skeleton, contracts, scope and QA plan | G0 |
| CORE-01 | SQLite + minimal durable local Job lifecycle + FastAPI | G1 |
| MEDIA-01 | Video import/choose folders/player/FFprobe | G2 |
| ASR-01 | Faster-Whisper transcript + correction | G3 |
| TRANS-01 | Two-pass Gemini translation + bulk edits | G4 |
| TTS-01 | Edge-TTS per-Unit synthesis + early listen/retry | G5 |
| SYNC-01 | Numeric sync/conflict table, **no video stretch** | G6 |
| RENDER-01 | Configurable local MP4 render + FFprobe verification | G7 |
| MVP-QA-01 | Cross-stage regression + owner E2E acceptance | G8 |

## Later stages

- **V1-SEP-01:** Optional Vocal Separation proof of concept, quality/hardware tests.
- **V1-OPS-01:** Full checksum backup/restore, robust job recovery, batch queue, profiling.
- **V2-TIMELINE-01:** Interactive multitrack timeline.
- **V2-RETIME-01:** Local video stretch with canonical remap and drift tests.

No V1/V2 item blocks the first verified MP4 except a critical safety defect.

## Principles

One focused Codex task at a time, smallest working vertical slice, tests with real media, honest status. Preserve approved edits and source files. Implement only the hardware acceleration actually supported by the host after probing.
