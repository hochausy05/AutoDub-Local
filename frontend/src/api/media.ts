import { currentCsrfToken } from "./session";

export type MediaAsset = {
  id: string;
  mode: "COPY" | "LINK";
  status: string;
  lock_version: number;
  display_name: string;
  sha256: string;
  file_size: number;
  metadata: {
    container: string;
    duration_us: number;
    width: number;
    height: number;
    video_codec: string;
    audio_streams: number;
    frame_rate: string | null;
  };
};

export type ImportJob = { id: string; project_id: string; status: string; error_code: string | null; retry_available?: boolean; retry_block_reason?: string | null; attempt?: { status: string; retryable: boolean; retry_after_at?: string | null } | null; progress?: { phase?: string; completed_units?: number; total_units?: number; fraction?: number; retry_after_at?: string } | null; attempts?: { status: string; retryable: boolean; retry_after_at?: string | null }[] };
export type ProcessingSource = { media_asset_id: string | null; generation: number; status: string; asset: MediaAsset | null };

export type ASRJob = ImportJob & { progress?: { phase?: string } | null };
export type ASRModel = { id: "base" | "small"; approximate_bytes: number; cached: boolean };
export type ASRSegment = { id: string; position: number; start_us: number; end_us: number; source_text: string; avg_logprob: number | null; no_speech_prob: number | null; compression_ratio: number | null; flags: string[] };
export type ASRRunSummary = { id: string; project_id: string; source_asset_id: string; model_id: string; language: string | null; actual_device: string; compute_type: string; status: string; duration_us: number; is_active: boolean; created_at: string };
export type ASRRun = ASRRunSummary & { model_revision: string; input_fingerprint: string; timing: Record<string, unknown>; provenance: { requested_device: string; actual_device: string; compute_type: string; fallback_reason: string | null; language_probability: number | null }; segments: ASRSegment[] };
export type TranscriptSegment = { row_id: string; stable_segment_key: string; position: number; text: string; start_us: number; end_us: number; parent_keys: string[]; coverage_group_id: string | null; coverage_status: "PENDING" | "REVIEWED" | null; source_segment_ids: string[]; source_texts: string[]; timing_warning: boolean };
export type TranscriptRevision = { id: string; project_id: string; run_id: string; source_asset_id: string; parent_revision_id: string | null; revision_no: number; status: "DRAFT" | "ACCEPTED"; lock_version: number; created_at: string; accepted_at: string | null; is_active: boolean; is_current: boolean; active_generation: number; run_status: string; duration_us: number; segments: TranscriptSegment[] };
export type TranslationUnit = { unit_id: string; logical_unit_key: string; position: number; revision_segment_ids: string[]; source_segment_ids: string[]; source_text: string; start_us: number; end_us: number; pass1_text: string | null; pass2_text: string | null; final_text: string | null; final_notes: string[]; status: string; error_code: string | null; review_flags: string[] };
export type TranslationRun = { id: string; project_id: string; producer_job_id: string; transcript_revision_id: string; source_asset_id: string; provider: "cloudflare" | "gemini"; model_id: string; status: string; pass_strategy: "SINGLE_PASS" | "TWO_PASS"; error_code: string | null; is_active: boolean; units: TranslationUnit[] };
export type TranslationRevisionUnit = Pick<TranslationUnit, "unit_id" | "logical_unit_key" | "position" | "revision_segment_ids" | "source_segment_ids" | "source_text" | "start_us" | "end_us"> & { final_text: string; unit_fingerprint: string; text_origin: "PROVIDER_OUTPUT" | "USER_EDIT" };
export type TranslationRevision = { id: string; project_id: string; run_id: string; parent_revision_id: string | null; source_revision_id: string | null; source_kind: "PROVIDER_OUTPUT" | "ACCEPTED_REVISION"; revision_no: number; status: "DRAFT" | "ACCEPTED"; lock_version: number; fingerprint: string; fingerprint_version: number; provenance: Record<string, unknown>; created_at: string; accepted_at: string | null; is_active: boolean; units: TranslationRevisionUnit[] };
export type TranslationEditorState = { project_id: string; run_id: string; source_asset_id: string; transcript_revision_id: string; run_is_current: boolean; active_revision_id: string | null; active_generation: number; original_units: TranslationRevisionUnit[]; draft: TranslationRevision | null; accepted: TranslationRevision | null; latest_accepted: TranslationRevision | null };

async function checked(response: Response) {
  if (response.ok) return;
  let message = `API trả HTTP ${response.status}.`;
  try {
    const body = await response.json() as { error?: { code?: string; message?: string } };
    const codes: Record<string, string> = {
      picker_unavailable: "Không tìm thấy Windows picker hoặc helper không khả dụng.",
      picker_timeout: "Hộp thoại chọn file đã hết thời gian chờ.",
      picker_failed: "Windows picker không hoàn tất việc chọn file.",
      retry_cooldown: "Hãy chờ hết thời gian tạm dừng trước khi thử lại.",
      selection_cancelled: "Bạn đã hủy chọn video.",
      selection_expired: "Phiên chọn file đã hết hạn. Hãy chọn lại video.",
      selection_consumed: "Selection đã được dùng. Hãy chọn lại nếu muốn tạo Import Job khác.",
      source_changed: "File nguồn đã thay đổi. Hãy chọn lại file và thử lại.",
      source_unavailable: "Không tìm thấy hoặc không thể đọc file nguồn.",
      ffprobe_unavailable: "Không tìm thấy FFprobe. Cài FFmpeg hoặc cấu hình FFPROBE_PATH.",
      ffprobe_timeout: "FFprobe xử lý quá lâu; Import Job đã dừng.",
      media_corrupt: "Video bị hỏng hoặc không đầy đủ.",
      media_unsupported: "FFprobe không hỗ trợ định dạng hoặc codec của video này.",
      insufficient_disk_space: "Không đủ dung lượng trống cho chế độ COPY.",
      asset_unavailable: "Video hiện không sẵn sàng. Hãy kiểm tra nguồn rồi thử lại.",
      asset_changed: "Video đã thay đổi kể từ lần xác minh gần nhất.",
      relink_content_mismatch: "Video được chọn có nội dung khác. Không thể thay thế nguồn đã xác minh.",
      asset_revision_conflict: "Asset vừa được cập nhật ở nơi khác. Tải lại danh sách rồi thử lại.",
      model_download_consent_required: "Model chưa có trong bộ nhớ cục bộ. Cho phép tải model để tiếp tục.",
      model_download_failed: "Không tải được model. Kiểm tra kết nối Internet rồi thử lại.",
      insufficient_model_disk_space: "Không đủ dung lượng trống để tải dữ liệu nhận diện giọng nói.",
      processing_source_unselected: "Hãy chọn một video làm video xử lý trước.",
      source_selection_conflict: "Danh sách video vừa thay đổi. Tải lại rồi chọn lại video.",
      faster_whisper_unavailable: "Faster-Whisper chưa được cài trong môi trường Python.",
      no_audio_stream: "Video không có luồng âm thanh.",
      audio_empty: "Luồng âm thanh rỗng.",
      ffmpeg_unavailable: "Không tìm thấy FFmpeg.",
      ffmpeg_extraction_failed: "FFmpeg không thể giải mã luồng âm thanh.",
      cuda_unavailable: "CUDA không khả dụng và CPU fallback đang tắt.",
      cuda_initialization_failed: "CUDA khởi tạo thất bại; CPU fallback không thành công.",
      asr_inference_failed: "Faster-Whisper không hoàn tất nhận diện.",
      transcript_revision_conflict: "Bản nháp đã thay đổi ở nơi khác. Nội dung bạn đang nhập vẫn được giữ; tải phiên bản mới và so sánh trước khi lưu lại.",
      translation_revision_conflict: "Bản dịch đã được cập nhật ở nơi khác. Nội dung đang nhập vẫn được giữ; tải lại để đối chiếu.",
      translation_active_revision_conflict: "Bản đã duyệt vừa thay đổi. Tải lại trạng thái rồi kiểm tra trước khi duyệt tiếp.",
      translation_run_stale: "Kết quả dịch đã cũ do nguồn hoặc Run thay đổi. Hãy tải lại kết quả hiện hành.",
      translation_revision_coverage_invalid: "Không thể xác minh đầy đủ các Unit của bản dịch.",
      translation_unit_not_found: "Một Unit không thuộc bản dịch hiện hành. Bản nháp chưa được lưu.",
      duplicate_translation_unit: "Danh sách thay đổi có Unit bị lặp. Không có thay đổi nào được lưu.",
      invalid_translation_text: "Nội dung cần có chữ hợp lệ và không được để trống.",
      translation_revision_immutable: "Bản đã duyệt chỉ có thể xem. Hãy sửa bản nháp mới.",
      transcript_active_revision_conflict: "Một phiên bản khác vừa được duyệt. Tải lại lịch sử rồi kiểm tra trước khi duyệt tiếp.",
      transcript_input_stale: "Video hoặc kết quả nhận diện đã thay đổi. Bản nháp được giữ lại nhưng cần kiểm tra trên kết quả hiện hành.",
      transcript_review_required: "Hãy kiểm tra và xác nhận các câu vừa tách hoặc gộp trước khi duyệt.",
      transcript_source_coverage_invalid: "Lời thoại chưa bao phủ đầy đủ nội dung nhận diện. Bản nháp chưa được duyệt.",
      transcript_member_coverage_invalid: "Có câu chưa được nối đúng với nội dung nhận diện. Bản nháp chưa được duyệt.",
      invalid_transcript_timing: "Thời điểm phải nằm trong video và thời điểm kết thúc phải sau thời điểm bắt đầu.",
      invalid_split_timing: "Thời điểm chia phải nằm giữa thời điểm đầu và cuối của câu.",
      unsafe_unicode_split: "Vị trí chia nằm giữa một ký tự Unicode. Hãy chọn vị trí khác.",
      transcript_revision_immutable: "Phiên bản đã duyệt chỉ có thể xem. Hãy tạo bản nháp mới để chỉnh sửa.",
      accepted_transcript_required: "Hãy duyệt lời thoại trước khi dịch.",
      accepted_transcript_stale: "Video hoặc lời thoại đã thay đổi. Hãy tải lại phiên bản hiện hành.",
      translation_review_required: "Hãy hoàn tất việc kiểm tra lời thoại trước khi dịch.",
      translation_unit_coverage_invalid: "Không thể xác minh đầy đủ các câu lời thoại.",
      translation_no_units: "Không có câu lời thoại cần dịch.",
      translation_unit_too_long_review_required: "Một đoạn quá dài để gửi an toàn. Hãy chia lời thoại thành các câu nhỏ hơn.",
    };
    const code = body.error?.code;
    message = code ? `${codes[code] || body.error?.message || "Đã xảy ra lỗi."} (${code})` : body.error?.message || message;
  } catch { /* status fallback */ }
  throw new Error(message);
}

export async function chooseSource(projectId: string) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/projects/${projectId}/selections`, {
    method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, body: "{}",
  });
  await checked(response);
  return await response.json() as { selection_id: string; display_name: string; expires_in_seconds: number };
}

export async function importSelected(projectId: string, selectionId: string, mode: "COPY" | "LINK") {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/projects/${projectId}/media/imports`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ selection_id: selectionId, mode }),
  });
  await checked(response);
  return await response.json() as ImportJob;
}

export async function getImportJob(jobId: string, projectId: string) {
  const response = await fetch(`/api/v1/jobs/${jobId}?project_id=${projectId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as ImportJob;
}

export async function cancelLocalJob(jobId: string, projectId: string) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/jobs/${jobId}/cancel?project_id=${projectId}`, {
    method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: "{}",
  });
  await checked(response);
  return await response.json() as ImportJob;
}

export async function retryLocalJob(job: ImportJob) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  if (job.status !== "FAILED" && job.status !== "INTERRUPTED") throw new Error("Job hiện không thể chạy lại.");
  const response = await fetch(`/api/v1/jobs/${job.id}/retry?project_id=${job.project_id}`, {
    method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ expected_status: job.status }),
  });
  await checked(response);
  return await response.json() as ImportJob;
}

export async function fetchMedia(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/media`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  const body = await response.json() as { items: MediaAsset[] };
  return body.items;
}

export async function fetchProcessingSource(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/processing-source`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as ProcessingSource;
}

export async function selectProcessingSource(projectId: string, mediaAssetId: string, expectedGeneration: number) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/projects/${projectId}/processing-source`, {
    method: "PUT", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
    body: JSON.stringify({ media_asset_id: mediaAssetId, expected_generation: expectedGeneration }),
  });
  await checked(response);
  return await response.json() as ProcessingSource;
}

export function mediaStreamUrl(projectId: string, assetId: string) {
  return `/api/v1/projects/${projectId}/media/${assetId}/stream`;
}

export async function relinkMedia(projectId: string, asset: MediaAsset) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const selectionResponse = await fetch(`/api/v1/projects/${projectId}/media/${asset.id}/relink-selections`, {
    method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, body: "{}",
  });
  await checked(selectionResponse);
  const selection = await selectionResponse.json() as { selection_id: string };
  const response = await fetch(`/api/v1/projects/${projectId}/media/${asset.id}/relink`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ selection_id: selection.selection_id, expected_lock_version: asset.lock_version }),
  });
  await checked(response);
  return await response.json() as { asset_id: string; status: string; lock_version: number };
}

export async function fetchASRModels() {
  const response = await fetch("/api/v1/asr/models", { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as { items: ASRModel[]; hardware: { cuda_device_count: number; cuda_compute_types: string[]; cpu_compute_types: string[]; probe_error: string | null } };
}

export async function createASRJob(projectId: string, language: "auto" | "en" | "zh", allowModelDownload: boolean) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/projects/${projectId}/asr/jobs`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ language, allow_model_download: allowModelDownload }),
  });
  await checked(response);
  return await response.json() as ASRJob;
}

export async function fetchASRRuns(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/asr/runs`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return (await response.json() as { items: ASRRunSummary[] }).items;
}

export async function fetchASRRun(projectId: string, runId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/asr/runs/${runId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as ASRRun;
}

async function transcriptMutation(path: string, body: unknown) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(body) });
  await checked(response);
  return await response.json() as TranscriptRevision;
}

export async function fetchTranscriptRevisions(projectId: string, assetId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/transcripts?source_asset_id=${assetId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return (await response.json() as { items: TranscriptRevision[] }).items;
}

export async function fetchTranscriptRevision(projectId: string, revisionId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/transcripts/revisions/${revisionId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as TranscriptRevision;
}

export async function createTranscriptDraft(projectId: string, base: { source_run_id: string } | { parent_revision_id: string }) {
  return transcriptMutation(`/api/v1/projects/${projectId}/transcripts/revisions`, base);
}

export async function saveTranscriptDraft(projectId: string, revision: TranscriptRevision) {
  const edits = revision.segments.map(({ row_id, text }) => ({ row_id, text }));
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(`/api/v1/projects/${projectId}/transcripts/revisions/${revision.id}`, { method: "PATCH", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ expected_lock_version: revision.lock_version, edits }) });
  await checked(response);
  return await response.json() as TranscriptRevision;
}

export async function splitTranscriptSegment(projectId: string, revisionId: string, segment: TranscriptSegment, expectedLockVersion: number, splitAtCodepoint: number, splitAtUs: number) {
  return transcriptMutation(`/api/v1/projects/${projectId}/transcripts/revisions/${revisionId}/segments/${segment.row_id}/split`, { expected_lock_version: expectedLockVersion, split_at_codepoint: splitAtCodepoint, split_at_us: splitAtUs });
}

export async function mergeTranscriptNext(projectId: string, revisionId: string, segment: TranscriptSegment, expectedLockVersion: number, mergedText: string) {
  return transcriptMutation(`/api/v1/projects/${projectId}/transcripts/revisions/${revisionId}/segments/${segment.row_id}/merge-next`, { expected_lock_version: expectedLockVersion, merged_text: mergedText });
}

export async function reviewTranscriptGroup(projectId: string, revisionId: string, groupId: string, expectedLockVersion: number) {
  return transcriptMutation(`/api/v1/projects/${projectId}/transcripts/revisions/${revisionId}/coverage-groups/${groupId}/review`, { expected_lock_version: expectedLockVersion });
}

export async function acceptTranscriptRevision(projectId: string, revision: TranscriptRevision, expectedActiveRevisionId: string | null) {
  return transcriptMutation(`/api/v1/projects/${projectId}/transcripts/revisions/${revision.id}/accept`, { expected_lock_version: revision.lock_version, expected_active_revision_id: expectedActiveRevisionId });
}

export async function fetchTranslationRuns(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/translations/runs`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return (await response.json() as { items: TranslationRun[] }).items;
}

export async function fetchTranslationRun(projectId: string, runId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/translations/runs/${runId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as TranslationRun;
}

async function translationRevisionMutation<T>(url: string, method: "POST" | "PATCH", body: unknown) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(url, { method, credentials: "same-origin", headers: {
    "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID(),
  }, body: JSON.stringify(body) });
  await checked(response);
  return await response.json() as T;
}

export async function fetchTranslationEditor(projectId: string, runId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/translations/runs/${runId}/editor`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as TranslationEditorState;
}

export async function createTranslationDraft(projectId: string, runId: string, baseAcceptedRevisionId: string | null) {
  return translationRevisionMutation<TranslationRevision>(`/api/v1/projects/${projectId}/translations/runs/${runId}/drafts`, "POST", { base_accepted_revision_id: baseAcceptedRevisionId });
}

export async function saveTranslationDraft(projectId: string, runId: string, revisionId: string, expectedLockVersion: number, edits: { unit_id: string; text: string }[]) {
  return translationRevisionMutation<TranslationRevision>(`/api/v1/projects/${projectId}/translations/runs/${runId}/drafts/${revisionId}/units`, "PATCH", { expected_lock_version: expectedLockVersion, edits });
}

export async function acceptTranslationDraft(projectId: string, runId: string, revisionId: string, expectedLockVersion: number, expectedActiveRevisionId: string | null) {
  return translationRevisionMutation<TranslationRevision>(`/api/v1/projects/${projectId}/translations/runs/${runId}/drafts/${revisionId}/accept`, "POST", { expected_lock_version: expectedLockVersion, expected_active_revision_id: expectedActiveRevisionId });
}

export async function acceptOriginalTranslation(projectId: string, runId: string, expectedActiveRevisionId: string | null) {
  return translationRevisionMutation<TranslationRevision>(`/api/v1/projects/${projectId}/translations/runs/${runId}/accept-original`, "POST", { expected_active_revision_id: expectedActiveRevisionId });
}
