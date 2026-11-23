# CHANGELOG — AutoDub Local vNext

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
