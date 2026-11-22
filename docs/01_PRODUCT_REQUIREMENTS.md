# 01 — Product Requirements (PRD)

**Status:** Proposed design, not implemented. **Owner:** Windows solo video creator/editor. **Interface:** Vietnamese. **Canonical scope:** `docs/14_SCOPE_BASELINE.md`.

## Product goal

Provide a configurable local-only dubbing webapp: select video/paths → ASR → inspect/correct → Gemini contextual translation → bulk edit → Edge-TTS voice/speed and per-Unit playback → conflict-aware numeric sync → FFmpeg/FFprobe MP4. A user can pause, review, adjust and rerun only affected stages. All media and structured work data stay on the user's disk.

## Product principles

- Fast iterative feedback and **no external media upload**; only Gemini and Edge-TTS need Internet.
- The user chooses storage, ASR model/device/language, translation style, voice/rate, sync overrides and render configuration.
- Never silently truncate text, replace missing speech with silence, mutate original media, or claim a Stage passed without evidence.
- Immutable, measured artifacts with input/config fingerprints. Preserve saved edits and verified output.
- First reach an accepted complete MP4, then add advanced quality/editing features.

## Scope matrix

| ID | Capability | Release | Acceptance example |
|---|---|---|---|
| F01 | Project Library, video import/copy/link, paths, local source playback | MVP | Opens after restart, no cloud upload |
| F02 | FFprobe metadata, orientation, source seek and safe deletion | MVP | Portrait/landscape/Unicode source works |
| F03 | ASR language/model/VAD/GPU-or-CPU controls and correction review | MVP | Real transcript with stable timestamps |
| F04 | Gemini two-pass translation, context/glossary and complete mappings | MVP | Missing source ID detected |
| F05 | Side-by-side translation bulk editor, save/correction history | MVP | Edited content survives restart |
| F06 | Edge-TTS voice/rate/preview, per-Unit retries and valid playable audio | MVP | First Unit playable while others generate |
| F07 | Selective regeneration using verified per-Unit fingerprints | MVP | One edit doesn't rerun unrelated units |
| F08 | Simple sync table with measured duration/offset/gap/conflicts | MVP | Overlap shown, video duration unchanged |
| F09 | Configurable original mute/gain/ducking + voice/subtitle render | MVP | FFprobe-verified MP4 |
| F10 | SQLite minimal durable Job state, cancellation and safe restart | MVP | No false READY after interruption |
| F11 | Optional short-range render preview | MVP after first working full render | No changes to final file |
| F12 | Optional Vocal Separation and audio quality comparison | V1 | Real vocal/music test with documented artifacts |
| F13 | Portable checksum-based backup/restore and richer Job recovery | V1 | Restore to another folder verified |
| F14 | Batch queue and advanced resource scheduling | V1 | One failed Project does not kill others |
| F15 | Multitrack drag/drop timeline and waveform | V2 | Smooth large-track interaction |
| F16 | Local video stretch and cross-track canonical retime | V2 | No cumulative A/V/subtitle drift |

## MVP out of scope

No Supabase, account login, remote media upload/sync, full DAW-style timeline, automatic video stretching, guaranteed source-vocal isolation, complex distributed heartbeat/lease infrastructure, or mandatory full Project backup. Basic integrity/crash safety is still required.

## User journey

1. Create a local Project and select video, target folder/output folder, Copy/Link mode.
2. Inspect FFprobe metadata; select ASR model/language/device/VAD and run.
3. Review/edit transcript against source seek controls.
4. Translate with Gemini, inspect Pass1/Pass2 outputs and bulk-edit final Vietnamese text.
5. Choose Edge voice/rate, preview, synthesize per Unit; listen to finished Unit immediately and correct/retry selected Unit.
6. Review timing conflict **table** with fixed original video timeline; adjust rate/text/start/gap within safe bounds.
7. Configure source sound mix, subtitles, codec/resolution and output path; render and verify MP4.
8. Close/reopen Project and confirm edits and verified artifacts remain intact.

## Release success measures

- End-to-end original video → Vietnamese MP4 completed with actual quality review.
- Audio playable per Unit before all units finish; invalid/missing Units never reported READY.
- No data loss for accepted edits/verified files after ordinary restart.
- Measured time-to-first-audio and encode performance reported; no fabricated 'maximum GPU' claims.
- CPU fallback explicit if GPU unavailable; basic UX functional on one Windows machine.

## Future quality criteria

V1 can add optional separation where demonstrably useful; it may fail on some videos and must be bypassable. V2 may retime video only after a single piecewise source→output time transform is applied to all tracks and tested on VFR/overlap/long duration media.
