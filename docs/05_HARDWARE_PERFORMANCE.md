# 05 — Hardware Utilization & Performance Strategy

> **Phase rule:** `14_SCOPE_BASELINE.md` defines mandatory MVP vs V1/V2 scope; advanced designs in this document do not imply MVP implementation.

**Goal:** Use available hardware efficiently without resource exhaustion, driver conflicts or falsely claiming that every step is GPU accelerated. **Hardware of the owner's PC has not been supplied**, so values below are **tunable policies**, not confirmed settings.

## 1. Hardware probe on first launch and Settings

Collect safely:

- CPU name/logical/physical cores, memory available, disk filesystem and free bytes, SSD/NVMe if observable.
- NVIDIA GPU name, VRAM total/free, driver, compute capability / CUDA runtime if `nvidia-smi` and dependency probe succeed.
- Faster-Whisper/CTranslate2 GPU initialization smoke test with an appropriately tiny model (opt-in if costly), distinguish CUDA DLL unavailable vs out-of-memory.
- FFmpeg build, decoders, `h264_nvenc`/`hevc_nvenc`/`av1_nvenc` (if supported), `libx264`, `subtitles/libass`, `scale_cuda`/`scale_npp`; actual micro encode/decode test.
- Network reachability for Gemini/Edge separately, without leaking credentials.

**Display:** `Configured / Tested / Disabled / Fallback`, not merely `GPU detected`.

## 2. Resource profiles

| Profile | ASR | TTS | Render | Suitable user experience |
|---|---|---|---|---|
| Auto (default) | Benchmark device and fit VRAM; choose conservative model | 2 concurrent Edge requests initially | NVENC if tested + beneficial | Safe fastest detected |
| Balanced | Moderate beam/batch, reserves UI headroom | 2 parallel requests | Quality preset balanced | Work while browsing |
| Fast | Larger ASR batch *only if VRAM permits* | Up to empirically tested max | More GPU utilization/fast preset | Speed over power |
| Custom | User sets device/model/beam/threads/VRAM budget | Custom bounded concurrency | Codec, CQ/CRF, threads | Experts with guardrails |

No hard-coded 100% CPU/VRAM setting. Reserve OS headroom (e.g., 15–25% RAM as initial heuristic) and abort unsafe allocations. When memory pressure rises, reduce batch size or queue jobs rather than crash.

## 3. GPU workload planning

**ASR:** Faster-Whisper uses CTranslate2; CUDA float16 may accelerate significantly. `batch_size` raises throughput at increased VRAM cost. Benchmark 30–60 s of source content with selected model and compute type. Cache one model per ASR Worker process; recycle only on model/config change, memory leak or crash.

**OCR:** OCR of subtitle bands is optional. Sample sparse frames, crop only candidate region, batch operations; use GPU only if compatible with ASR scheduling and worth VRAM. Do not allocate a second heavy model while ASR occupies nearly all VRAM.

**Video decoding:** hardware decode optional. Processing with CPU-only filters may force GPU→CPU copies; benchmark end-to-end. CPU-side subtitle composition can be unavoidable with chosen libass graph.

**Video encoding:** NVENC hardware encoder for H.264/HEVC where available; use `libx264` fallback. VFR source should use appropriate timestamp handling; do not round fps and blindly force CFR unless selected.

**Edge-TTS:** remote synthesis is network-bound. Local GPU won't accelerate Microsoft's remote voice generation. Tune concurrency to avoid throttling, rate-limit classification and loss of samples.

## 4. Scheduling resource budgets

Suggested initial tokens (tune by actual hardware):

```text
GPU_COMPUTE = 1      # one high-VRAM inference task at a time
GPU_ENCODE  = 1      # concurrent encoding only if benchmark benefits
CPU_HEAVY  = 1 or 2 # based on cores, RAM and disk
NETWORK_TTS = 2     # adaptive after controlled tests
GEMINI_API = 1       # quota-aware request scheduling
DISK_WRITE_HEAVY = 1 # especially HDD
```

Workers acquire resource permits **before** processing; release in `finally`; persisted Job ownership/attempt state is separate from in-memory semaphore (V1 may add lease/heartbeat if required). Interactive voice preview may outrank background batch with bounded priority to prevent starvation. Thermal throttling and memory pressure should reduce concurrency.

## 5. Feedback measurements

Track per Job `elapsed_s`, `input_duration_s`, `real_time_factor = elapsed / duration`, CPU %, peak RAM, GPU load/peak VRAM, encoder FPS, network wait, failed/retried requests, cache hit ratio. Benchmark three representative clips: short 21s, medium 5 min and longer 20 min (or owner media). Record exact hardware, model, FFmpeg build, driver and settings. Never use simulated numbers as production results.

## 6. Fallback matrix

| Failure | Response | User message |
|---|---|---|
| CUDA/cuDNN DLL missing | Option to retry once CPU `int8`, log actual device | "GPU unavailable; running CPU" |
| CUDA VRAM exhausted | Reduce batch/model or queue; confirm before large quality downgrade | "Insufficient VRAM" |
| NVENC not installed or unsupported codec | Offer libx264, report codec difference | "Using CPU encoder" |
| Edge network disconnect | Bounded retry then FAILED; preserve good Units | "Unit failed, retry" |
| Gemini quota exhausted | Pause translation Job; preserve drafts | "Quota exhausted" |
| Disk full | Stop BEFORE destructive writes; retain verified source/results | "Select another destination" |

## 7. Hardware compatibility controls

- Pin tested matrix during implementation; don't promise CUDA 12/cuDNN 9 on every Windows PC.
- Test subprocess loading of CTranslate2 separately from FFmpeg NVENC; working one does not prove the other.
- Prefer `nvidia-smi` only as diagnostic, not proof that FFmpeg build supports NVENC.
- Record all fallbacks in Job metadata and visible UI.
- Provide CPU-only quality-preserving mode for users without NVIDIA.

## Staged resource work

MVP prioritizes benchmarking Faster-Whisper ASR, short Edge-TTS network concurrency and FFmpeg encoding against actual hardware; do not simultaneously saturate CPU, RAM, VRAM and disk. **V1 only:** any selected Vocal Separation model must undergo VRAM/CPU profiling and preflight checks. **V2 only:** interactive timeline and video retime performance work. No unsupported claim that a model uses GPU simply because a GPU exists.
