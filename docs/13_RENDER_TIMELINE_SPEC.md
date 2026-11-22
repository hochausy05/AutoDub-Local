# 13 — Sync and Render Specification (MVP / V1 / V2)

**Scope authority:** `docs/14_SCOPE_BASELINE.md`. This file intentionally describes *future* multitrack/retime mechanics but does **not** make them MVP requirements.

## 1. Coordinates and measurement

- Source Segment `[start_us,end_us)` refers to original video presentation timeline, in **integer microseconds**.
- Synthesized audio has measured `sample_rate`, `sample_count`, `duration_us`, actual file/hash, provider, voice and rate.
- UI displays floats only for readability. FFmpeg PTS/video frame mapping must respect rational time bases/VFR.
- No timing inferred solely from character counts when an actual WAV/MP3 artifact is available.

## 2. MVP sync strategy — preserve video duration

Input for Unit `i`: source window `[s_i,e_i)`, measured audio duration `d_i`, optional manual shift `delta_i`, allowed gap tolerance, global/unit voice rate and available free gap.

A table row shows: Unit ID, source time, source duration, TTS duration, target onset, overflow/overlap, proposed action and user decision.

Allowed proposals:

1. Shorten/edit Vietnamese text (requires approval and real resynthesis).
2. Adjust Edge-TTS speaking rate within documented/intelligibility bounds, synthesize candidate and measure real result.
3. Shift onset into a **bounded empty gap** while preserving order and other Unit limits.
4. Leave unresolved as an explicit conflict; prevent a supposedly validated plan from hiding the conflict.

Do **not** stretch video. Do **not** change global source/target duration. Do **not** automatically truncate audio. Overlap can be accepted only via an explicitly defined policy with warning and listening preview.

A SyncPlan is immutable and references exact input video, translation revisions and TTS artifact hashes; editing it creates a new version. The start/end placement is checked before render.

## 3. MVP rendering and audio mix

- Preserve original video duration, orientation and aspect ratio unless explicitly configured safe crop/fit/pad/resize.
- Source soundtrack options: `mute`, fixed gain, `duck` (reduce source sound under narration). These **also affect music/effects**, so explain limitations.
- Overlay narration at planned sample positions; use short fades near boundaries to prevent clicks; detect clipping and preserve measured output duration.
- Optional basic subtitle render: off, external SRT, burn-in ASS with escaped Unicode and user-configured style/position. Keep timestamps on original fixed source timeline.
- FFmpeg `filter_complex_script` for long Windows-safe graphs; CPU x264 and probed NVIDIA NVENC supported where actually available. User selects resolution, bitrate/quality, FPS policy and output folder.
- Verify the temporary MP4 with FFprobe and a decode/playback smoke test; publish immutable verified output by atomic move. Previous good output remains.
- A short preview-range render is introduced **after** first validated full render pipeline, not a blocker for that first pipeline.

## 4. V1 optional Vocal Separation

Goal: preserve more non-speech background while reducing source dialogue. A separation adapter is **optional**, never auto-required.

Evaluate Demucs or a dialogue-targeted separator on real video samples; compare vocal leakage, background music/SFX damage, latency, CPU/GPU/VRAM requirements, license and maintenance. Store stems as local optional artifacts with input/model/config fingerprint. Provide baseline mute/ducking controls and A/B audio preview. Avoid claims of perfect separation or guaranteed quality improvement.

## 5. V2 multitrack Timeline Editor

Drag/resize/seek across video, original vocal, ambience, dubbed Unit and subtitle lanes. Must be virtualized and preserve unit time coordinates, undo/redo and all constraints. No drag/drop requirement in MVP.

### V2 video retime / stretch

A piecewise monotone time-remap `T(t)` maps source PTS to output PTS. If any region is lengthened, every downstream source event, subtitle, audio background, narration and render duration must be remapped through the same function. Respect VFR PTS and rational frame conversions, ensure no negative/overlapping invalid intervals; test cumulative drift and boundary continuity. Video stretching is never silently enabled. The editor must show the consequences before approval.

## 6. Render QA examples

- 1.8-second window and 2.9-second TTS: explicit conflict requiring text/rate/gap handling, not automatic video retime.
- Two overlapping original speakers: show overlap and selected manual policy.
- Portrait clip, landscape clip, VFR source and Unicode file path: test A/V length and player compatibility.
- Audio clipping or failed FFmpeg: no verified render publication.
