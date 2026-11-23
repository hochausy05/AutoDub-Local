# CHANGELOG — AutoDub Local vNext

## 2026-11-23 13:55 UTC+7 — ARCH-00D — Runnable development skeleton
- Tạo FastAPI loopback backend với one-time bootstrap, in-memory session, CSRF, strict Host/Origin/Fetch Metadata và health liveness; tạo React/TypeScript status UI (trạng thái trống, không giả lập media), Vite dev proxy, lockfile/dependency pins và Windows launcher `scripts/windows/dev.ps1`.
- Thêm backend security smoke tests, frontend/proxy integration smoke, cập nhật README/`.env.example`/`.gitignore`; chuyển giao quy tắc hai tầng CoverageGroup → RevisionSegment → TranslationUnit cho TRANS-01 trong proposal ARCH-00C. Không thêm ASR, Gemini, TTS, SQLite, import hay render.
- Backend unittest **PASS** (4 tests); `npm ci` và `npm run build` **PASS**; `scripts/windows/dev.ps1 -SmokeTest` **PASS** (2 tests, real local FastAPI/Vite proxy). Windows start/stop đã chạy; browser visual acceptance **BLOCKED** do computer-use native pipe unavailable. G0 chưa nghiệm thu.

## 2026-11-23 13:14 UTC+7 — ARCH-00C — Đề xuất hợp đồng pipeline MVP
- Tạo `docs/proposals/ARCH-00C_PIPELINE_CONTRACT_PROPOSAL.md`: đặc tả Gemini hai pass, Edge-TTS theo Unit, SyncPlan trên thời lượng đo thực và RenderPlan/Verified MP4; gồm schema, trạng thái, fingerprint, lỗi/recovery, test matrix và quyết định chờ PO/TL.
- Xác minh UTF-8 và nội dung proposal; `git diff --check` **PASS**. Runtime/provider/Windows/FFmpeg tests **NOT RUN**; G0 chưa nghiệm thu.

## 2026-11-23 13:08 UTC+7 — ARCH-00B-R1 — Sửa hợp đồng an ninh local
- Cập nhật `docs/proposals/ARCH-00B_LOCAL_API_SECURITY_CONTRACT_PROPOSAL.md`: bổ sung durable selection cho Import Job, trusted picker helper IPC, chính sách DataRoot và hợp đồng session/Host/Origin/CSRF độc lập công cụ build; sửa đoạn lỗi mã hóa và thêm test cases B23–B28.
- Proposal được giải mã UTF-8 nghiêm ngặt và kiểm tra lại nội dung tiếng Việt. Test runtime/browser/Windows/helper IPC **NOT RUN**; G0 chưa nghiệm thu.

## 2026-11-23 12:40 UTC+7 — ARCH-00B — Đề xuất API local và an ninh
- Tạo `docs/proposals/ARCH-00B_LOCAL_API_SECURITY_CONTRACT_PROPOSAL.md`; cập nhật `CHANGELOG.md`. Đề xuất trust boundary, phiên local, picker và COPY/LINK, media Range, API/Job/CAS, Windows cancellation và privacy; chờ review, chưa owner duyệt.
- Đọc lại tài liệu, đối chiếu ARCH-00A/scope và kiểm tra whitespace Git; runtime, browser, picker, provider và Windows process tests **NOT RUN**. Không triển khai API; G0 chưa nghiệm thu.

## 2026-11-23 02:16 UTC+7 — ARCH-00A — Đề xuất hợp đồng dữ liệu
- **2026-11-23 12:33 UTC+7 — ARCH-00A-R2:** sửa `docs/proposals/ARCH-00A_DATA_CONTRACT_PROPOSAL.md` và mục CHANGELOG này: bổ sung SyncPlan fingerprint/timing, schema coverage group, stage CAS và logical Unit key; kiểm tra tài liệu và git diff --check, runtime tests **NOT RUN**. Chờ review, G0 chưa nghiệm thu.
- **2026-11-23 02:32 UTC+7 — ARCH-00A-R1:** sửa `docs/proposals/ARCH-00A_DATA_CONTRACT_PROPOSAL.md` và bổ sung mục này: làm rõ Segment identity/Unicode, TTS reuse 99/100, CAS publication, FK và state transitions; test runtime **NOT RUN**. Các policy và thiết kế vẫn chờ review; G0 chưa nghiệm thu.
- Tạo `docs/proposals/ARCH-00A_DATA_CONTRACT_PROPOSAL.md`; cập nhật `CHANGELOG.md`. Đề xuất revision/lineage, fingerprint, invalidation, publication/recovery và trạng thái Job/Run/Artifact.
- Kiểm tra tài liệu và phạm vi bằng đọc lại, tìm nội dung và git diff --check; test runtime/provider chưa chạy, chưa có implementation.
- Chính sách sync conflict, split mapping, recovery và tolerance còn chờ Technical Lead/Product Owner review; G0 chưa được nghiệm thu.

## 2026-11-23 — ARCH-DOC-02 — Tái cấu trúc bộ tài liệu và thư mục gốc
- Tạo cấu trúc repository mẫu `AutoDub-Local-vNext/` với các thư mục backend, frontend, scripts, tests, docs và file cấu hình an toàn.
- Điều chỉnh phạm vi: MVP chỉ có bảng Sync đơn giản, Job local tối thiểu, render MP4; Vocal Separation và Backup/Restore đầy đủ ở V1; Timeline nhiều track và Video Stretch ở V2.
- Cập nhật PRD, PLAN, TASK, roadmap, tài liệu kỹ thuật và báo cáo Word/PDF; tất cả mới là thiết kế, chưa chạy code hoặc nghiệm thu.

## 2026-11-23 — ARCH-DOC-01 — Hồ sơ thiết kế ban đầu
- Lập bộ tài liệu tham khảo kế thừa tool cũ và kiến trúc local-first; không có mã nguồn ứng dụng mới.
