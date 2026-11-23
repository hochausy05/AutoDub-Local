# ARCH-00A — Đề xuất hợp đồng dữ liệu và toàn vẹn trạng thái

## 1. Trạng thái và nguồn

**PENDING REVIEW; G0 NOT ACCEPTED.** Thiết kế MVP, chưa có migration, backend hay kết quả runtime. Các bổ sung bên dưới là đề xuất chưa được Product Owner phê duyệt. Nguồn: `AGENTS.md`, `RULES.md` R02–R05, R10, R13–R17; `docs/02_SYSTEM_ARCHITECTURE.md` §§4–6, `docs/03_DATA_AND_STORAGE.md` §§2–7, `docs/04_PIPELINE_ALGORITHMS.md` §§2–7, 10, `docs/07_JOBS_AND_API.md` §§4–6, `docs/08_IMPLEMENTATION_ROADMAP.md` G0–G1, `docs/10_QA_SECURITY_RECOVERY.md`, `docs/12_USER_CONFIG_CONTRACT.md`, `docs/13_RENDER_TIMELINE_SPEC.md` §§1–3, `docs/14_SCOPE_BASELINE.md`, `PLAN.md`, `TASK.md`.

## 2. Tóm tắt

Giữ kết quả ASR và provider bất biến; sửa trên draft có version, chấp nhận bằng compare-and-swap (CAS). Mỗi job chụp input, kết quả cũ có thể lưu nhưng chỉ kích hoạt nếu input vẫn hiện hành. Fingerprint theo nội dung và cấu hình liên quan, kiểm tra file trước tái sử dụng. File và SQLite không có transaction chung: publish có staging, kiểm chứng và đối chiếu sau crash. Không xóa kết quả cũ khi invalidation. Chỉ một supervisor claim job local. Scope ưu tiên `docs/14`; separation và backup đầy đủ thuộc V1, timeline và retime thuộc V2.

## 3. MVP data model

Quy ước đề xuất: `id` UUID text PK NOT NULL; FK NOT NULL trừ khi đánh dấu `?`; `schema_version` integer NOT NULL; `created_at` UTC NOT NULL; JSON là UTF-8 đã validate; `fp` SHA-256 text NOT NULL cho snapshot tính toán; `?` nghĩa nullable. Text và paths không ghi log. Mỗi record thuộc project; FK phải kiểm tra cùng project, kể cả qua composite FK hoặc domain transaction. Status liệt kê tại §11. Các bảng chuyên biệt dưới đây kế thừa schema logical `docs/03` (đã đặc tả); revision segment, lineage, CAS, publication state và các constraint chi tiết là **đề xuất bổ sung, chờ duyệt**.

| Entity | Mục đích; trường bắt buộc ngoài quy ước; FK | Ràng buộc, mutability, transaction |
|---|---|---|
| Project | sở hữu active pointers; name text, lock_version integer, active_source?, active_transcript_revision?, active_translation_revision?, active_tts_run?, active_sync_plan?, active_render? | pointers FK cùng project; mutable bằng CAS; cập nhật pointer trong transaction sau validation; không xóa revision đã duyệt |
| MediaAsset | source/output; project FK, role, mode COPY/LINK, location, size integer, sha256, probe JSON?, status | size >= 0; location được chọn tin cậy; immutable sau READY; link không thuộc quyền xóa; publish và pointer cùng transaction |
| TranscriptionRun | project, source FK, config JSON, tool/model version, fp, status | input/config/output bất biến sau hoàn tất; status lifecycle mutable; tạo run mới khi đổi model |
| Segment | ASR gốc; run FK, position integer, start_us/end_us integer, text, flags JSON | UNIQUE(run,position), 0 <= start < end; immutable; thời gian gốc không bị edit |
| TranscriptRevision | project, run FK, parent revision?, revision_no, status, lock_version, schema_version | UNIQUE(run,revision_no); draft mutable với CAS; accepted snapshot immutable; gồm RevisionSegment và lineage bên dưới; accept + pointer cùng transaction |
| TranslationRun | project, accepted transcript revision FK, provider/model/config JSON, fp, status | pass/chunk result immutable sau verify; không thay draft hay pointer cũ; snapshot và provenance bắt buộc |
| TranslationUnit | translation run FK, position, ordered mapping tới revision segments, pass1/pass2 text?, status | UNIQUE(run,position); mapping và coverage xác minh trước publish; proposed output immutable; thiếu output explicit review |
| TranslationRevision / Edit | revision: project, run FK, parent?, revision_no, lock_version, status; edit: revision FK, unit FK, text | UNIQUE(revision,unit); draft mutable CAS; accepted snapshot bất biến; Save All atomic; lịch sử edit riêng tùy chọn, không cần event sourcing MVP |
| TTSRun | project, accepted translation revision FK, config JSON, fp, status | snapshot immutable; status tổng hợp theo Unit; không xóa output cũ |
| TTSArtifact | producer_run FK, logical_unit_key, fp, fingerprint_version, path?, sha256?, duration_us?, sample_count?, format?, status, verification JSON? | UNIQUE(project,id); tham chiếu qua TTSRunUnit; READY yêu cầu file, hash, duration > 0, decode và kiểm chứng speech; publication transaction |
| SyncPlan | project, source FK, translation revision FK, config, ordered TTS artifact references, conflicts JSON, fp, status | immutable snapshot; edit tạo plan mới; chỉ plan được xác minh và duyệt đủ điều kiện mới render |
| RenderRun | project, sync plan FK, config, fp, output asset FK?, probe report?, status | READY yêu cầu output verified; không overwrite MP4 đã duyệt; actual encoder và timing được lưu |
| Job | project FK, kind, input snapshot JSON, fp, idempotency key, status, cancel flag, lock_version | UNIQUE(project,kind,idempotency key); key tái dùng với payload khác phải conflict; tạo/claim/cancel trong transaction ngắn |
| JobAttempt | job FK, number integer, status, owner?, started_at?, ended_at?, error_code? | UNIQUE(job,number); một attempt active/job; retry tạo attempt mới, giữ lịch sử |
| ArtifactDependency | child kind/id, parent kind/id, parent fp, dependency role | UNIQUE(child,role,parent); đề xuất relation typed với FK thật theo loại hoặc bảng registry nếu cần; không dùng polymorphic FK không kiểm chứng. Có thể suy ra từ snapshot MVP nếu graph đơn giản |

Mỗi bảng có PK; `schema_version` là phiên bản định dạng, `lock_version` là số CAS, `revision_no` là thứ tự revision, `fingerprint_version` là phiên bản thuật toán hash. RevisionSegment có `row_id` PK mới, `stable_segment_key`, revision FK, position, text, start/end_us; UNIQUE(revision_id,stable_segment_key), UNIQUE(revision_id,position), CHECK(0 <= start_us < end_us). Nguồn liên kết qua coverage group bên dưới, không qua RevisionSegmentSource trực tiếp.

**Logical SQLite FK:** bảng project-owned có project_id NOT NULL, PK id, UNIQUE(project_id,id); con dùng composite FK (project_id,parent_id) tới parent(project_id,id), ON DELETE RESTRICT. Active pointer nullable dùng (project.id,active_id) tới target(project_id,id); NULL bỏ qua FK, transaction kiểm ACCEPTED/READY. Circular FK deferred hoặc insert Project pointer NULL rồi update. RevisionSegment UNIQUE(project_id,revision_id,row_id), FK cùng project tới revision; mapping FK (project_id,revision_id,row_id) tới revision segment và (project_id,source_id) tới Segment; domain kiểm source run trùng revision. TranslationUnitRevisionSegment chứa project_id, translation_run_id, unit_id, revision_id, row_id, ordinal; có composite FK cùng project tới Unit và (project_id,revision_id,row_id) tới RevisionSegment; domain kiểm transcript revision; UNIQUE(unit_id,ordinal), UNIQUE(translation_run_id,row_id). Validation cross-row kiểm coverage và explicit skip.

TTSRunUnit(project_id,tts_run_id,logical_unit_key,artifact_id?,expected_fp,status) PK (tts_run_id,logical_unit_key), composite FK cùng project tới TTSRun và TTSArtifact; artifact_id NULL chỉ khi chưa READY. Artifact thuộc Project, producer_run là provenance; ON DELETE RESTRICT. Transaction kiểm logical key, fingerprint/version, file/hash và revision membership trước gắn. SyncPlanUnit có composite FK cùng project tới SyncPlan và TTSArtifact, UNIQUE(plan_id,logical_unit_key), UNIQUE(plan_id,ordinal); accept kiểm exact Unit coverage, snapshot và artifact READY. Cleanup kiểm reference và giữ verified artifact.

### Source Coverage Group — schema logical đề xuất

MVP chọn coverage theo toàn bộ source Segment; character range có thể lưu như chú thích tùy chọn, không dùng để chia quyền sở hữu source hay chứng minh coverage. Mỗi revision tạo group ID mới, không sửa group của revision cha. Các trường bên dưới NOT NULL trừ khi ghi `?`; mọi FK dùng `ON DELETE RESTRICT`.

| Bảng | Khóa và ràng buộc |
|---|---|
| `CoverageGroup` | `group_id` PK, `project_id`, `revision_id`, `review_status` ∈ {PENDING, REVIEWED}, `disposition` ∈ {INCLUDED, SKIPPED}, `skip_reason?`; UNIQUE(project_id,revision_id,group_id); composite FK (project_id,revision_id) → TranscriptRevision(project_id,id); CHECK `((disposition='SKIPPED' AND skip_reason IS NOT NULL AND length(trim(skip_reason))>0) OR (disposition='INCLUDED' AND skip_reason IS NULL))`. |
| `CoverageGroupSource` | project_id, revision_id, group_id, source_id, ordinal; PK(revision_id,source_id), UNIQUE(group_id,ordinal), CHECK(ordinal >= 0); composite FK (project_id,revision_id,group_id) → CoverageGroup(project_id,revision_id,group_id), FK (project_id,source_id) → Segment(project_id,id). PK ngăn một source xuất hiện hai lần trong cùng revision, kể cả giữa hai group. |
| `CoverageGroupMember` | project_id, revision_id, group_id, row_id, ordinal; PK(revision_id,row_id), UNIQUE(group_id,ordinal), CHECK(ordinal >= 0); composite FK tới group như trên và (project_id,revision_id,row_id) → RevisionSegment(project_id,revision_id,row_id). Mỗi revision segment thuộc tối đa một group. |

Parent keys được nêu rõ là UNIQUE trong các bảng đích, kể cả `Segment(project_id,id)` và `TranscriptRevision(project_id,id)`. Validation trong cùng transaction trước accept kiểm source IDs đúng ASR run, tập source của các group bằng tập Segment gốc, mỗi group có ít nhất một source và member, thứ tự liên tục, mọi revision segment có đúng một membership, và group đã REVIEWED. Group INCLUDED yêu cầu từng member được ánh xạ đúng một Translation Unit; group SKIPPED yêu cầu lý do, không member nào được dịch. Mapping Unit chỉ validate khi tạo/chấp nhận translation, sau khi transcript revision đã accept; thiếu source/member hay mapping trùng đều block. Ràng buộc UNIQUE(translation_run_id,row_id) ở bảng mapping ngăn một member thuộc hai Unit trong cùng run. Review group xác nhận provenance/coverage, không chứng minh nội dung dịch đúng nghĩa.

Merge các group chỉ trong draft: kiểm cùng revision/project, union source IDs; nếu hai group có source ID trùng thì reject và rollback, không âm thầm deduplicate. Xóa membership draft cũ và tạo group mới cùng nguồn/member đã merge trong một transaction; accepted snapshot vẫn bất biến. Không làm automatic semantic alignment.
Chọn bảng run chuyên biệt theo `docs/03`, tránh bảng Run chung và inheritance khi MVP chỉ có vài stage. Tái dùng các trường contract qua domain type; registry artifact chỉ thêm nếu chứng minh cần FK và traversal. Hạn chế bảng edit log nếu revision snapshot đủ; quyết định retention do PO duyệt.

## 4. Quan hệ entity

`Project → MediaAsset → TranscriptionRun → Segment → CoverageGroup ← RevisionSegment → TranslationUnit → TranslationRevision → TTSRun → TTSArtifact → SyncPlan → RenderRun → MediaAsset`. CoverageGroup thuộc TranscriptRevision; Run mới liên kết artifact qua TTSRunUnit. Mỗi source thuộc đúng một group trong revision sau validation; group có ordered members theo §6. Overlap source speakers là timing hợp lệ và không thay đổi uniqueness của source coverage.

## 5. Constraints và invariants

SQLite foreign_keys ON mỗi connection, WAL trên ổ local, busy_timeout và migration version; một supervisor claim bằng transaction ngắn. Timestamps là integer microseconds, audio sample count integer; không làm tròn PTS VFR tùy tiện. Các snapshot accepted không overwrite; DB constraint hỗ trợ nhưng validation cross-row thực hiện trong transaction. Mỗi artifact có ID, fingerprint version, provenance, hash và verification; READY không đồng nghĩa active hay user accepted. TTS không thay lỗi bằng silence hoặc truncate; render phải FFprobe và so effective timestamps/sample lengths trước publish. Không dùng dữ liệu test giả làm bằng chứng thực tế.

## 6. Transcript revision và lineage

ASR Segment ID thuộc run gốc bất biến. Mỗi RevisionSegment có `row_id` PK mới cho mỗi revision và `stable_segment_key` giữ khi sửa text/timing. Split/merge tạo keys mới và lưu parent keys, source IDs. Parent revision là snapshot được clone; accepted revision đã duyệt, active pointer chuyển qua CAS. ASR và accepted revisions giữ nguyên.

Ví dụ ASR S1/S2/S3, revision sau có parent revision trước; K là stable key, row IDs khác toàn cục:

| Revision | row_id:stable key | parent key | source mapping | trạng thái |
|---|---|---|---|---|
| R1 | a1:K1, a2:K2, a3:K3 | — | S1,S2,S3 | accepted lịch sử |
| R2 | b1:K1, b2:K2, b3:K3 | K1,K2,K3 | S1,S2,S3 | accepted, b2 sửa text |
| R3 | c1:K1, c2:K2a, c3:K2b, c4:K3 | K1,K2,K2,K3 | S1, group S2, group S2, S3 | accepted sau review |
| R4 | d1:K1, d2:K4, d3:K3 | K1,K2a+K2b,K3 | S1,S2,S3 | accepted active |

Row IDs riêng tránh xung đột PK. Group S2 được tính coverage một lần; hai child là thành viên có thứ tự, không sở hữu trùng S2. Group cần user review; cả hai child được dịch hoặc cùng SKIPPED có lý do; translation mapping phủ mọi member đúng một lần. Nếu không thể đáp ứng, giữ draft và block acceptance/translation. Đề xuất chờ PO duyệt.

Ví dụ theo schema §3: revision R1 có G1→S1/member K1, G2→S2/member K2, G3→S3/member K3. R2 clone thành group IDs mới H1, H2, H3; H2 có một `CoverageGroupSource` S2 và hai `CoverageGroupMember` (K2a, ordinal 0), (K2b, ordinal 1). H2 PENDING không được accept tự động; sau REVIEWED, hai member cùng INCLUDED và mỗi member dịch đúng một lần, hoặc cả group SKIPPED kèm lý do. R1 không đổi. Để đơn giản MVP, không phân chia source ownership bằng character range ngay cả khi có boundary; có thể lưu range làm chú thích, không thay uniqueness.

Source text gốc khác edited text; coverage chứng minh source được xử lý, không chứng minh semantic equivalence. Source character ranges chỉ là chú thích khi boundary được chứng minh; timing boundary do user chọn và kiểm tra, không suy ra từ text offset. Sau rewrite, split giữ shared coverage group và review, không tự đoán. API dùng Unicode scalar code point offsets của text snapshot chính xác; frontend đổi UTF-16 sang code point, reject surrogate lẻ và split giữa surrogate pair. UI chọn grapheme cluster boundary; backend validate against snapshot và segmentation version. Không yêu cầu alignment tự động.

```text
create_transcript_revision(base, expected_lock_version, edits):
  BEGIN IMMEDIATE; CAS draft lock_version; clone rows với row_id mới, giữ stable keys
  apply edits; validate ownership, time, group coverage; COMMIT
split_segment(draft, row, split_point, expected_lock_version):
  CAS draft; tạo hai row_id và stable keys mới, parent key cũ
  giữ source IDs trong group, thêm hai member theo thứ tự; đặt review PENDING
  validate group membership, timing; COMMIT
merge_segments(draft, rows, expected_lock_version):
  CAS draft; require ordered compatible members; tạo row/key mới, ordered parents
  reject source trùng giữa các group; union nguồn, tạo group mới cần review; validate; COMMIT
validate_source_coverage(revision, optional_translation_mapping):
  mỗi source ID thuộc đúng một group trong revision
  kiểm đủ ordered members, disposition, review; reject missing/duplicate/foreign
  nếu có translation mapping: mỗi included member đúng một unit hoặc skip theo group policy
accept_revision_if_current(project, draft, expected_active, expected_lock_version):
  BEGIN IMMEDIATE; validate snapshot; CAS active pointer và draft.lock_version
  freeze accepted snapshot, update pointer; COMMIT; mismatch ROLLBACK, giữ draft đã lưu
```
Job cũ chỉ publish historical artifact nếu hợp lệ, activation CAS thất bại. Transaction revision commit đầy đủ hoặc rollback; draft đã lưu trước đó vẫn giữ.
## 7. Artifact fingerprints

Đề xuất canonical JSON version 1: UTF-8, keys sắp xếp theo Unicode code point, array giữ thứ tự, UUID lowercase canonical, integer base 10; boolean/null chuẩn JSON; không NaN/Infinity, không float cho thời gian/config ảnh hưởng kết quả (dùng integer đơn vị hoặc decimal string canonical). Chuỗi preserve chính xác code points; normalization chỉ áp dụng tại input theo version rõ ràng và không làm mất accepted text. Hash SHA-256 trên bytes canonical; include schema/normalization version, ordered input IDs và content hashes khi identity ảnh hưởng mapping, relevant effective config, tool/model/provider version. Không đưa output folder hoặc secret vào render fingerprint. Nếu provider không công bố model version, ghi version client và model ID cùng thông tin version không xác định; cache không đảm bảo provider determinism.

Artifact ID là identity file/snapshot; revision ID là identity phiên bản edit; fingerprint là nội dung/đầu vào tính toán. Không dùng hash một mình làm quyền truy cập hay identity.

```text
compute_fingerprint(kind, ordered_inputs, effective_config, versions):
  return SHA256(canonical_json({kind, schema_version, normalization_version,
      ordered_inputs, relevant_config, tool_model_provider_versions}))
is_artifact_reusable(artifact, expected_fp):
  return artifact.status == READY and artifact.fp == expected_fp and
    path_is_owned_and_present and hash_matches and media_validation_still_valid
```

Revalidate decoder/probe theo policy và khi cần; thiếu file/hash mismatch không reuse, chuyển MISSING/CORRUPT, giữ record và báo user. TTS key gồm logical Unit key trong project, full text, language, voice/rate/pitch/volume/format, provider/model/client/synthesis và normalization versions; không gồm TranslationRevision ID, TranslationRun ID, TTSRun ID hay Artifact ID. Render key gồm source hash, SyncPlan fingerprint, mix/subtitle/codec settings và tool version.

**SyncPlan fingerprint**, theo canonical JSON §7 và fingerprint_version, gồm source asset identity/hash, timeline origin, duration và time base/PTS policy có ảnh hưởng; danh sách ordered logical Unit keys; với từng Unit là source start_us/end_us, audio artifact content hash, measured duration_us, sample rate/count và channel layout khi sử dụng, manual offset/timing overrides; cùng gap/overlap constraints, các quyết định đã duyệt, sync algorithm/config/version. Array giữ thứ tự, null và default được biểu diễn nhất quán. Audio hash và policy chung đơn lẻ không đủ. Validate artifact/ownership độc lập với hash; approval và conflict status được kiểm lại trước render.

## 8. Dependency invalidation

Graph: `source → ASR → transcript revision → translation units/revision → TTS per unit → sync → render → export`. Fingerprint theo dependency thực, không chỉ traversal theo mọi revision ID ở cấp project; mapping provenance kiểm riêng để tránh một edit Unit làm stale các Unit khác.

| Thay đổi | Stale/tạo mới | Giữ lại |
|---|---|---|
| Một translation Unit | TTS Unit đó, sync và render phụ thuộc | TTS Unit khác |
| Voice/rate một Unit | TTS Unit đó, sync/render | Unit khác |
| Voice toàn bộ | TTS các Unit có effective voice đổi, sync/render | ASR/translation |
| Subtitle font/color | render | source, ASR, translation, TTS, sync |
| Output folder | export copy | MP4 đã verify và toàn pipeline |
| Source timing Unit đổi, text/voice không đổi | SyncPlan và render phụ thuộc | TTS nếu fingerprint và file hợp lệ; ASR/Gemini nếu input thực tế không đổi |
| ASR model | ASR run mới và descendants | run cũ, source |
| Job cũ hoàn tất | kết quả lịch sử nếu hợp lệ; không activate khi snapshot stale | active revision mới |

```text
invalidate_descendants(changed_inputs):
  recompute effective fingerprints for direct dependents
  mark only mismatches stale; propagate only where actual input changed
  retain all physical files and accepted snapshots
  never activate completed job unless stage input fingerprint/generation and target pointer generation match snapshot via CAS
```

Kịch bản A: start_us Unit 25 từ 10.000.000 sang 12.000.000, text/voice và đầu vào khác giữ nguyên → TTS reuse, SyncPlan fingerprint đổi và tính plan mới, render stale; không gọi ASR/Gemini/TTS chỉ do timing đổi nếu snapshot input của các stage đó không đổi. Translation grouping có thể dùng gaps/timestamps hoặc gửi timing cho Gemini: khi timing làm đổi group, context, prompt hay input mapping thì translation fingerprint đổi và cần tính lại; nếu các input đó giữ nguyên có thể reuse. Kịch bản B: chỉ subtitle color đổi → TTS và SyncPlan reuse, render fingerprint đổi. Kịch bản C: chỉ output folder đổi → RenderPlan fingerprint giữ nguyên, export/copy từ MP4 verified.

### TTS reuse xuyên revision

TranslationRun ID xác định output Gemini; TranslationUnit ID xác định record output; logical Unit key ổn định khi edit text, đổi khi split/merge mapping. TranslationRevision ID xác định snapshot duyệt, không là input âm thanh. TTSRun ID xác định batch; TTSArtifact ID xác định file immutable do producer run tạo. Fingerprint theo §7 và logical key; mapping và project ownership kiểm riêng.

Logical Unit key là UUID scoped theo Project, liên kết từ TranslationUnit của Run tới các row của TranslationRevision. Final text edit giữ key; split/merge sinh key mới và lưu parent keys. Khi TranslationRun mới tạo mapping khác về ordered source/group membership, tạo key mới; chỉ giữ key khi mapping lineage tương đương đã được kiểm chứng, không gán lại cho nội dung nguồn khác. Thay revision toàn cục không tự đổi key. TTSRun mới chỉ tham chiếu artifact cùng project khi logical key, fingerprint/version và file verification đều khớp.

Ví dụ 100 Unit: revision B chỉ đổi final_text Unit 25, 99 logical keys/config/text còn nguyên. TTSRun B tạo 99 association tới artifact READY từ Run A sau kiểm hash/media và fingerprint; Unit 25 tạo artifact mới. Không gọi ASR/Gemini, không xóa audio A; sync tính lại và render phụ thuộc stale. Nếu B không còn active lúc hoàn tất, kết quả B READY lịch sử, activation CAS thất bại.

```text
resolve_tts_artifacts_for_revision(project, accepted_revision, config):
  assert revision accepted và mapping complete; snapshot stage input fingerprint/generation and target pointer generation
  create TTSRun for revision
  for unit in revision ordered units:
    expected = audio_fingerprint(logical_key, final_text, language, effective_config, versions)
    candidate = same-project READY artifact matching logical key, fp, fingerprint_version
    if candidate and verify_file_hash_media(candidate):
      transaction: insert TTSRunUnit reference after rechecking status and membership
    else: insert pending association; queue synthesis for unit
  activate only with CAS against snapshot after required outputs verified
```
Không giữ DB transaction trong hash/decode; recheck record status khi commit, file đổi sau commit xử lý corruption/revalidation.
## 9. Atomic publication

Worker ghi scratch, flush/close; xác minh hash, decode, duration/sample count và metadata; render phải FFprobe và so effective timestamps/sample lengths. Rename immutable cùng volume; cross-volume copy temp tại đích, flush/verify rồi rename, không overwrite. Không giữ transaction trong network, inference, hash hay FFmpeg. Sau rename, một transaction ngắn lưu artifact và thử CAS activation:

```sql
BEGIN IMMEDIATE;
-- validate attempt/manifest/ownership; insert artifact READY idempotently
INSERT verified_artifact (...);
-- completion chỉ khi cancellation chưa thắng; lưu kết quả/attempt SUCCEEDED
UPDATE stage_activation SET active_result_id = :result, pointer_generation = pointer_generation + 1
 WHERE project_id = :project AND stage = :stage
   AND input_generation = :expected_input_generation
   AND input_fingerprint = :expected_input_fingerprint
   AND pointer_generation = :expected_pointer_generation;
-- changes() = 0: lưu activation_outcome = 'STALE'; KHÔNG ROLLBACK
-- changes() = 1: lưu activation_outcome = 'ACTIVE'
COMMIT;
```
Đây là pseudocode, không phải SQL executable; `stage_activation` biểu diễn state theo stage, có UNIQUE(project_id,stage), FK tới project và pointer typed cùng project. Input generation tăng trong cùng transaction với thay đổi dependency liên quan; input fingerprint tính từ snapshot thực tế. Pointer generation tăng khi active result của stage thay đổi, ngăn job cũ ghi đè lựa chọn mới ngay cả khi input giống nhau. Rename Project chỉ tăng `Project.lock_version` để chống ghi đè metadata, không tăng stage input/pointer generation, nên TTS Job phù hợp vẫn activate được; các thay đổi input liên quan phải tăng input generation atomically. `lock_version` vẫn dùng cho edit Project, không dùng làm điều kiện activation toàn cục. Nếu fingerprint và generation không được cập nhật nhất quán trong transaction thì CAS không an toàn; CORE-01 phải kiểm thử invariant này.

Zero-row CAS vẫn commit artifact READY lịch sử và attempt thành công nếu validation/cancel cho phép; A không trở lại active. Job success là tính toán, artifact READY là integrity, stale so với active snapshot, active là pointer riêng. Crash trước commit rollback cả insert/CAS và D3 xử lý orphan; sau commit record và outcome cùng tồn tại, D4 revalidate. SQL error thật rollback. Transaction không bao phủ filesystem rename.
## 10. Crash recovery

| Điểm crash | File / DB | Reconciliation, reuse và điều kiện |
|---|---|---|
| D1 đang ghi temp | temp không đầy đủ; job RUNNING | đánh INTERRUPTED; temp chỉ là candidate, retry; không READY, giữ source/output cũ |
| D2 verify trước rename | temp có thể đầy đủ; DB chưa publish | xác minh lại hash/media và snapshot, sau đó resume hoặc retry; không suy luận READY từ tên file |
| D3 rename trước commit | immutable file mồ côi; DB không READY | inventory theo manifest/attempt, xác minh identity/hash/media rồi gắn lại bằng transaction nếu chứng minh được; nếu không quarantine và review; không xóa source/output |
| D4 sau commit | file và DB có thể đã READY | đối chiếu existence/hash; chỉ giữ READY khi hợp lệ; interrupted job reconcile theo kết quả đã commit |
| D5 READY rồi mất/hỏng | record READY nhưng file thiếu/hash sai | chuyển MISSING/CORRUPT, bỏ khả năng reuse/active, báo user; retry nếu có input, không xóa record |
| D6 copy sang volume khác bị ngắt | temp đích partial; bản nguồn vẫn tồn tại | kiểm hash toàn bộ trước rename, retry copy; temp không READY; không overwrite output cũ |
| D7 dependency stage đổi khi job xong | file verified, DB kết quả lịch sử | input generation/fingerprint hoặc pointer generation mismatch; CAS thất bại, stale, không active; reuse chỉ khi fingerprint và mapping hợp lệ |

Để D3 có thể nhận diện an toàn cần manifest nhỏ gắn job/attempt, destination ID, fingerprint, hash và verification version trước rename; manifest cũng là candidate, không phải bằng chứng READY. Nếu không chứng minh được liên kết thì quarantine, không tự nhận success. Không tự động xóa source, accepted edits hay verified outputs.

## 11. Job / Run / Artifact state machines

| Entity / From | Event | Guard | To | Side Effects |
|---|---|---|---|---|
| Job QUEUED | claim | một supervisor, BEGIN IMMEDIATE | RUNNING | insert RUNNING attempt cùng commit |
| Job QUEUED | cancel | CAS QUEUED | CANCELLED | không tạo attempt |
| Job RUNNING | cancel | CAS RUNNING | CANCEL_REQUESTED | signal worker, timeout dừng process tree |
| Job CANCEL_REQUESTED | dừng xong | process đã dừng | CANCELLED | attempt CANCELLED; giữ verified artifacts |
| Job CANCEL_REQUESTED | lỗi trong hủy | process đã dừng | CANCELLED | lưu sanitized error; nếu chưa dừng giữ CANCEL_REQUESTED, báo lỗi hủy |
| Job RUNNING | complete | required outputs verified, transaction thắng race | READY | attempt SUCCEEDED; activation CAS riêng |
| Job CANCEL_REQUESTED | complete race | cancel commit trước completion | CANCEL_REQUESTED | transaction riêng lưu artifact verified lịch sử khi đủ evidence, không activate; kết thúc cancel thành CANCELLED |
| Job READY | cancel race | completion commit trước cancel | READY | trả already completed |
| Job RUNNING/CANCEL_REQUESTED | restart | owner chết và reconcile | INTERRUPTED | attempt INTERRUPTED, reconcile candidates |
| Job INTERRUPTED | retry | reconciliation xong, eligible | QUEUED | attempt mới khi claim |
| Job FAILED | explicit retry | retryable, input hợp lệ | QUEUED | giữ attempt cũ |
| Job RUNNING | failure | failure được phân loại | FAILED | attempt FAILED |
| Attempt RUNNING | finish/fail/cancel/restart | job transition tương ứng | SUCCEEDED/FAILED/CANCELLED/INTERRUPTED | retry tạo attempt mới |
| Run PENDING | start | snapshot hợp lệ | RUNNING | input snapshot bất biến |
| Run RUNNING | all verified | coverage đầy đủ | READY | có thể stale; activation riêng |
| Run RUNNING | mixed Unit failures | một required Unit lỗi và một READY | PARTIAL | Unit READY playable; run không READY |
| Run RUNNING | failure/review/cancel/restart | tương ứng | FAILED/REVIEW_REQUIRED/CANCELLED/INTERRUPTED | giữ output verified |
| Artifact STAGING | verify | hash, decode, metadata pass | VERIFIED | lưu evidence candidate |
| Artifact VERIFIED | publish | immutable path và DB commit | READY | eligible reuse, không mặc định active |
| Artifact STAGING/VERIFIED | error | validation/publication lỗi | FAILED | không READY |
| Artifact READY | mất/hỏng | kiểm tra thất bại | MISSING/CORRUPT | chặn reuse/active; giữ record |
| Artifact MISSING/CORRUPT | restore | xác minh lại đầy đủ | READY | activation phải CAS lại |

Mọi transition khác bị từ chối. Job READY và artifact stale có thể cùng tồn tại. User review DRAFT/REVIEW_REQUIRED/ACCEPTED riêng; accepted snapshot bất biến. Duplicate key cùng payload trả job hiện có, payload khác conflict. Retry có giới hạn; không lease/heartbeat. Sync conflict chưa duyệt block render.
## 12. Test specifications

Tất cả **NOT RUN**, dự định dùng fixture có kiểm soát; runtime/provider thật được nghiệm thu riêng. TC tham chiếu `docs/10`.

| ID / stage | Preconditions | Action | Expected / invariant |
|---|---|---|---|
| A01 ASR-01 | accepted revision | edit một segment, save | source không đổi, draft đúng mapping; bảo vệ provenance |
| A02 ASR-01 | source có text | split rồi merge | group coverage đầy đủ không trùng, parents đúng; lineage |
| A03 TRANS-01 / TC-05 | 500 segments | group/skip | mỗi source covered đúng một lần hoặc group skip lý do; không mất ID |
| A04 CORE-01 / TC-07 | hai client cùng version | save đồng thời | một thành công, một conflict; không mất draft |
| A05 CORE-01 | job snapshot revision cũ | accept revision mới rồi kết thúc job | không đổi active pointer; stale result giữ provenance |
| A06 TTS-01 / TC-11 | nhiều verified Units | sửa một Unit | chỉ Unit đó regenerate; descendants stale |
| A07 CORE-01 | cùng key/payload | gửi job hai lần | cùng job; key khác payload conflict |
| A08 CORE-01 / TC-16 | READY file | xóa hoặc đổi bytes | MISSING/CORRUPT, không reuse hoặc READY |
| A09 CORE-01 / TC-16,17 | publication từng phase | inject crash D1–D7 và restart | kết quả như §10; không false READY, output cũ còn |
| A10 TTS-01 / TC-08,10 | một số Units verified | dừng giữa TTS, restart | reconcile verified Units, retry thiếu; run PARTIAL |
| A11 RENDER-01 / TC-23 | render verified | đổi subtitle style | chỉ render stale |
| A12 RENDER-01 | MP4 verified | đổi output folder | chỉ export lại, hash đối chiếu |
| A13 CORE-01 | DB FK ON | negative/đảo timestamps, FK sai project | reject và rollback; không dữ liệu mồ côi |
| A14 SYNC-01 / TC-12 | overflow/overlap | yêu cầu render chưa duyệt | block; conflict hiển thị |
| A15 TTS-01 / TC-09 | text dài, lỗi provider | tổng hợp | giữ toàn bộ mapping hoặc review, không truncate/silence giả |

### R1 test vectors — tất cả NOT RUN

| ID / stage | Input / action | Expected / invariant |
|---|---|---|
| R1-01 ASR-01 | R1→R2 edit K2 | row IDs khác, K2 ổn định, ASR và R1 nguyên vẹn |
| R1-02 ASR-01 | rewrite rồi split/merge | group reviewed hoặc block; coverage đúng một lần, parent keys đầy đủ |
| R1-03 TTS-01 | 100 verified Units, sửa Unit 25 | 99 hits, 1 synthesis, sync mới/render stale, không gọi ASR/Gemini |
| R1-04 TTS-01 | TTSRun B tham chiếu artifact Run A | FK cùng project, fingerprint/file hợp lệ, không copy hoặc orphan |
| R1-05 CORE-01 | Job A chạy, accept B, A hoàn thành | artifact A READY lịch sử, B active, A stale |
| R1-06 CORE-01 | CAS activation zero rows | commit verified artifact, không rollback, B active |
| R1-07 CORE-01 | cancel và completion đồng thời | transaction thắng trước quyết định trạng thái, không activate sau cancel thắng |
| R1-08 CORE-01 | association cross-project | composite FK reject, rollback |
| R1-09 CORE-01 | cùng key cùng/khác payload | cùng job / conflict, không duplicate |
| R1-10 ASR-01 | emoji, combining mark, UTF-16 offsets | conversion đúng, reject boundary sai, mapping nguyên vẹn |

### R2 test specifications — tất cả NOT RUN

| ID / stage | Preconditions và action | Expected / invariant |
|---|---|---|
| R2-01 SYNC-01 | Unit 25 start 10.000.000→12.000.000 us, text/voice/input khác không đổi | TTS reuse; Sync fp đổi, plan mới, render stale; không gọi lại ASR/Gemini/TTS |
| R2-02 RENDER-01 | chỉ đổi subtitle color | TTS/Sync reuse, render fp đổi |
| R2-03 RENDER-01 | chỉ đổi output folder | RenderPlan fp giữ nguyên; export lại MP4 verified |
| R2-04 ASR-01 | S2 split K2a/K2b trong một group đã review | một source row S2, hai member thứ tự 0/1; R1 bất biến |
| R2-05 ASR-01 | insert source trùng hoặc bỏ sót source | UNIQUE reject trùng; validator reject thiếu trước accept |
| R2-06 ASR-01 | group PENDING, thử accept | block, draft giữ nguyên |
| R2-07 ASR-01 | merge groups có source trùng | reject/rollback, không mất coverage |
| R2-08 CORE-01 | rename Project trong khi TTS job hoàn thành, stage inputs/pointer không đổi | metadata giữ tên mới; activation thành công, không false stale |
| R2-09 TRANS-01 | edit text rồi split/merge Unit | edit giữ logical key; split/merge tạo keys mới, lineage giữ, không gán key cho mapping khác |
| R2-10 CORE-01 | group source hoặc member thuộc project khác | composite FK reject, rollback |

### Proposed changes cho canonical specs sau phê duyệt

Đề xuất cập nhật `docs/03_DATA_AND_STORAGE.md` với schema group, Unit key và stage activation; `docs/04_PIPELINE_ALGORITHMS.md` với coverage/group review và fingerprint theo timing; `docs/07_JOBS_AND_API.md` với activation CAS; `docs/13_RENDER_TIMELINE_SPEC.md` với SyncPlan fingerprint; bổ sung test vào `docs/10_QA_SECURITY_RECOVERY.md`. Chưa sửa các tài liệu này.
## 13. Trade-offs

Snapshot revision tốn storage hơn event log nhưng dễ rollback và đối chiếu CAS. Coverage group giảm thao tác alignment sau rewrite nhưng cần review và hạn chế mapping; không chứng minh semantic equivalence. Bảng run chuyên biệt đơn giản hơn generic inheritance nhưng lặp metadata. Hash file đầy đủ tốn I/O; tránh rehash vô điều kiện theo policy được review nhưng phải xác minh trước reuse đáng tin. Manifest và reconciliation giải quyết crash phổ biến với một supervisor, không giả lập atomicity xuyên filesystem/DB.

## 14. PENDING OWNER DECISION

PO: chính sách chấp nhận overlap có warning/listening preview theo `docs/13` hay luôn block; cách user chỉ định split mapping khi sửa tự do; thời gian giữ revision và các output cũ; phạm vi skip có chủ đích. Technical Lead: encoding interval cho Unicode và text rỗng; schema mapping typed/FK; mức revalidation/hash; crash durability của Windows rename/flush và cross-volume; retry/cancel race; tolerance FFprobe, PTS/sample counts; versioning khi provider không công bố model version. `docs/04` cho phép manual accept overlap/overflow, trong khi `docs/13` chỉ nói overlap theo policy và yêu cầu preview; mặc định block đến khi policy được phê duyệt. Không sửa canonical specs trong task này.

## 15. Handoff CORE-01

Chốt các câu hỏi trên, API error/CAS/idempotency, lifecycle và publication manifest trước migration. Viết test A04, A05, A07–A09, A13 trước provider thật; ASR/translation/TTS/sync/render tests triển khai ở stage tương ứng. Kiểm tra SQLite WAL local, process tree Windows, Unicode/long path, FFmpeg/FFprobe và hardware thực trên môi trường mục tiêu. ARCH-00B cần snapshot contracts, trusted selection boundary và error/state semantics ở đây để thiết kế API; đây là đề xuất, không phải approval hay implementation.
