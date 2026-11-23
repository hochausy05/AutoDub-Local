# CHANGELOG — AutoDub Local vNext

## 2026-11-23 17:16 UTC+7 — MEDIA-01B — HTTP Range playback và safe relink
- Thêm migration v6 additive: selection hỗ trợ purpose `relink` gắn Asset, optimistic `lock_version` và lịch sử relink; v1–v5 được giữ nguyên. Thêm GET/HEAD media stream cho asset READY thuộc đúng Project, session/Host/Origin/Fetch-Site guard, MIME whitelist, no-store/nosniff, ETag/conditional request, byte Range, streaming theo chunk và đóng handle khi response kết thúc.
- COPY chỉ đọc artifact đã được ArtifactService xác minh; LINK kiểm tra path/open-handle identity mỗi lần mở, SHA-256 khi identity chưa được xác minh trong tiến trình, đổi identity hoặc sau restart; full response cũng được kiểm tra digest trong lúc truyền. Nguồn mất/đổi sẽ cập nhật trạng thái. Relink dùng Windows picker nonce hiện có, selection gắn session/Project/Asset, kiểm tra identity/hash/FFprobe, chỉ tự nhận cùng nội dung và dùng transaction/CAS; nội dung khác bị từ chối, selection bị revoke, lịch sử nguồn giữ lại. Thêm HTML video player và hành động chọn lại LINK bị thiếu/thay đổi.
- Backend unittest **PASS** (64 tests), gồm MP4 Unicode thật tạo bằng FFmpeg, full/HEAD/Range/open-ended/suffix/416/400, ETag/If-Range/If-None-Match, cross-Project, session/Origin, COPY/LINK integrity, restart, disconnect handle, relink cùng/khác nội dung và CORE-01/MEDIA-01A regression. Frontend `npm run build` **PASS**; PowerShell helper parse **PASS**.
- Frontend `npm run test:smoke` **FAIL** khi gọi service đang chiếm cổng; bootstrap trả HTTP 410. Launcher `dev.ps1 -SmokeTest -NoBrowser` **BLOCKED** vì cổng 5173 và 8000 đang bận; launcher dừng trước khi khởi chạy, không dừng process đang chạy. Browser visual/manual playback, native picker và relink **NOT RUN**. G2 **NOT ACCEPTED**; cần PO chạy hướng dẫn manual trên Windows với launcher và file thật.

## 2026-11-23 16:56 UTC+7 — MEDIA-01A-FIX — Sửa import POST bị 422
- Tái hiện lỗi 422 của PO với request POST thực tế; FastAPI phân loại `body: ImportRequest` thành required query vì model được khai báo cục bộ trong `create_app` dưới postponed annotations. Đưa model lên module scope, giữ schema bắt buộc `selection_id`/`mode` và `extra="forbid"`.
- Thêm regression test: payload hợp lệ qua body validation và tới kiểm tra selection; payload thiếu `mode` vẫn bị từ chối 422 đúng field. Thêm API integration test chạy worker thật với MP4 test fixture: COPY và LINK qua API/Job/FFprobe, idempotency replay trả cùng Job, metadata được lưu và thấy lại sau restart; nguồn gốc giữ nguyên. Chuyển công bố MediaAsset và Job READY vào cùng transaction hoàn tất, tránh trạng thái thành công quan sát được khi asset còn PENDING.
- Backend unittest **PASS** (60 tests), frontend `npm run build` **PASS**. `git diff --check` và UTF-8 strict decode **PASS**. Windows launcher smoke **BLOCKED** do port 5173/8000 thuộc các process Node/Python đang chạy dưới tài khoản hiện tại; không dừng process. Manual native-picker/UI **NOT RUN/BLOCKED**.
- G2 vẫn **NOT ACCEPTED**; PO cần dừng ứng dụng đang chạy rồi khởi động lại để nạp backend mới và thực hiện manual picker/COPY/LINK/restart test.

## 2026-11-23 16:23 UTC+7 — MEDIA-01A — Windows video selection & source import
- Thêm migration v5 additive cho private one-time selection, MediaAsset và generation state; v1–v4 không bị sửa. Thêm helper PowerShell STA do backend mở với nonce một lần qua stdin/stdout pipe; response chỉ trả opaque selection ID/tên hiển thị. Selection gắn session/Project, TTL 5 phút, consume cùng transaction tạo durable `media_import` Job snapshot; absolute source path chỉ lưu trong dữ liệu nội bộ, không có trong API response hoặc error.
- Thêm worker dùng Job/Attempt và ArtifactPublicationService hiện có. COPY hash/size, disk preflight, FFprobe, publish artifact bất biến và giữ file gốc; LINK không copy/move nguồn, lưu private reference và tạo manifest metadata riêng. FFprobe trích container, duration, kích thước, codec, streams, frame-rate; lỗi file hỏng/không hỗ trợ/no video/no FFprobe được chặn. Thêm API selection/import/list media và UI chọn Project, COPY/LINK, tiến độ cùng metadata.
- Backend unittest **PASS** (58 tests, gồm CORE-01 regression, migration v5, COPY/LINK service, giữ nguyên nguồn, idempotency/session/project-bound và expired selection, source change trước/trong import, stale generation CAS, Job cancel/restart/retry, Unicode MP4 thật do FFmpeg tạo làm test fixture, no-audio/corrupt FFprobe và API session/CSRF/path rejection); frontend `npm run build` **PASS**; helper PowerShell syntax parse **PASS**. `git diff --check` và UTF-8 strict decode **PASS**.
- Windows launcher smoke **BLOCKED**: 127.0.0.1 ports 5173/8000 đang được PID khác sử dụng; không dừng các process đó. Windows desktop/native picker và manual COPY/LINK với video người dùng, hiển thị UI, restart persistence **BLOCKED/NOT RUN** vì computer-use native pipe không khả dụng và launcher không khởi động được. G1 được PO xác nhận đã nghiệm thu theo yêu cầu task; G2 **chưa nghiệm thu**. HTTP Range playback và missing-source relink thuộc MEDIA-01B.

## 2026-11-23 15:56 UTC+7 — CORE-01D — Startup Recovery & G1 Verification
- Thêm migration v4 giữ nguyên v1–v3: stage pointer theo stage với input/pointer generation, Job activation snapshot bất biến, Attempt activation outcome và bảng recovery findings bền vững. Thêm `StartupRecovery` vào FastAPI lifespan sau migration và trước khi bật Job claims.
- Reconcile RUNNING/CANCEL_REQUESTED thành INTERRUPTED, giữ attempt history, retry tường minh có giới hạn/idempotency; claim bị khóa trước recovery và một local supervisor không claim Job thứ hai khi còn Job active. Không kill process theo PID cũ.
- Đối chiếu Artifact READY bằng hash/validator; phục hồi STAGING/VERIFIED candidate chỉ khi đủ bằng chứng, nhận diện orphan qua manifest/inventory, không xóa candidate. Mất/hỏng Artifact bỏ active pointer nhưng giữ record; thiếu validator cho READY sẽ fail closed trước claim. Thêm typed stage activation với CAS theo input fingerprint/generation và pointer generation; stale/cancelled output giữ lịch sử nhưng không thay pointer.
- Backend unittest **PASS** (46 tests: CORE-01A/B/C regression, migration v1→v4 giữ Project/Job/Attempt, recovery idempotency, crash-point recovery, orphan không xóa, Job A/B CAS, cancel/completion và retry). Windows hard process termination harness **PASS** tại điểm sau rename/trước DB READY commit. Frontend build **PASS**; Windows launcher smoke **PASS** (2 tests). `git diff --check` và UTF-8 strict decode **PASS**.
- Windows process-tree cancellation của worker thật **NOT RUN** vì chưa có worker/executor trong slice; cross-volume copy không được dùng/hỗ trợ và không có media/provider validator. G1 **READY FOR PO REVIEW**, chưa được PO nghiệm thu.

## 2026-11-23 15:20 UTC+7 — CORE-01C — Atomic Artifact Publication
- Thêm migration v3 (giữ nguyên v1/v2) với manifest `artifacts`, FK ownership Project/Job/Attempt, fingerprint, relative paths, SHA-256, size, validator metadata/version, trạng thái publication và trigger khóa manifest/nội dung đã xác minh. Migration v3 được kiểm tra bảo toàn Project, Job và Attempt có sẵn.
- Thêm `ArtifactPublicationService` nội bộ: streaming vào staging, flush/fsync, validator theo kind, ghi nhận VERIFIED, publish đường dẫn bất biến không ghi đè, hash lại file đích rồi mới commit READY. Completion evidence được seal và kiểm tra lại artifacts; Job/Attempt phải còn hợp lệ. Không có API artifact công khai, validator media production hay activation/current pointer.
- Backend unittest **PASS** (35 tests, gồm artifact, CORE-01A/B regression, migration, đồng thời, Windows Unicode/no-overwrite, fault injection sau manifest/staging/verification/rename/DB commit, lỗi commit, mất/hỏng file, cancel và artifact lịch sử khi attempt lỗi/stale). `git diff --check` và UTF-8 strict decode **PASS**.
- Frontend `npm run build` **PASS**; Windows `scripts/windows/dev.ps1 -SmokeTest` **PASS** (2 proxy/session security tests). Không có thay đổi UI hoặc artifact endpoint.
- Startup reconciliation/orphan recovery và stale activation **NOT RUN / CORE-01D**; FFmpeg/TTS/media validators **NOT RUN / ngoài phạm vi**; G1 chưa nghiệm thu.

## 2026-11-23 14:55 UTC+7 — CORE-01B — Durable Job & Attempt lifecycle
- Thêm migration v2 (không sửa migration v1): `jobs`, `job_attempts`, FK/UNIQUE/CHECK, one-active-Attempt index, immutable input-snapshot và state-transition guards; giữ nguyên Project cũ. Dùng chung bảng `idempotency_records` theo scope; Job lưu snapshot JSON, fingerprint, request digest và khóa idempotency.
- Thêm `JobService` nội bộ: tạo, đọc, claim `BEGIN IMMEDIATE` cùng insert Attempt, progress có whitelist, cancel, complete có `CompletionEvidence`, fail với error code đã sanitize và retry có điều kiện (tối đa 3 Attempts). Thêm session/CSRF-protected GET/cancel/retry API; không có public Job creation hay executor/provider.
- Backend unittest **PASS** (24 tests, gồm 14 Job tests và regression CORE-01A): migration bảo toàn Project, restart giữ RUNNING để CORE-01D reconcile, idempotency đồng thời, claim race, cancel/completion race, retry/history, auth/ownership và transition guards. Frontend build/typecheck **PASS**; Windows launcher/Vite proxy smoke **PASS** (2 tests). `git diff --check` và UTF-8 strict decode **PASS**.
- Process-tree cancellation, executor thật và startup reconciliation **NOT RUN / ngoài CORE-01B**; G1 chưa nghiệm thu.

## 2026-11-23 14:34 UTC+7 — CORE-01A — SQLite & Project persistence
- Tạo SQLite factory và migration v1 transactional với WAL, `foreign_keys=ON`, `busy_timeout=5000`; mặc định lưu ngoài source tại `%LOCALAPPDATA%\AutoDub-Local`, từ chối UNC/network drive và cho phép `AUTODUB_DATA_ROOT` local ngoài source. Thêm bảng Projects và durable idempotency records.
- Thêm API session-protected tạo/liệt kê/đọc Project, validation UUID/name, error envelope, CSRF và idempotency key. Nối UI thật để tạo/liệt kê Project; không có mock Projects. Cập nhật hướng dẫn, smoke test, `TASK.md` đánh dấu riêng slice CORE-01A đã kiểm tra và `UPDATES.md`.
- Backend unittest **PASS** (10 tests: startup/reopen/migration/SQLite PRAGMAs/FK, persistence, validation/idempotency, session/CSRF, concurrent writes và data-root guard); frontend build/typecheck **PASS**; Windows `scripts/windows/dev.ps1 -SmokeTest` **PASS** (2 tests qua Vite proxy, bao gồm Project list và POST thiếu CSRF).
- Browser visual acceptance **NOT RUN**. Job/Attempt, recovery, media import, provider và render chưa triển khai; G1/CORE-01 chưa nghiệm thu.

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
