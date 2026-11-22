# 09 — Audit of Uploaded Legacy Tool `video-dubbing-automation.zip`

**Source basis:** Actual attached ZIP inspected for this design. Files: `app.py` (~530 lines), `modules/transcriber.py` (~134), `modules/translator.py` (~777), `modules/tts_engine.py` (~280), `modules/video_editor.py` (~1284), README, `AI_RULES.md`, `PENDING_TASKS.md`, `CHANGELOG.md`, `ERROR_LOG.md`. Archive includes `.env`, bytecode and an 87 MB FFmpeg executable; **none should be copied into the new doc/source package**.

**Important:** Source code reviewed statically. Its claims in README/changelog are not independently verified benchmarks/functional acceptance; the uploaded scripts were not executed against real media.

## Reuse matrix

| Old source | Confirmed function / useful idea | Treatment | Danger to correct |
|---|---|---|---|
| `app.py` | Streamlit UI, upload-only, selectable source language/genre/voice, output path, render options, multi-video loop | Rebuild as React stage-based editors; reuse UX requirement | Single long blocking run, rerun collision, buffer copy; few stage inspection controls |
| `modules/transcriber.py` | FFmpeg prep, Faster-Whisper model cache, CUDA DLL handling, CPU int8 fallback, VAD timestamps | Adapt behavior into ASR Worker, add robust model lifecycle | Global singleton in wrong process, GPU setup dependencies, simplistic error None |
| `modules/translator.py` | Gemini prompts per genre/Manhua, bulk-first, quota guard, missing-ID retry, chunk fallback and API-key awareness | Re-implement via `google-genai`, preserve useful glossary/validation tests | Regex JSON extraction as trust boundary, model guesses, incomplete/missing output replaced by original text |
| `modules/tts_engine.py` | Edge-TTS async `Communicate`, voice/rate config, semaphore=5, retry, duration heuristic | Default provider adapter; retain bounded async design | **Silent WAV on failure**, **hard truncation >250 chars**, text mutation, simple file validation (MP3 only size check), rate heuristic not audio-measured |
| `modules/video_editor.py` | FFprobe duration, timing attempts, ASS/SRT, FFmpeg `filter_complex_script`, audio mix, NVENC -> x264 fallback, OCR subtitle placement | Mine for specific tested utilities and fixtures | Monolithic 1284-line method, mixed MoviePy/FFmpeg, source copy each render, unverified sync accuracy, path assumptions, false claim all filters GPU |
| `modules/downloader.py` | Legacy social-video download | Do **not** port into MVP | Old user intended local import only, platform dependency/cookies/DPAPI issues |
| `ERROR_LOG.md` | Windows WinError206, CUDA DLL errors, Gemini 404/429, FFmpeg fallback, subtitle positioning | Convert to new regression cases | Error notes do not prove defect fixed for all media |
| `AI_RULES.md` | Clear UI/logic boundary, cleanup, error discipline | Distill into new `AGENTS.md`/`RULES.md` | Tied to Streamlit and older architecture |

## Major findings (grounded)

1. Old TTS `process_single_segment` calls `safe_truncate_text(text, 250)` and assigns shortened text back to `item['text']`, risking omitted speech. New system must split without content loss or block.
2. On Edge failure it creates a 0.5–8.0 second silent WAV and treats path as valid; new system must show error and never claim spoken success.
3. Legacy Edge output is typically MP3, whereas fallback is WAV; mixed formats need explicit decoder/duration validation and standardized mix format before render.
4. `_calculate_adaptive_rate` derives a TTS speed choice from word-per-second thresholds, not measured generated audio; replace with generate -> FFprobe -> compare -> user review.
5. Legacy translation uses JSON parse salvage and even original-text fallback for missing translations; acceptable only with explicit `NEEDS_REVIEW`, never as valid Vietnamese.
6. Render invokes `h264_nvenc` and CPU fallback, but filters such as libass may run CPU-side. NVENC does not imply all graphics processing is GPU-native.
7. Render had Windows `WinError 206` precautions using `-filter_complex_script` and short workspace names; retain the technique with properly escaped inputs.
8. The old README describes filters as a way to avoid copyright detection. This assertion is not technically reliable and must **not** be carried over as a feature or guarantee. Optional crop, color, flip and logo masking are legitimate editing tools where the user has rights; do not market as evasion.
9. Source `app.py` copies uploads to workspace and has an auto-cleanup option. New project must separate source/owned output from disposable scratch to avoid accidental deletion.
10. Legacy translated text, multiple API keys and model fallback may incur inconsistent outputs or terms; persist config/provider and require explicit user choice for sensitive fallbacks.

## Reuse strategy

- **Reuse as algorithmic reference:** ASR settings, Edge async pattern, FFmpeg subprocess/`filter_complex_script`, subtitle timing/escaping tests, Gemini genre glossary.
- **Do not copy unchanged:** Streamlit `app.py`, giant translator/editor functions, implicit silent fallback, old SDK calls, downloader, `.env`, executable binary, cached data, names/personal paths.
- **Port by contract:** Write adapters with tests using fixed small video/audio/translation fixtures, compare timings and correctness; mark differences explicitly.

## Privacy of attachment

The supplied ZIP contains an `.env` file. This design intentionally does not inspect, reproduce or include its values. If it contained active API keys, revoke/rotate them and move secrets into ignored local environment/credential storage before any public commit.
