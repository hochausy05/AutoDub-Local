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

export type ImportJob = { id: string; project_id: string; status: string; error_code: string | null };

async function checked(response: Response) {
  if (response.ok) return;
  let message = `API trả HTTP ${response.status}.`;
  try {
    const body = await response.json() as { error?: { code?: string; message?: string } };
    const codes: Record<string, string> = {
      picker_unavailable: "Không tìm thấy Windows picker hoặc helper không khả dụng.",
      picker_timeout: "Hộp thoại chọn file đã hết thời gian chờ.",
      picker_failed: "Windows picker không hoàn tất việc chọn file.",
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
    };
    message = (body.error?.code && codes[body.error.code]) || body.error?.message || body.error?.code || message;
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

export async function fetchMedia(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/media`, { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  const body = await response.json() as { items: MediaAsset[] };
  return body.items;
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
