# 11 — Technology References, Compatibility and Decision Rationale

**Reviewed for architecture on 2026-11-23.** Exact library versions require a fresh compatibility test when creating the new repo. References are *not evidence that the new app has passed runtime testing*.

| Domain | Primary reference | Design implications |
|---|---|---|
| Faster-Whisper | https://github.com/SYSTRAN/faster-whisper | CTranslate2-based ASR, CUDA vs CPU, batched inference, GPU installation requirements |
| CTranslate2 | https://opennmt.net/CTranslate2/ | Hardware backend and compute type compatibility |
| FFmpeg CLI | https://ffmpeg.org/ffmpeg.html | Audio/video timeline, filter graphs, process progress, hardware accel may move frames between devices |
| FFmpeg encoders | https://ffmpeg.org/ffmpeg-codecs.html | NVENC support depends on GPU/driver/build and codec/filter requirements |
| NVIDIA FFmpeg | https://developer.nvidia.com/ffmpeg | Distinguishes hardware decoding, encoding and GPU-native filtering |
| SQLite WAL | https://www.sqlite.org/wal.html | Readers/writer and checkpoint requirements, backup behavior |
| Google Gen AI SDK | https://googleapis.github.io/python-genai/ | Use `google-genai`, structured generation, model discovery and provider errors |
| Edge-TTS | https://github.com/rany2/edge-tts | Online provider, voice/rate API, treat service reliability as unguaranteed |
| FastAPI | https://fastapi.tiangolo.com/ | API routing, file responses, validation |
| React | https://react.dev/ | Stateful editors, effects cleanup |
| Vite | https://vite.dev/ | Local frontend development build |

## Dependency policy

1. Bootstrap with Python 3.11 as initial compatibility candidate (legacy itself used 3.11/3.14 bytecode; this is not a compatibility guarantee). Test library installation on target Windows machine.
2. Pin `faster-whisper`, `ctranslate2`, CUDA/cuDNN-compatible runtime, `edge-tts`, `google-genai`, Pydantic/FastAPI, frontend packages after a working clean environment.
3. Record executable versions and FFmpeg license. Include lockfiles and hashes where feasible.
4. GPU availability cannot be inferred from owner's ZIP; `ffmpeg.exe` included in ZIP is not redistributed here.
5. Structured output and two-pass translation must be tested with actual selected Gemini model/SDK at development time.
6. Do not reintroduce Supabase libraries, cloud buckets, or auth into the new MVP.

## Risks requiring runtime verification

- Edge TTS may change API behavior, available voices or throttling. Measure error rate, avoid unbounded concurrency.
- Gemini models/quotas/prices change; list model availability or provide explicit configurable model ID.
- CUDA DLL/CTranslate2 mismatch on Windows; probe actual loader and allow CPU-only mode.
- GPU encoding alongside CPU subtitle filters: benchmark graph end-to-end; avoid fake "all-GPU" labels.
- SQLite locking under concurrent Workers; test busy_timeout and short transactions.

## Vocal separation future evaluation (V1)

Demucs and dialogue-oriented alternatives are **candidates**, not selected dependencies. Before implementation, verify actual maintained package, model/license, hardware support and listening results; music-vocal separation does not guarantee clean speech/background/SFX isolation. See `14_SCOPE_BASELINE.md`.
