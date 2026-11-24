# ERROR_LOG — Reproducible Incidents in the New Project

## TRANS-01B — Draft editor retained leading whitespace
- **Date/time:** 2026-11-24 19:40 UTC+7
- **Observed:** The focused translation-revision test submitted an edited Unit with surrounding spaces; the saved response retained leading spaces while trailing spaces were removed by the existing punctuation normalizer.
- **Correction:** Trim surrounding whitespace at the draft-save boundary before invoking the shared punctuation normalizer; provider output normalization behavior remains unchanged.
- **Verification:** The focused migration, revision integrity, atomic save, 100/500/1000-unit fixture, and API session/CSRF tests passed after correction; the full backend suite (144 tests) and frontend production build also passed.

## TRANS-01A-FINAL-FIX — Translation resume and provenance contract defects
- **Date/time:** 2026-11-24 19:04 UTC+7
- **Observed:** Code review found Single-Pass omitted the Unit duration from its provider payload; worker resume checked only the current wire version; the final artifact used only the current provider object's in-memory call history. Translation request/persistence tests now verify these failure paths with captured payloads and simulated retries across two provider instances. No live Gemini call or user-data incident is claimed.
- **Correction:** Validate timing and include `duration_ms` for Single-Pass; version the changed wire contract; gate unfinished Runs against their immutable execution snapshot before provider creation; build manifest call provenance from the ordered SQLite ledger and Run configuration. Frontend async commits now validate selected Project/media/revision/run/job identity and respect consent state.
- **Verification:** Six focused backend regression tests **PASS**; frontend production build **PASS**. Real Gemini and browser interaction **NOT RUN**.

## TRANS-01A-UX-REFINE-001 — ASR result requires browser reload to appear
- **Date/time:** 2026-11-24 18:06 UTC+7
- **Observed:** Product Owner reported that a newly completed ASR Job showed no transcript until browser reload. The same report requested checking translation completion synchronization.
- **Root cause confirmed by source control flow:** The ASR polling effect called `setAsrJob(READY)` before awaiting ASR Runs and transcript revisions. Its dependency included the full `asrJob` object, so the state update caused React cleanup (`stopped = true`) while those fetches were still pending; subsequent state commits were discarded. Translation polling similarly committed the Job before fetching the Translation Run, and its effect depended on the full Run/Job state, allowing cleanup before the paired data was committed. This establishes a deterministic race from the source; interactive reproduction is **BLOCKED** while the existing UI/backend listeners occupy 127.0.0.1 ports 5173 and 8000.
- **Correction:** Fetch Job, matching Run and revisions first; validate Project/source identity against the currently selected context; then commit related state together. Polling effects are keyed to stable identities rather than whole mutable objects. Automated UI interaction remains NOT RUN.
- **Status:** Frontend production build passed; backend contract tests cover the service state but cannot directly prove browser rendering. Windows UI acceptance remains pending.

## TRANS-01A-ENGINE-COMPLETION-001 — Credential value surfaced by diagnostic command; Gemini Free Tier returned 503
- **Date/time:** 2026-11-24 15:45 UTC+7
- **Task/environment:** TRANS-01A-ENGINE-COMPLETION; Windows; configured local `.env`; Gemini API calls used only synthetic dialogue and the documented Free Tier model.
- **Observed security incident:** A repository search command included the root `.env` file and its configured credential value appeared in command output. The value was not copied into source, diagnostics, this log, or the change record. Because the conversation/tool output may be retained, the Product Owner should rotate that credential after review. No account or provider settings were changed by this task.
- **Observed provider incidents:** With valid Unicode input and one HTTP attempt per adapter call, Interactions returned HTTP 503 once in the 8-Unit comparison. Generate Content completed the same 8-Unit structured request in one physical HTTP request with exact IDs. In the 100-Unit synthetic run, Pass 1 returned 503 once, then succeeded on a separate bounded retry; Pass 2 returned 503 once. These observations identify no provider-side cause. No model fallback was enabled.
- **Request budget:** The valid-Unicode A/B used 2 measured HTTP requests. Three earlier route/schema probes and the first 100-Unit attempt added 4 observed requests; the final 100-Unit Pass 1 retry and Pass 2 added 2. An earlier preliminary harness did not return its transport summary and could have used at most 4 requests. Conservatively, the entire sweep used no more than 12; no live calls were made afterward.
- **Assessment:** Generate Content is the only route with successful valid-Unicode A/B evidence. Long-run Pass 2, an end-to-end persisted Job restart, and G4 remain unverified.

## TRANS-01A-ENGINE-OPTIMIZATION-001 — Small translation payload returned 503; separate output mapping rejected
- **Date/time:** 2026-11-24 13:41 UTC+7 (local diagnostic event time)
- **Task/environment:** Existing Windows local app diagnostics for `gemini-3.5-flash-lite`; inspected only allow-listed metadata, not transcript or response bodies.
- **Observed:** One 10-Unit request (2,319 prompt characters; local estimate 580 tokens) returned HTTP 503 on both bounded provider attempts. The diagnostic did not observe physical HTTP count. A separate 10-Unit request returned HTTP success but failed strict `gemini_response_mapping_invalid` validation.
- **Assessment:** The 503 occurred on a small request, so excessive payload size is not supported as its cause. The evidence does not identify Google's internal capacity cause. A successful HTTP response can still be unusable when output IDs/mapping fail validation; such a run must stay failed/reviewable and must not publish READY.
- **Verification:** Metadata was present in the existing local diagnostic JSONL. No live requests were sent during this task because PO set the live-test budget to $0. Automated mapping/503 regressions pass.
- **Remaining:** No model/API A/B evidence or real long transcript. Do not claim 503 resolved; G4 remains unaccepted.

## TRANS-01A-RETRY-FIX-001 — Interactions SDK nhân đôi request 503 với retry của adapter
- **Date/time:** 2026-11-24 13:34 UTC+7
- **Task/environment:** TRANS-01A-RETRY-FIX; Windows; ứng dụng pin `google-genai==2.34.0`, model `gemini-3.5-flash-lite`. PO báo AI Studio có 6 request, cả 6 lỗi HTTP 503, 0 output token cho lần dịch Demo2 gồm 10 câu.
- **Reproduction:** Với SDK 2.34.0, `HttpRetryOptions(attempts=1)` trên Interactions thực hiện thêm 1 retry. Transport mock qua SDK ghi nhận 2 HTTP request cho một `interactions.create`. Trong source, `GeminiProvider.translate` gọi SDK tối đa 3 lần cho cùng pass.
- **Root cause:** Retry multiplication `3 adapter calls × 2 HTTP requests/SDK call = 6 HTTP requests` khi Pass 1 luôn trả 503. Worker chỉ gọi Pass 2 sau khi Pass 1 đã trả về và qua mapping validation; do đó không có Pass 2 trong trường hợp này. Số request thực tế PO báo khớp đường gọi tái hiện; do lần chạy cũ không có request ID/log chi tiết nên không thể liên kết từng request lịch sử với JobAttempt cụ thể. Không có bằng chứng cho giả thuyết “retry quá nhanh gây 503”; 503 là response của Gemini.
- **Remediation:** Nâng pin lên `google-genai==3.6.1` — release chính thức sửa `Interactions retries = attempts - 1` — và đặt `attempts=1`, chọn API `v1beta` tường minh. Chỉ adapter quản lý retry: tối đa một lần cho 503/lỗi kết nối tạm thời; quota 429 dừng ngay. Job/Attempt không tự chạy lại FAILED; manual retry giữ các pass/chunk đã hoàn tất, áp dụng cooldown theo metadata Retry-After đáng tin cậy. Xem CHANGELOG TRANS-01A-RETRY-FIX.
- **Regression/verification:** SDK 3.6.1 HTTP transport mock xác nhận 503 tối đa hai HTTP request cho Pass 1 và không gọi Pass 2; 503 một lần rồi thành công hoàn tất hai pass dùng 3 HTTP request; 429, lỗi 400/401/403/404, timeout/cancel, redaction, cooldown, Bulk-First và duplicate Job được kiểm tra. Backend **PASS** (120 tests), frontend build **PASS**, Windows launcher smoke **PASS** (2/2). Một mẫu tiếng Trung tổng hợp qua Gemini thật hoàn tất 2 pass với 2 generation SDK calls và mapping/schema hợp lệ; không dùng transcript Demo2.
- **Remaining:** Demo2 translation qua UI, kết quả lưu qua restart sau Gemini thật và browser review **NOT RUN**. G4 vẫn chưa được PO nghiệm thu.

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
