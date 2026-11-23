# ERROR_LOG — Reproducible Incidents in the New Project

## ASR-01A-INFERENCE-FIX-001 — Faster-Whisper truyền tham số PyAV đã bị loại bỏ
- **Date/time:** 2026-11-23 19:43 UTC+7
- **Task/environment:** ASR-01A-INFERENCE-FIX; Windows, Python 3.14.2, Faster-Whisper 1.2.1, PyAV 19.0.2, CTranslate2 4.9.1; Demo2.mp4 là MediaAsset COPY READY.
- **Reproduction:** Chọn MediaAsset READY, tạo ASR Job với model base, CPU, không tải model; FFmpeg tạo WAV mono PCM 16 kHz hợp lệ, sau đó Faster-Whisper bắt đầu inference.
- **Observed:** Adapter chuyển lỗi thành `asr_inference_failed`; exception gốc là `TypeError: open() got an unexpected keyword argument 'metadata_errors'` tại `faster_whisper/audio.py` khi gọi `av.open`.
- **Root cause:** PyAV 19 đã loại bỏ đối số `metadata_errors`, trong khi Faster-Whisper 1.2.1 vẫn truyền đối số đó.
- **Remediation:** Ghim PyAV `18.1.0` trong `backend/requirements.txt`. Thêm regression giải mã qua `faster_whisper.audio.decode_audio` và diagnostic local có lọc, liên kết Job/Attempt/phase mà không lưu traceback, đường dẫn hay nội dung media.
- **Regression/verification:** PyAV decode test PASS. Faster-Whisper thật chạy CPU/int8 với Demo2 COPY: Job READY, 10 Segment có timestamp; transcript đọc lại sau khi khởi tạo DB/service mới. Backend suite PASS (85 tests), frontend build PASS.
- **Remaining:** UI interaction/browser visual test NOT RUN; launcher smoke BLOCKED bởi cổng 5173/8000 đang được tiến trình hiện hữu giữ. G3 chưa được PO nghiệm thu.

# ERROR_LOG — Reproducible Incidents in the New Project

## MEDIA-01A-FIX-001 — Import POST rejected because body model was parsed as query
- **Date/time:** 2026-11-23 16:56 UTC+7
- **Task/environment:** MEDIA-01A-FIX; Windows development environment, exact Windows build not recorded.
- **Reproduction:** Open an existing Project, select COPY, choose a video, then let the UI POST JSON `{selection_id, mode}` to `/api/v1/projects/{project_id}/media/imports` with its existing session/CSRF/Host/Origin headers.
- **Observed before fix:** HTTP 422 `validation_error`, sanitized detail `[{"field":"query","type":"missing"}]`. Reproduced with TestClient using the same method, URL shape, body and security headers.
- **Expected:** FastAPI validates JSON fields `selection_id` and `mode`, then validates the supplied selection and creates the durable import Job only when the selection is valid.
- **Root cause:** `ImportRequest` was declared inside `create_app`, while `main.py` uses postponed annotations. FastAPI observed `body` as `ForwardRef('ImportRequest')` outside the local class scope and classified it as a required query parameter. Route inspection showed one required query field named `body` and no body fields.
- **Remediation:** Move `ImportRequest` to module scope. Keep both fields required and `extra="forbid"`; do not weaken session, CSRF, Origin/Host or selection validation. Also make the `MediaAsset READY` transition and Job READY commit atomic so the API/UI cannot observe a successful Job while its asset is still PENDING.
- **Regression/verification:** The regression POST with valid JSON now reaches selection validation and receives 404 `selection_invalid` for a deliberately invalid token; omitting `mode` still returns 422 with field `mode`. Real FFmpeg fixture integration through FastAPI completed COPY and LINK Jobs, verified FFprobe metadata, preserved the source hash and returned both MediaAssets after reopening the database. Full backend suite: PASS (60 tests); frontend build: PASS.
- **Windows manual status:** Native picker and UI verification NOT RUN/BLOCKED. Launcher smoke was blocked because ports 5173/8000 were occupied by existing Node/Python processes owned by the current Windows account; neither process was stopped.

For known failure patterns from the reference ZIP, use `docs/09_LEGACY_REUSE_AUDIT.md`. Do not import them as incidents in the new project.

Each future incident should record: ID, date/time UTC+7, Task ID, environment, reproducible steps, observed/expected result, sanitized trace, root cause, remediation, regression test, and verified state.
