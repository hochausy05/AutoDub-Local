# 14 — Accepted Scope Baseline: MVP / V1 / V2

**Status:** Product-scope decision for AutoDub Local vNext; no implementation done. This document governs feature priority if older technical specs suggest implementing advanced features earlier. Owner acceptance of the design and subsequent code is still pending.

## Why scope was reset

The previous blueprint put a distributed-style SQLite lease/heartbeat system, complete portable backup/restore with checksums, multitrack drag/drop editing and local video retime too close to the first deliverable. This creates unnecessary UI, scheduling and time-mapping complexity before the core dubbing path exists.

## MVP — Core Dubbing (must work end-to-end)

1. One active local Project, file import, configurable data/output folder, FFprobe metadata and source-video player.
2. ASR with Faster-Whisper; model/language/device selection; GPU use only after capability probe and CPU fallback.
3. Gemini two-pass contextual translation, conservative grouping, structured mapping and bulk manual edits.
4. Edge-TTS voice/rate preview; bounded retries; generated per-Unit artifact playable as soon as verified; no silent failure substitution; regenerate changed Units only where fingerprints match.
5. **Simple timing editor as a table**, not multitrack UI: start_us, source window, real TTS duration, allowed shift/gap, overlap/conflict, manual numeric override and seek to video. Limit safe strategies to text reduction/explicit revoice + rate adjustment + bounded gap/offset shift **while source video duration is unchanged**. Conflicts require user action; no automatic video stretch.
6. Render original-duration MP4 with configurable original-audio mute/gain/ducking, narration gain, optional basic subtitles, output path, quality/encoder and FFprobe validation. Short-range render preview is a desirable MVP deliverable after the first successful full render, not a prerequisite for the first end-to-end gate.
7. SQLite persistent state, minimal single-host Job/Attempt states + cancellation and interruption recovery; atomic artifact publication. No complex lease/heartbeat or multi-host queue until evidence requires it. Safe startup recovery must never mislabel partial work READY.
8. Basic cleanup for temporary files and user confirmation for destructive actions. Keep original and verified outputs. **No complete backup/restore pipeline required in MVP**.

## V1 — Quality & Reliability

- **Optional Vocal Separation** adapter evaluated on actual dialogue-plus-music/effects: compare clean reference/quality/artifacts, CPU/GPU cost, memory and licensing. Candidate: Demucs or another dialogue-oriented separation model. This is NOT a dependency for MVP and not assumed perfect.
- Portable full Project backup/restore with SQLite consistent snapshot, manifest, checksums and actual restore test.
- Robust restart/retry policies and lease/heartbeat only if a real multi-process claim race or orphan-job condition requires it; enhanced provenance and resource scheduler.
- Batch Project queue and concurrency policies; advanced monitoring/profiling; render preview UI refinement; richer silence/waveform indicators as justified.

## V2 — Advanced Editing

- Multitrack interactive timeline with drag/resize, waveform and source/TTS/subtitle lanes, undo and scale/virtualization.
- Video stretch / local retime **only behind an explicit advanced mode** and a canonical time-remap function applied to source video, narration, subtitles, background audio and all downstream events; dedicated tests for VFR, rounding and cumulative drift.
- Detailed effect/keyframe editing and advanced interactive mixing.

## Important constraints

- No Supabase, no cloud backup/sync, no login/multi-user server.
- Gemini and Edge-TTS need network access; local-first refers to persistence and media processing, not offline provider inference.
- On ordinary Windows local disk, SQLite WAL with busy_timeout and single-writer semantics is sufficient for the initial single-machine MVP. Avoid long-lived database transactions during inference, synthesis or rendering.
- Keep ASR/Translation/TTS immutable Run outputs and mutable saved drafts separated; artifacts must have input/config fingerprint.
- Do not trim/silence text or audio to make a failing unit appear successful. Every audio Unit must have a real file and measured duration.
- Do not require direct file path from browser input; native picker or loopback copy must be implemented safely.
- No implied performance numbers or guarantees without real hardware measurements.

## Acceptance gates

**MVP Gate:** One real source video completes import → ASR → translation review → Edge-TTS (listen to early Unit) → simple sync conflict review → validated MP4; restart preserves saved edits and completed outputs. GPU/CPU behavior is honestly reported. No Stage can claim pass on build success alone.

**V1 Gate:** Selected optional separation cases objectively compared to baseline, backup/restore exercised across directories, multi-Project recovery and failure cases tested.

**V2 Gate:** Interactive timeline and video time-remap tested for continuous synchronized media under VFR, overlapping dialogue and long durations, including no cumulative drift.
