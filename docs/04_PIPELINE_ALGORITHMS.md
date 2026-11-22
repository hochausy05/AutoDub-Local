# 04 — Pipeline Algorithms, Validation & Incremental Processing

> **Phase rule:** `14_SCOPE_BASELINE.md` defines mandatory MVP vs V1/V2 scope; advanced designs in this document do not imply MVP implementation.

**Rule:** Time represented as integer microseconds (`*_us`) for timeline operations; audio sample/frame counts where possible. Floats only at API boundaries and diagnostic displays. Algorithms below are design targets; none are runtime-verified in the new app.

## 1. Source intake and media probing

```text
Choose source -> explicit Copy or Link -> file type/size/safety check
-> FFprobe streams/duration/frame rate/aspect/rotation/audio
-> SHA-256 fingerprint + Project source asset -> READY
```

For copied source: stream in chunks to local temp, fsync where needed, checksum and atomic publish. For linked source: never modify source; relink if missing; hash on demand with progress for large files. Show whether frame rate is VFR; do not round source fps to two decimals blindly. Estimate required disk space before extraction/render.

## 2. ASR — speech to stable Segments

**Inputs:** verified source, source language, ASR model, beam, VAD config, prompt/vocabulary, device profile.  
**Algorithm:**

1. FFmpeg extracts mono 16 kHz PCM/resamples accurately (`-vn -ac 1 -ar 16000`), or stream decode with bounded memory.
2. Probe GPU; choose Faster-Whisper/CTranslate2 device and compute type. Warm model inside Worker; on genuine CUDA error, log classification and retry once on CPU if allowed.
3. Run VAD if selected, use timestamps relative to original audio; map segment times to original media PTS, not simply contiguous VAD chunks.
4. Transcribe with bounded worker; iterate generator fully; validate ordered segments and finite timestamps. Store model/device/latency.
5. Detect likely hallucination (long repeated patterns, low/no speech, compression anomalies) as `REVIEW_REQUIRED`; **do not erase text silently**.
6. Save immutable ASR Run; user may edit transcript using draft layer; accepted run ID remains unchanged.

**Important:** Legacy `_GLOBAL_WHISPER_MODEL` is a useful cache concept but must be adapted to a supervised process, not a module-global variable inside UI. Device/beam/batch must be benchmarked against VRAM.

## 3. Segment normalization and translation grouping

Goal: maintain accurate source mapping while translating natural thoughts, not every tiny ASR fragment in isolation.

- Sort by `start_us`, `position`, stable ID; reject invalid time spans.
- Build candidate groups based on sentence boundaries, punctuation, gap <= configured threshold, topic continuity and max group duration/characters; grouping is explicitly configurable.
- The user can merge/split within limits, and each group stores `source_segment_ids` and temporal envelope.
- **Coverage invariant:** every included source segment maps to exactly one output group or is explicitly marked SKIPPED with reason; no lost IDs.
- Do not hard-code `3 segments max` unless benchmark and product requirements justify it; preserve a guard against unbounded groups.

Pseudocode:

```python
for source in verified_segments:
    assert source.end_us > source.start_us
    if can_append(group, source, gap_limit_us, duration_limit_us, text_limit):
        group.append(source)
    else:
        flush(group); group = [source]
flush(group)
assert covered_source_ids(groups) == expected_ids
```

Grouping is translation input management, not a guarantee of natural semantics. User review stays available.

## 4. Gemini two-pass translation

**Pass 1 (context):** Translate grouped source into Vietnamese with whole-video summary/context, genre/glossary, pronoun consistency, stable IDs. Return schema-validated JSON. Preserve numbers/names/negation; no automatic "creative" insertion.

**Pass 2 (dubbing optimization):** Rewrite Pass 1 for natural spoken Vietnamese and target duration; preserve meaning, source IDs, named entities, pronunciation hints. Avoid padding words/automatic terminal periods that are not needed. Return exact schema.

**Long input:** token-estimate -> context windows with overlap and carried glossary/summary -> output segments -> reconcile; never drop groups exceeding context. Pass 2 runs per bounded chunk after Pass 1 validated. Retry bounded transient errors only; quota exhausted returns `WAITING_FOR_QUOTA`, not fake success.

**Validation:** exact ID set, no extra IDs, no missing/empty output unless explicitly approved, no impossible timestamps, Unicode valid, no invalid content type, drift/character ratio warnings, protected manual revision compare-and-swap. Do not use regex greedily to extract arbitrary JSON as trust boundary; prefer structured generation where available and strict parser.

**Edit UI:** source original, Pass 1, Pass 2, final text; Save All; role/style glossary; regenerate selected only; editorial approval needed for missing translations.

## 5. Edge-TTS — per-Unit synthesis

**Inputs:** accepted translation revision, chosen voice (default `vi-VN-NamMinhNeural` from legacy), rate%, pitch/volume if supported, output format, provider version. Edge-TTS requires Internet and is not under the same local CPU/GPU performance optimization as ASR/FFmpeg.

1. Compute artifact key from `unit_id + text hash + voice + rate/pitch/volume + provider/version + synthesis version`.
2. Reuse valid matching cached artifact. If not, create TTS Job for that Unit; use bounded async semaphore (start 2-3, tune based on real error/network data, not automatically 5).
3. Sanitize only unsupported control characters. **Never shorten, truncate or rewrite the user's accepted text** behind their back.
4. If Edge length/format limit is encountered, split by sentence/word boundaries into all subchunks with retained exact concatenated text and source mapping; synthesize all; concatenate with measured pauses. If impossible, return REVIEW_REQUIRED.
5. Download to temp file, verify codec/duration, decode sample, inspect non-silence when non-empty text, sha256 and atomic publish; mark Unit READY independently.
6. Retry transient network/429/5xx with jitter and caps; fail with a clear error when attempts exhausted. No silent WAV substitution for failed speech.
7. Expose voice preview cache with exact config, asynchronous cancellation, bounded memory/disk.
8. A Unit can be played while other Units generate. TTS Run READY only after all required Units READY and still matching latest revision.

**Rate fitting:** optional `target_rate = clamp(base_rate * (measured_duration / target_window), allowed_min, allowed_max)` is only a first estimate. Rate/duration can be nonlinear; verify after synthesis, maybe one bounded adjustment, never infinite retries. Explain remaining overflow to user.

## 6. Incremental invalidation

For any changed Unit:

```text
new Unit revision -> compute input fingerprint
same fingerprint + existing verified artifact? reuse
otherwise queue only affected Unit -> produce new artifact
then rebuild sync plan (cheap) -> invalidate render output only
```

Changes in voice/rate globally affect all Units by design; per-Unit overrides only affect selected Units. No delete-before-replace; old artifacts remain referenced until a new Run is explicitly accepted.

## 7. AV synchronization constraints

Define each Unit `i`: source window `[s_i, e_i)`, speech duration `d_i`, selected audio-rate `r_i`, start shift `Δ_i`, V2-only optional local video stretch `v_i`; target window `w_i = e_i - s_i`.  
`overflow_i = max(0, d_i - w_i - allowed_tail_i)`.

Possible responses in preference order selected by user:

1. **Rewrite text** (back to Translation, with review) if meaning can be preserved.
2. **Adjust Edge rate** within user allowed range and remeasure.
3. **Use gaps/shift start** if no conflict with adjacent speech and within timing tolerance.
4. **V2 only — Local video retime** bounded (requires rebuilding video/audio/subtitle timeline; excluded from MVP and cannot be proposed by MVP planner).
5. **Manual accept overlap/overflow** with visually explicit warnings, never silently treat as valid.

A default optimization can solve weighted objective:

`MVP: min Σ_i [ λ1 * max(0, overflow_i)^2 + λ2 * (r_i - r_base)^2 + λ3 * (Δ_i)^2 ]` (V2 adds a separately validated video-retime penalty)

subject to `r_min <= r_i <= r_max`, monotonic source event order and no unapproved overlap (V2 only: add bounded total video stretch). The optimizer generates a plan **without producing audio/video changes**. The UI displays candidate settings and conflict reasons; user approves before full render.

Caveat: if source has overlapping speakers, monophonic voiceover policy cannot always satisfy all spans. Flag rather than forge impossible timing.

## 8. Audio compositing and mixing

- Decode each TTS artifact to common sample rate/format only for the final mix; position at rounded sample index from plan `round(t_us * sample_rate / 1e6)`.
- Apply equal-power or selected gain crossfade on overlapping clips; detect peaks/clipping and loudness, use limiting if enabled.
- Original audio mix options: mute original dialogue, mix original audio at configurable gain, duck original around synthesized speech using sidechain if selected.
- MVP uses mute/gain/ducking; preserve ambience where possible but note that voice and music are mixed together. V1 may add **optional** model-based Vocal Separation after real listening/hardware evaluation; do not claim perfect separation.
- Render intermediate `voice_mix.wav` with origin metadata and sample count; compare predicted vs measured duration.

## 9. Subtitle generation

- Render separate `.ass` with `PlayRes`, font/size/outline/background/position and exactly chosen timeline. Escape ASS syntax and Unicode correctly.
- UI offers subtitle toggle, placement, color, font, margins and optional OCR suggestion to avoid overlaying existing subtitle areas.
- OCR outputs are suggestions, with bounding boxes and preview; never automatically obscure important visual content.

## 10. Render and validate

1. Build immutable `RenderPlan` containing source hash, Sync Plan fingerprint, subtitle style, audio gains, video filter list, output codec/quality and paths.
2. Probe capabilities. Use no forced GPU path. Determine a compatible filter chain; if CPU subtitle filter is required, account for frame transfer.
3. Use FFmpeg arguments built as an argv list; long filter graph stored in `-filter_complex_script` file (legacy WinError 206 insight).
4. Render short range preview first on demand; full render in scratch, parse `-progress pipe:1` for real progress.
5. On encoder failure, controlled fallback only if user allows; report actual codec and hardware path used; do not hide quality change.
6. FFprobe output; validate video/audio stream, duration/rate/rotation, expected subtitle/audio, nonzero payload and checksum. Publish to chosen output folder atomically (cross-volume copy+verify if necessary).
7. Render failure leaves previous verified MP4 intact and an actionable log.

## 11. Inference/performance telemetry

Every stage writes elapsed times and resources: decode, model warm-up, inference, network wait, synth, disk, mix, encode, verify. No actual video transcript text or secrets in logs by default. Benchmark results separate cold/warm starts and Auto/Balanced/Fast profiles.
