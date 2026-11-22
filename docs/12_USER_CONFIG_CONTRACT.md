# 12 — User Configuration Schema & Presets

**Goal:** Expose meaningful choices at every stage; preserve a snapshot of settings used by each Run. Values below are candidate defaults, not hard-coded limits until implementation tests.

**Scope:** MVP exposes fixed-duration numeric synchronization; V1 optional separation and V2 stretch are not visible as MVP options. See `14_SCOPE_BASELINE.md`.

## 1. Scope inheritance

`AppDefault < UserPreset < ProjectSetting < RunSnapshot < PerUnitOverride`.

A Run stores effective merged config with schema version and hash, so editing app defaults later never silently changes an existing Run.

## 2. Project / import

| Setting | Type | Default (proposed) | User controls |
|---|---|---|---|
| `data_root` | approved local directory | user selected on first start | Yes |
| `output_root` | approved local directory | `data_root/exports` | Yes |
| `import_mode` | COPY/LINK | COPY | Yes |
| `retain_source` | bool | true | Yes, no auto delete |
| `filename_template` | string pattern | `{date}_{title}_{render_id}.mp4` | Yes, sanitized |
| `auto_cleanup_scratch` | bool | true after validation | Yes, scratch only |

## 3. ASR

| Setting | Default candidate | Valid choices / notes |
|---|---|---|
| `language` | zh or Auto | User-selected source language; unknown accepted |
| `model` | small | tiny/base/small/medium/large if installed and supported |
| `device` | Auto | CUDA/CPU only when probe confirms |
| `compute_type` | Auto | float16/int8/other supported modes |
| `beam_size` | 5 | bounded integer, cost shown |
| `vad_enabled` | true | on/off + advanced thresholds |
| `word_timestamps` | off initial | on if supported and budget allows |
| `initial_prompt` | blank | speaker/context glossary, safety validation |

## 4. Translation

| Setting | Default candidate | Description |
|---|---|---|
| `provider` | Gemini | no offline translation in MVP |
| `model_id` | compatible runtime choice | provider-discovered, no stale hardcoded name |
| `source_language` | inherit ASR | editable |
| `target_language` | vi | editable future extension |
| `genre` | General | review, tutorial, narrative, manhua, custom |
| `tone` | natural spoken | concise, formal, lively, custom |
| `two_pass` | true | explicit Pass1 context + Pass2 length/speakability |
| `glossary` | Project-scoped | named entities, terms, pronouns |
| `max_group_gap_ms` | 750 candidate | grouping only; don't lose IDs |
| `translation_length_policy` | balanced | preserve meaning; suggest shorter version |
| `punctuation_policy` | natural | no forced trailing period |
| `manual_review_required` | true on validation warning | never silently accept missing translation |

## 5. TTS (Edge default)

| Setting | Default candidate | Description |
|---|---|---|
| `provider` | edge | network-required |
| `voice_id` | vi-VN-NamMinhNeural | from legacy; verify current availability |
| `rate_percent` | 0 | global + per Unit |
| `pitch` | provider neutral | only if supported |
| `volume` | provider neutral | only if supported |
| `concurrency` | 2 | adjust within empirical limit |
| `max_retries` | 3 | transient only, bounded backoff |
| `preview_text` | short Vietnamese sentence | used in keyed cache |
| `auto_fit` | off initially | manual approval + measured recheck |
| `regenerate_mode` | only stale/missing | don't rewrite unchanged units |

## 6. AV-Sync

| Setting | Default | Effect |
|---|---|---|
| `strategy` | review_conflicts | MVP: use fixed-source-time conflict table; video stretch unavailable |
| `max_rate_change_pct` | 15 candidate | bounded voice speed deviation |
| `max_video_stretch_pct` | V2-only, disabled | Do not expose in MVP; requires canonical video+audio+subtitle time map |
| `allowed_overlap_ms` | 0 | explicit overlap policy |
| `target_gap_ms` | 0 | controlled padding |
| `manual_offsets_us` | empty | per Unit controls |
| `fit_tolerance_ms` | 100 candidate | flag when audio exceeds window |

## 7. Render

| Setting | Default | User choices |
|---|---|---|
| `resolution` | source | 720p,1080p,custom; respect aspect |
| `fps_mode` | preserve source timestamps | CFR explicit option |
| `codec` | h264 | H.264 / HEVC if supported |
| `encoder` | Auto | NVENC if tested, CPU x264 fallback |
| `quality_mode` | perceptual target | NVENC CQ or CPU CRF, not identical numeric scale |
| `preview_range` | user selected 10–20s | for quick validation |
| `original_audio_gain` | 0.1 | 0–1 scaled; show results |
| `voice_gain` | 1.0 | gain with clipping guard |
| `ducking_enabled` | false | with parameters if on |
| `subtitles` | enabled (default) | on/off, soft/hard, ASS/SRT |
| `subtitle_style` | clear white + outline | font/size/position/outline/back |
| `flip_crop_color` | off | optional creative editing, not copyright evasion |
| `output_path` | Project export default | choose folder/name |

## 8. Example user preset (illustrative, not validated API JSON)

```json
{
  "schema_version": 1,
  "preset_name": "Video dọc - tiếng Trung - thuyết minh",
  "import": {"mode":"COPY"},
  "asr": {"language":"zh","model":"small","device":"auto","vad":true},
  "translation": {"provider":"gemini","genre":"General","two_pass":true,"punctuation":"natural"},
  "tts": {"provider":"edge","voice_id":"vi-VN-NamMinhNeural","rate_percent":0,"concurrency":2},
  "sync": {"strategy":"review_conflicts","fit_tolerance_ms":100},
  "render": {"resolution":"source","encoder":"auto","subtitle_enabled":true,"original_gain":0.1}
}
```

Do not persist credentials in a preset export. Each Run stores the effective config snapshot; user changes cause new Run or appropriate downstream invalidation.
