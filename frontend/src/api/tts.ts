import { currentCsrfToken } from "./session";
import type { TranslationRevision } from "./media";

export type TTSVoice = { short_name: string; locale: string; gender?: string };
export type TTSArtifact = { id: string; duration_us: number; sample_count: number; sample_rate: number; channels: number; codec: string; container: string };
export type TTSUnit = { unit_id: string; position: number; source_text: string; accepted_text: string; start_us: number; end_us: number; status: string; error_code: string | null; cache_hit: number; artifact: TTSArtifact | null };
export type TTSRun = { id: string; project_id: string; producer_job_id: string; translation_revision_id: string; status: string; job_status: string; job_error_code: string | null; is_active: boolean; config: { voice: string; rate_percent: number }; retry_available: boolean; retry_block_reason: string | null; retry_after_at: string | null; job_progress: { current_round?: number; next_round?: number; round_count?: number; phase?: string; retry_after_at?: string; retry_units?: number } | null; progress: { total_units: number; ready_units: number; reused_units: number; running_units: number; queued_units: number; failed_units: number; review_required_units: number }; units: TTSUnit[] };
export type TTSPreview = { cached: boolean; artifact_id?: string; status: string; job?: { id: string; status: string } };

async function checked(response: Response) {
  if (response.ok) return;
  let message = `API trả HTTP ${response.status}.`;
  try { const body = await response.json() as { error?: { message?: string; code?: string } }; message = body.error?.message ?? body.error?.code ?? message; } catch { /* keep HTTP status */ }
  throw new Error(message);
}

async function mutate(url: string, body: unknown) {
  const csrf = currentCsrfToken();
  if (!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch(url, { method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(body) });
  await checked(response);
  return await response.json();
}

export async function fetchTTSVoices() {
  const response = await fetch("/api/v1/providers/edge-tts/voices", { credentials: "same-origin", cache: "no-store" });
  await checked(response);
  return await response.json() as { voices: TTSVoice[]; default_voice: string | null };
}

export async function fetchTTSRun(projectId: string, runId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/tts/runs/${runId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response); return await response.json() as TTSRun;
}

export async function fetchCurrentTTSRun(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/tts/runs/current`, { credentials: "same-origin", cache: "no-store" });
  await checked(response); return await response.json() as TTSRun | null;
}

export async function fetchTTSActivity(projectId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/tts/activity`, { credentials: "same-origin", cache: "no-store" });
  await checked(response); return await response.json() as { run: TTSRun | null; preview: { job_id: string; status: string; config: { voice: string; rate_percent: number }; job: { status: string; error_code: string | null } } | null };
}

export async function createTTSRun(projectId: string, revision: TranslationRevision, voice: string, rate: number) {
  return await mutate(`/api/v1/projects/${projectId}/tts/jobs`, { translation_revision_id: revision.id, voice,
    rate_percent: rate, pitch_hz: 0, volume_percent: 0, consent_confirmed: true }) as { run_id: string; job: { id: string }; status: TTSRun };
}

export async function createTTSPreview(projectId: string, voice: string, rate: number) {
  return await mutate(`/api/v1/projects/${projectId}/tts/previews`, { text: "Xin chào, đây là câu nghe thử giọng đọc.", voice,
    rate_percent: rate, pitch_hz: 0, volume_percent: 0, consent_confirmed: true }) as TTSPreview;
}

export async function fetchTTSPreview(projectId: string, jobId: string) {
  const response = await fetch(`/api/v1/projects/${projectId}/tts/previews/${jobId}`, { credentials: "same-origin", cache: "no-store" });
  await checked(response); return await response.json() as { job: { id: string; status: string; error_code?: string | null }; artifact_id: string | null };
}

export async function cancelTTSJob(projectId: string, jobId: string) { return await mutate(`/api/v1/jobs/${jobId}/cancel?project_id=${projectId}`, {}); }
export async function retryTTSJob(projectId: string, jobId: string, expectedStatus: "FAILED" | "INTERRUPTED") {
  return await mutate(`/api/v1/jobs/${jobId}/retry?project_id=${projectId}`, { expected_status: expectedStatus });
}

export function ttsAudioUrl(projectId: string, runId: string, unitId: string) {
  return `/api/v1/projects/${projectId}/tts/runs/${runId}/units/${unitId}/audio`;
}
export function ttsPreviewUrl(projectId: string, artifactId: string) {
  return `/api/v1/projects/${projectId}/tts/artifacts/${artifactId}/audio`;
}
