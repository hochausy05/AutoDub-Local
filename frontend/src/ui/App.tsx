import { memo, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { fetchBackendHealth, type BackendHealth } from "../api/health";
import { createProject, fetchProjects, type Project } from "../api/projects";
import { establishSession } from "../api/session";
import { acceptOriginalTranslation, acceptTranscriptRevision, acceptTranslationDraft, cancelLocalJob, chooseSource, createASRJob, createTranscriptDraft, createTranslationDraft, createTranslationJob, fetchGeminiStatus, fetchTranslationEditor, fetchTranslationRun, fetchTranslationRuns, fetchASRModels, fetchASRRun, fetchASRRuns, fetchMedia, fetchProcessingSource, fetchTranscriptRevisions, getImportJob, importSelected, mediaStreamUrl, relinkMedia, retryLocalJob, saveTranscriptDraft, saveTranslationDraft, selectProcessingSource, type ASRJob, type ASRRun, type ASRRunSummary, type ImportJob, type MediaAsset, type ProcessingSource, type TranscriptRevision, type TranslationEditorState, type TranslationRevisionUnit, type TranslationRun } from "../api/media";

type Connection =
  | { state: "checking"; message: string; health: null }
  | { state: "online"; message: string; health: BackendHealth }
  | { state: "offline"; message: string; health: null };

function asrPhaseLabel(phase?: string) {
  if (phase === "extracting_audio") return "Đang chuẩn bị video";
  if (phase === "loading_model") return "Đang chuẩn bị dữ liệu nhận diện";
  if (phase === "transcribing") return "Đang nhận diện lời thoại";
  if (phase === "publishing_transcript") return "Đang lưu kết quả";
  return phase ? "Đang xử lý" : "";
}

function asrFailureLabel(code?: string | null) {
  const messages: Record<string, string> = {
    asr_inference_failed: "Không thể hoàn tất nhận diện. Kết quả cũ vẫn được giữ; xem nhật ký chẩn đoán cục bộ để biết chi tiết.",
    model_download_failed: "Không tải được dữ liệu nhận diện. Kiểm tra kết nối Internet rồi thử lại.",
    insufficient_model_disk_space: "Không đủ dung lượng trống để tải dữ liệu nhận diện.",
    no_audio_stream: "Video không có âm thanh để nhận diện.",
    asset_unavailable: "Không tìm thấy video nguồn. Chọn lại video hoặc relink file LINK.",
    asset_changed: "Video nguồn đã thay đổi. Kiểm tra hoặc chọn lại file rồi thử lại.",
  };
  return code ? `${messages[code] ?? "Nhận diện chưa hoàn tất. Kiểm tra video rồi thử lại nếu được phép."} (${code})` : "Nhận diện chưa hoàn tất.";
}

function translationStatusLabel(status?: string) {
  const labels: Record<string, string> = { QUEUED: "Đang chờ", RUNNING: "Đang dịch", PARTIAL: "Một số câu đã xong", WAITING_FOR_QUOTA: "Gemini đang giới hạn lượt gọi", REVIEW_REQUIRED: "Cần kiểm tra lại", FAILED: "Dịch thất bại", READY: "Đã dịch", STALE: "Kết quả thuộc phiên bản cũ", CANCELLED: "Đã hủy" };
  return status ? labels[status] ?? "Chưa bắt đầu" : "Chưa bắt đầu";
}

function translationErrorLabel(code?: string | null) {
  const labels: Record<string, string> = {
    gemini_service_unavailable: "Dịch vụ Gemini đang tạm thời không khả dụng. Vui lòng thử lại sau.",
    gemini_quota_exceeded: "Đã đạt giới hạn sử dụng Gemini. Vui lòng thử lại khi hạn mức được khôi phục.",
    gemini_network_error: "Không thể kết nối tới Gemini. Hãy kiểm tra kết nối Internet.",
    gemini_api_key_missing: "Chưa cấu hình Gemini API key. Kiểm tra AUTODUB_GEMINI_API_KEY trong .env rồi khởi động lại.",
    gemini_api_key_invalid: "Gemini không chấp nhận API key. Kiểm tra key trong .env.",
    gemini_permission_denied: "Tài khoản hoặc dự án chưa có quyền sử dụng Gemini API.",
    gemini_model_unsupported: "Model Gemini hiện không khả dụng cho cấu hình này.",
    gemini_request_invalid: "Cấu hình yêu cầu Gemini không hợp lệ. Bản dịch chưa hoàn tất.",
    gemini_proxy_error: "Proxy được cấu hình không kết nối được tới Gemini.",
    gemini_tls_error: "Không xác minh được chứng chỉ của Gemini. Kiểm tra cấu hình chứng chỉ hệ thống.",
    gemini_transient_provider_error: "Dịch vụ Gemini đang tạm thời gặp sự cố. Vui lòng thử lại sau.",
    gemini_response_schema_invalid: "Gemini trả về dữ liệu không đúng định dạng. Bản dịch chưa hoàn tất.",
    gemini_response_mapping_invalid: "Gemini trả về thiếu hoặc sai câu. Bản dịch chưa hoàn tất.",
  };
  return code ? `${labels[code] ?? "Bản dịch chưa hoàn tất."} (${code})` : "Bản dịch chưa hoàn tất.";
}

function formatSeconds(microseconds: number) {
  return (microseconds / 1_000_000).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

type TranslationEditorRowProps = { unit: TranslationRevisionUnit; text: string; editable: boolean; changed: boolean;
  onChange: (unitId: string, text: string) => void; onSeek: (startUs: number) => void };

const TranslationEditorRow = memo(function TranslationEditorRow({ unit, text, editable, changed, onChange, onSeek }: TranslationEditorRowProps) {
  return <tr className={changed ? "translation-unit-changed" : ""}>
    <td><button type="button" className="translation-timestamp" onClick={() => onSeek(unit.start_us)}>{formatSeconds(unit.start_us)}–{formatSeconds(unit.end_us)} s</button></td>
    <td className="translation-source-text">{unit.source_text}</td>
    <td>{editable ? <textarea aria-label={`Bản dịch câu ${unit.position + 1}`} value={text} rows={2}
      onChange={(event) => onChange(unit.unit_id, event.target.value)}
      onInput={(event) => { event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${event.currentTarget.scrollHeight}px`; }} /> : <p className="transcript-readonly-text">{unit.final_text}</p>}</td>
  </tr>;
});

function sourceTranscriptDraft(run: ASRRun, projectId: string): TranscriptRevision {
  return {
    id: "", project_id: projectId, run_id: run.id, source_asset_id: run.source_asset_id,
    parent_revision_id: null, revision_no: 0, status: "DRAFT", lock_version: 0,
    created_at: run.created_at, accepted_at: null, is_active: false, is_current: false,
    active_generation: 0, run_status: run.status, duration_us: run.duration_us,
    segments: run.segments.map((segment) => ({
      row_id: segment.id, stable_segment_key: segment.id, position: segment.position,
      text: segment.source_text, start_us: segment.start_us, end_us: segment.end_us,
      parent_keys: [], coverage_group_id: null, coverage_status: "REVIEWED",
      source_segment_ids: [segment.id], source_texts: [segment.source_text], timing_warning: false,
    })),
  };
}

export function App() {
  const [connection, setConnection] = useState<Connection>({
    state: "checking",
    message: "Đang xác thực phiên và kết nối backend…",
    health: null,
  });
  const [retryCount, setRetryCount] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectName, setProjectName] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  const [savingProject, setSavingProject] = useState(false);
  const [selectedProject, setSelectedProject] = useState("");
  const [importMode, setImportMode] = useState<"COPY" | "LINK">("COPY");
  const [mediaAssets, setMediaAssets] = useState<MediaAsset[]>([]);
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [selectingMedia, setSelectingMedia] = useState(false);
  const [selectedMediaId, setSelectedMediaId] = useState("");
  const [playingMediaId, setPlayingMediaId] = useState("");
  const [processingSource, setProcessingSource] = useState<ProcessingSource | null>(null);
  const [savingProcessingSource, setSavingProcessingSource] = useState(false);
  const [relinkingAssetId, setRelinkingAssetId] = useState("");
  const videoRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<{ assetId: string; timeSeconds: number } | null>(null);
  const [asrModelCached, setAsrModelCached] = useState(false);
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrJob, setAsrJob] = useState<ASRJob | null>(null);
  const [asrJobSourceId, setAsrJobSourceId] = useState("");
  const [asrRuns, setAsrRuns] = useState<ASRRunSummary[]>([]);
  const [selectedRun, setSelectedRun] = useState<ASRRun | null>(null);
  const [asrError, setAsrError] = useState<string | null>(null);
  const [startingASR, setStartingASR] = useState(false);
  const [asrActionBusy, setAsrActionBusy] = useState(false);
  const [transcriptRevisions, setTranscriptRevisions] = useState<TranscriptRevision[]>([]);
  const [transcriptRevision, setTranscriptRevision] = useState<TranscriptRevision | null>(null);
  const [transcriptDirty, setTranscriptDirty] = useState(false);
  const [transcriptEditing, setTranscriptEditing] = useState(false);
  const [transcriptSaving, setTranscriptSaving] = useState(false);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [translationRun, setTranslationRun] = useState<TranslationRun | null>(null);
  const [translationEditor, setTranslationEditor] = useState<TranslationEditorState | null>(null);
  const [translationTab, setTranslationTab] = useState<"original" | "approved">("original");
  const [translationEditing, setTranslationEditing] = useState(false);
  const [translationEditSource, setTranslationEditSource] = useState<"original" | "accepted">("original");
  const [translationShowDraft, setTranslationShowDraft] = useState(false);
  const [translationEdits, setTranslationEdits] = useState<Record<string, string>>({});
  const [translationDirtyCount, setTranslationDirtyCount] = useState(0);
  const [translationSaving, setTranslationSaving] = useState(false);
  const [translationReviewError, setTranslationReviewError] = useState<string | null>(null);
  const translationEditsRef = useRef<Record<string, string>>({});
  const translationBaselineRef = useRef<Record<string, string>>({});
  const translationDirtyRef = useRef(0);
  const [translationJob, setTranslationJob] = useState<ASRJob | null>(null);
  const [translationError, setTranslationError] = useState<string | null>(null);
  const [translationStarting, setTranslationStarting] = useState(false);
  const [translationRetrying, setTranslationRetrying] = useState(false);
  const translationSubmissionLock = useRef(false);
  const [clockNow, setClockNow] = useState(Date.now());
  const [geminiConfigured, setGeminiConfigured] = useState(false);
  const [transcriptTab, setTranscriptTab] = useState<"original" | "approved">("original");
  const consentScope = "gemini-transcript-translation-v1";
  const [translationConsent, setTranslationConsent] = useState(() => window.localStorage.getItem("autodub:translation-consent") === consentScope);
  const translationEditorContext = useRef<{ projectId: string; mediaId: string; revisionId: string; runId: string } | null>(null);
  const selectedContext = useRef({ projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision?.id ?? null });
  selectedContext.current = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision?.id ?? null };
  translationDirtyRef.current = translationDirtyCount;
  const savedTranscript = useRef<TranscriptRevision | null>(null);
  const cancelToOriginal = useRef(false);
  const [videoPlayheadUs, setVideoPlayheadUs] = useState(0);

  const installTranscript = useCallback((revision: TranscriptRevision | null) => {
    setTranscriptRevision(revision);
    savedTranscript.current = revision;
    cancelToOriginal.current = false;
    setTranscriptDirty(false);
    setTranscriptEditing(false);
    setTranscriptError(null);
  }, []);

  const check = useCallback(async (signal?: AbortSignal) => {
    try {
      await establishSession();
      const health = await fetchBackendHealth(signal);
      setConnection({ state: "online", message: "Backend đang phản hồi", health });
      try { setGeminiConfigured((await fetchGeminiStatus()).configured); } catch { setGeminiConfigured(false); }
      try {
        setProjects(await fetchProjects(signal));
        setProjectError(null);
      } catch (error) {
        if (signal?.aborted) return;
        setProjectError(error instanceof Error ? error.message : "Không tải được Project.");
      }
    } catch (error) {
      if (signal?.aborted) return;
      setConnection({
        state: "offline",
        message: error instanceof Error ? error.message : "Không kết nối được backend.",
        health: null,
      });
    }
  }, []);

  const submitProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSavingProject(true);
    setProjectError(null);
    try {
      await createProject(projectName);
      setProjectName("");
      setProjects(await fetchProjects());
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : "Không tạo được Project.");
    } finally {
      setSavingProject(false);
    }
  };

  const refreshMedia = useCallback(async (projectId: string) => {
    if (!projectId) { setMediaAssets([]); return; }
    try { setMediaAssets(await fetchMedia(projectId)); setMediaError(null); }
    catch (error) { setMediaError(error instanceof Error ? error.message : "Không tải được media."); }
  }, []);

  const refreshProcessingSource = useCallback(async (projectId: string) => {
    if (!projectId) { setProcessingSource(null); setSelectedMediaId(""); return; }
    try {
      const source = await fetchProcessingSource(projectId);
      setProcessingSource(source);
      setSelectedMediaId(source.media_asset_id ?? "");
    } catch (error) { setMediaError(error instanceof Error ? error.message : "Không tải được video xử lý."); }
  }, []);

  useEffect(() => { void refreshMedia(selectedProject); }, [selectedProject, refreshMedia]);

  useEffect(() => {
    if (connection.state !== "online") return;
    void fetchASRModels().then((result) => setAsrModelCached(result.items.find((item) => item.id === "base")?.cached ?? false)).catch(() => setAsrModelCached(false));
  }, [connection.state]);

  useEffect(() => {
    setAsrRuns([]); setSelectedRun(null); setAsrJob(null);
    if (!selectedProject) return;
    void refreshProcessingSource(selectedProject);
  }, [selectedProject, refreshProcessingSource]);

  useEffect(() => {
    let stopped = false;
    setSelectedRun(null); setAsrRuns([]);
    if (!selectedProject || !selectedMediaId) return;
    void fetchASRRuns(selectedProject).then(async (runs) => {
      if (stopped) return;
      const matching = runs.filter((run) => run.source_asset_id === selectedMediaId);
      setAsrRuns(matching);
      const active = matching.find((run) => run.is_active) ?? matching[0];
      if (active) {
        const detail = await fetchASRRun(selectedProject, active.id);
        if (!stopped && detail.source_asset_id === selectedMediaId) setSelectedRun(detail);
      }
    }).catch((error) => { if (!stopped) setAsrError(error instanceof Error ? error.message : "Không tải được kết quả nhận diện."); });
    return () => { stopped = true; };
  }, [selectedProject, selectedMediaId]);

  useEffect(() => {
    let stopped = false;
    setTranslationRun(null); setTranslationJob(null); setTranslationError(null);
    setTranslationStarting(false); setTranslationRetrying(false);
    if (!selectedProject || !selectedMediaId) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision?.id ?? null };
    const stillCurrent = () => !stopped && selectedContext.current.projectId === context.projectId
      && selectedContext.current.mediaId === context.mediaId && selectedContext.current.revisionId === context.revisionId;
    void (async () => {
      const runs = await fetchTranslationRuns(context.projectId);
      if (!stillCurrent()) return;
      const current = runs.find((run) => run.project_id === context.projectId && run.source_asset_id === context.mediaId && run.transcript_revision_id === context.revisionId);
      if (!current) { setTranslationRun(null); setTranslationJob(null); return; }
      const job = await getImportJob(current.producer_job_id, current.project_id);
      if (!stillCurrent() || current.producer_job_id !== job.id || job.project_id !== context.projectId) return;
      setTranslationRun(current); setTranslationJob(job);
    })().catch((error) => { if (stillCurrent()) setTranslationError(error instanceof Error ? error.message : "Không tải được bản dịch đã lưu."); });
    return () => { stopped = true; };
  }, [selectedProject, selectedMediaId, transcriptRevision?.id]);

  useEffect(() => {
    let stopped = false;
    const run = translationRun;
    if (!run || run.status !== "READY") {
      setTranslationEditor(null);
      translationEditorContext.current = null;
      return;
    }
    const context = { projectId: run.project_id, mediaId: run.source_asset_id, revisionId: run.transcript_revision_id, runId: run.id };
    translationEditorContext.current = context;
    void fetchTranslationEditor(context.projectId, context.runId).then((editor) => {
      const selected = selectedContext.current;
      if (stopped || translationDirtyRef.current > 0 || selected.projectId !== context.projectId || selected.mediaId !== context.mediaId
          || selected.revisionId !== context.revisionId || editor.project_id !== context.projectId || editor.run_id !== context.runId
          || editor.source_asset_id !== context.mediaId || editor.transcript_revision_id !== context.revisionId) return;
      setTranslationEditor(editor);
      setTranslationShowDraft(!!editor.draft);
      if (editor.draft) setTranslationEditSource(editor.draft.source_kind === "ACCEPTED_REVISION" ? "accepted" : "original");
    }).catch((error) => {
      const selected = selectedContext.current;
      if (!stopped && selected.projectId === context.projectId && selected.mediaId === context.mediaId && selected.revisionId === context.revisionId)
        setTranslationReviewError(error instanceof Error ? error.message : "Không tải được trạng thái bản dịch đã lưu.");
    });
    return () => { stopped = true; };
  }, [translationRun?.id, translationRun?.status]);

  useEffect(() => {
    const activeRun = !!translationRun && ["QUEUED", "RUNNING", "PARTIAL", "WAITING_FOR_QUOTA"].includes(translationRun.status);
    const waitingRetryCooldown = !!translationJob && translationJob.status === "FAILED" && translationJob.retry_block_reason === "retry_cooldown";
    if (!translationRun || !selectedProject || (!activeRun && !waitingRetryCooldown)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const job = await getImportJob(translationRun.producer_job_id, selectedProject);
        if (stopped) return;
        const run = await fetchTranslationRun(selectedProject, translationRun.id);
        const context = selectedContext.current;
        if (stopped || context.projectId !== selectedProject || context.mediaId !== translationRun.source_asset_id
            || context.revisionId !== translationRun.transcript_revision_id || run.project_id !== selectedProject
            || run.source_asset_id !== context.mediaId || run.transcript_revision_id !== context.revisionId
            || run.id !== translationRun.id || job.id !== translationRun.producer_job_id || job.project_id !== selectedProject) return;
        setTranslationJob(job);
        setTranslationRun(run);
        const stillActiveRun = ["QUEUED", "RUNNING", "PARTIAL", "WAITING_FOR_QUOTA"].includes(run.status) && ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(job.status);
        const stillCoolingDown = job.status === "FAILED" && job.retry_block_reason === "retry_cooldown";
        if (stillActiveRun || stillCoolingDown) timer = window.setTimeout(poll, 900);
        else if (job.status === "FAILED" || job.status === "INTERRUPTED") setTranslationError(job.error_code ? `Bản dịch chưa hoàn tất (${job.error_code}).` : "Bản dịch chưa hoàn tất.");
      } catch (error) { const context = selectedContext.current; if (!stopped && context.projectId === selectedProject && context.mediaId === translationRun.source_asset_id && context.revisionId === translationRun.transcript_revision_id) setTranslationError(error instanceof Error ? error.message : "Không theo dõi được tiến trình dịch."); }
    };
    timer = window.setTimeout(poll, 400);
    return () => { stopped = true; if (timer) window.clearTimeout(timer); };
  }, [translationRun?.id, translationRun?.status, selectedProject, selectedMediaId, transcriptRevision?.id]);

  const translationRetryDeadline = translationJob?.progress?.retry_after_at ?? translationJob?.attempt?.retry_after_at ?? null;
  const originalTranslationUnits: TranslationRevisionUnit[] = translationEditor?.original_units ?? (translationRun?.units ?? []).filter((unit) => !!(unit.final_text ?? unit.pass2_text)).map((unit) => ({
    unit_id: unit.unit_id, logical_unit_key: unit.logical_unit_key, position: unit.position,
    revision_segment_ids: unit.revision_segment_ids, source_segment_ids: unit.source_segment_ids,
    source_text: unit.source_text, start_us: unit.start_us, end_us: unit.end_us,
    final_text: unit.final_text ?? unit.pass2_text ?? "", unit_fingerprint: "", text_origin: "PROVIDER_OUTPUT",
  }));
  const showingAcceptedTranslation = translationTab === "approved";
  const showingDraftTranslation = !showingAcceptedTranslation && translationShowDraft && !!translationEditor?.draft;
  const visibleTranslationUnits = showingAcceptedTranslation ? translationEditor?.accepted?.units ?? []
    : showingDraftTranslation ? translationEditor?.draft?.units ?? [] : originalTranslationUnits;
  const visibleTranslationEditable = translationEditing && showingDraftTranslation && !translationSaving;
  const activeTranscriptAccepted = transcriptRevisions.find((revision) => revision.run_id === selectedRun?.id && revision.status === "ACCEPTED" && revision.is_current) ?? null;
  const approvedViewRevision = transcriptEditing ? transcriptRevision : transcriptRevision?.status === "DRAFT" ? activeTranscriptAccepted ?? transcriptRevision : transcriptRevision;
  useEffect(() => {
    if (!translationRetryDeadline) return;
    setClockNow(Date.now());
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [translationRetryDeadline]);

  useEffect(() => {
    let stopped = false;
    setTranscriptRevisions([]);
    installTranscript(null);
    if (!selectedProject || !selectedMediaId) return;
    void fetchTranscriptRevisions(selectedProject, selectedMediaId).then((revisions) => {
      if (stopped) return;
      setTranscriptRevisions(revisions);
      const preferred = revisions.find((revision) => revision.status === "DRAFT") ?? revisions.find((revision) => revision.is_active) ?? null;
      installTranscript(preferred);
    }).catch((error) => { if (!stopped) setTranscriptError(error instanceof Error ? error.message : "Không tải được lời thoại đã lưu."); });
    return () => { stopped = true; };
  }, [selectedProject, selectedMediaId, installTranscript]);

  useEffect(() => {
    let stopped = false;
    if (!selectedProject || !selectedRun || selectedRun.source_asset_id !== selectedMediaId) return;
    void fetchTranscriptRevisions(selectedProject, selectedMediaId).then((revisions) => {
      if (stopped) return;
      setTranscriptRevisions(revisions);
      if (!transcriptDirty && !transcriptEditing) {
        const matching = revisions.filter((revision) => revision.run_id === selectedRun.id);
        installTranscript(matching.find((revision) => revision.status === "DRAFT") ?? matching.find((revision) => revision.is_active) ?? null);
      }
    }).catch((error) => { if (!stopped) setTranscriptError(error instanceof Error ? error.message : "Không tải được phiên bản lời thoại."); });
    return () => { stopped = true; };
  }, [selectedProject, selectedMediaId, selectedRun?.id, transcriptDirty, transcriptEditing, installTranscript]);

  useEffect(() => {
    if (!importJob || !["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(importJob.status)) return;
    let stopped = false;
    const poll = async () => {
      try {
        const next = await getImportJob(importJob.id, importJob.project_id);
        if (stopped) return;
        setImportJob(next);
        if (next.status === "READY") { void refreshMedia(next.project_id); void refreshProcessingSource(next.project_id); }
        else if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(next.status)) setMediaError(next.error_code ? `Import dừng: ${next.error_code.replaceAll("_", " ")}.` : "Import thất bại.");
        else window.setTimeout(poll, 800);
      } catch (error) { if (!stopped) setMediaError(error instanceof Error ? error.message : "Không theo dõi được Import Job."); }
    };
    const timer = window.setTimeout(poll, 500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [importJob, refreshMedia, refreshProcessingSource]);

  useEffect(() => {
    if (!asrJob || !["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(asrJob.status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await getImportJob(asrJob.id, asrJob.project_id);
        if (stopped) return;
        if (next.status === "READY") {
          const runs = await fetchASRRuns(next.project_id);
          const matching = runs.filter((run) => run.source_asset_id === asrJobSourceId);
          const active = matching.find((run) => run.is_active) ?? matching[0];
          const detail = active ? await fetchASRRun(next.project_id, active.id) : null;
          const revisions = await fetchTranscriptRevisions(next.project_id, asrJobSourceId);
          const context = selectedContext.current;
          if (stopped || context.projectId !== next.project_id || context.mediaId !== asrJobSourceId || detail && detail.source_asset_id !== context.mediaId) return;
          setAsrJob(next);
          setAsrRuns(matching);
          setSelectedRun(detail);
          setTranscriptRevisions(revisions);
          if (!transcriptDirty && !transcriptEditing) installTranscript(revisions.find((revision) => revision.status === "DRAFT") ?? revisions.find((revision) => revision.is_active) ?? null);
          void fetchASRModels().then((result) => setAsrModelCached(result.items.find((item) => item.id === "base")?.cached ?? false)).catch(() => undefined);
        } else if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(next.status)) {
          setAsrJob(next);
          setAsrError(asrFailureLabel(next.error_code));
          void fetchASRModels().then((result) => setAsrModelCached(result.items.find((item) => item.id === "base")?.cached ?? false)).catch(() => setAsrModelCached(false));
        } else { setAsrJob(next); timer = window.setTimeout(poll, 800); }
      } catch (error) { if (!stopped) setAsrError(error instanceof Error ? error.message : "Không theo dõi được ASR Job."); }
    };
    timer = window.setTimeout(poll, 500);
    return () => { stopped = true; if (timer) window.clearTimeout(timer); };
  }, [asrJob?.id, asrJobSourceId, selectedProject, selectedMediaId, transcriptDirty, transcriptEditing, installTranscript]);

  const startImport = async () => {
    if (!selectedProject) return;
    setSelectingMedia(true); setMediaError(null); setImportJob(null);
    try {
      const selection = await chooseSource(selectedProject);
      const job = await importSelected(selectedProject, selection.selection_id, importMode);
      setImportJob(job);
    } catch (error) { setMediaError(error instanceof Error ? error.message : "Không thể import video."); }
    finally { setSelectingMedia(false); }
  };

  const startRelink = async (asset: MediaAsset) => {
    if (!selectedProject) return;
    setRelinkingAssetId(asset.id); setMediaError(null);
    try {
      await relinkMedia(selectedProject, asset);
      await refreshMedia(selectedProject);
      await refreshProcessingSource(selectedProject);
    } catch (error) { setMediaError(error instanceof Error ? error.message : "Không thể chọn lại video."); }
    finally { setRelinkingAssetId(""); }
  };

  const chooseProcessingVideo = async (asset: MediaAsset) => {
    if (!selectedProject || asset.status !== "READY") return;
    if ((transcriptDirty || translationDirtyCount > 0) && !window.confirm("Bạn còn thay đổi chưa lưu trong lời thoại hoặc bản dịch. Đổi video sẽ bỏ phần đang nhập chưa lưu. Bạn muốn tiếp tục?")) return;
    setSavingProcessingSource(true); setMediaError(null);
    try {
      const source = await selectProcessingSource(selectedProject, asset.id, processingSource?.generation ?? 0);
      setProcessingSource(source);
      setSelectedMediaId(asset.id);
    } catch (error) { setMediaError(error instanceof Error ? error.message : "Không thể chọn video xử lý."); }
    finally { setSavingProcessingSource(false); }
  };

  const startTranslation = async () => {
    if (!selectedProject || !transcriptRevision?.id || transcriptRevision.status !== "ACCEPTED" || !transcriptRevision.is_current) return;
    if (!translationConsent || translationSubmissionLock.current) return;
    if (translationDirtyCount > 0 && !window.confirm("Bạn còn bản dịch chưa lưu. Bắt đầu Run mới sẽ bỏ phần đang nhập. Tiếp tục?")) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision.id };
    const stillCurrent = () => selectedContext.current.projectId === context.projectId && selectedContext.current.mediaId === context.mediaId && selectedContext.current.revisionId === context.revisionId;
    translationSubmissionLock.current = true;
    setTranslationStarting(true); setTranslationError(null);
    try {
      const result = await createTranslationJob(selectedProject, transcriptRevision.id, translationConsent);
      const run = await fetchTranslationRun(context.projectId, result.run_id);
      if (!stillCurrent() || run.id !== result.run_id || run.project_id !== context.projectId || run.source_asset_id !== context.mediaId
          || run.transcript_revision_id !== context.revisionId || result.job.id !== run.producer_job_id || result.job.project_id !== context.projectId) return;
      setTranslationJob(result.job); setTranslationRun(run);
    } catch (error) {
      if (stillCurrent()) setTranslationError(error instanceof Error ? error.message : "Không thể bắt đầu dịch.");
      try { setGeminiConfigured((await fetchGeminiStatus()).configured); } catch { /* keep current configuration status */ }
    } finally { translationSubmissionLock.current = false; if (stillCurrent()) setTranslationStarting(false); }
  };

  const cancelTranslation = async () => {
    if (!translationJob || !translationRun || !selectedProject) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision?.id ?? null, runId: translationRun.id, jobId: translationJob.id };
    const stillCurrent = () => selectedContext.current.projectId === context.projectId && selectedContext.current.mediaId === context.mediaId
      && selectedContext.current.revisionId === context.revisionId && translationRun?.id === context.runId && translationJob?.id === context.jobId;
    try { const job = await cancelLocalJob(context.jobId, context.projectId); if (stillCurrent() && job.id === context.jobId && job.project_id === context.projectId) setTranslationJob(job); }
    catch (error) { if (stillCurrent()) setTranslationError(error instanceof Error ? error.message : "Không thể hủy Job."); }
  };

  const retryTranslation = async () => {
    if (!translationJob || !translationRun) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision?.id ?? null, runId: translationRun.id, jobId: translationJob.id };
    const stillCurrent = () => selectedContext.current.projectId === context.projectId && selectedContext.current.mediaId === context.mediaId
      && selectedContext.current.revisionId === context.revisionId && translationRun?.id === context.runId && translationJob?.id === context.jobId;
    setTranslationRetrying(true); setTranslationError(null);
    try { const job = await retryLocalJob(translationJob); if (stillCurrent() && job.id === context.jobId && job.project_id === context.projectId) { setTranslationJob(job); setTranslationRun({ ...translationRun, status: "RUNNING" }); } }
    catch (error) { if (stillCurrent()) setTranslationError(error instanceof Error ? error.message : "Không thể thử lại."); }
    finally { if (stillCurrent()) setTranslationRetrying(false); }
  };

  const translationCurrent = (context: { projectId: string; mediaId: string; revisionId: string; runId: string }) => {
    const selected = selectedContext.current;
    return selected.projectId === context.projectId && selected.mediaId === context.mediaId && selected.revisionId === context.revisionId
      && translationRun?.id === context.runId;
  };

  const beginTranslationEdit = async (source: "original" | "accepted") => {
    if (!translationRun || !translationEditor || !selectedProject || !selectedMediaId || !transcriptRevision?.id || !translationEditor.run_is_current) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision.id, runId: translationRun.id };
    setTranslationReviewError(null);
    try {
      const base = source === "accepted" ? translationEditor.accepted : null;
      const draft = translationEditor.draft ?? await createTranslationDraft(context.projectId, context.runId, base?.id ?? null);
      if (!translationCurrent(context) || draft.project_id !== context.projectId || draft.run_id !== context.runId || draft.status !== "DRAFT") return;
      setTranslationEditor((current) => current?.run_id === draft.run_id ? { ...current, draft } : current);
      translationBaselineRef.current = Object.fromEntries(draft.units.map((unit) => [unit.unit_id, unit.final_text]));
      translationEditsRef.current = {};
      setTranslationEdits({}); setTranslationDirtyCount(0);
      setTranslationEditSource(draft.source_kind === "ACCEPTED_REVISION" ? "accepted" : "original"); setTranslationShowDraft(true); setTranslationTab("original"); setTranslationEditing(true);
    } catch (error) { if (translationCurrent(context)) setTranslationReviewError(error instanceof Error ? error.message : "Không thể mở bản nháp để sửa."); }
  };

  const updateTranslationUnit = useCallback((unitId: string, text: string) => {
    const next = { ...translationEditsRef.current };
    if (text === translationBaselineRef.current[unitId]) delete next[unitId];
    else next[unitId] = text;
    translationEditsRef.current = next;
    const count = Object.keys(next).length;
    translationDirtyRef.current = count;
    setTranslationEdits(next); setTranslationDirtyCount(count);
  }, []);

  const cancelTranslationEdit = () => {
    if (translationDirtyCount > 0 && !window.confirm("Bạn có thay đổi bản dịch chưa lưu. Hủy chỉnh sửa sẽ bỏ các thay đổi này; bản nháp đã lưu và bản đã duyệt vẫn được giữ. Tiếp tục?")) return;
    translationEditsRef.current = {};
    translationDirtyRef.current = 0;
    setTranslationEdits({}); setTranslationDirtyCount(0); setTranslationEditing(false);
    setTranslationShowDraft(!!translationEditor?.draft);
  };

  const changeTranslationTab = (tab: "original" | "approved") => {
    if (tab === translationTab) return;
    if (translationDirtyCount > 0) {
      if (!window.confirm("Bạn có thay đổi bản dịch chưa lưu. Rời chế độ sửa sẽ bỏ phần chưa lưu; bản nháp đã lưu vẫn được giữ. Tiếp tục?")) return;
      translationEditsRef.current = {}; translationDirtyRef.current = 0;
      setTranslationEdits({}); setTranslationDirtyCount(0); setTranslationEditing(false);
      setTranslationShowDraft(!!translationEditor?.draft);
    }
    setTranslationTab(tab);
  };

  const saveAllTranslationEdits = async () => {
    const run = translationRun, draft = translationEditor?.draft;
    if (!run || !draft || !selectedProject || !selectedMediaId || !transcriptRevision?.id || !translationDirtyCount || !translationEditor?.run_is_current) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision.id, runId: run.id };
    const edits = Object.entries(translationEditsRef.current).map(([unit_id, text]) => ({ unit_id, text }));
    setTranslationSaving(true); setTranslationReviewError(null);
    try {
      const saved = await saveTranslationDraft(context.projectId, context.runId, draft.id, draft.lock_version, edits);
      if (!translationCurrent(context) || saved.id !== draft.id || saved.status !== "DRAFT") return;
      setTranslationEditor((current) => current?.run_id === context.runId ? { ...current, draft: saved } : current);
      translationBaselineRef.current = Object.fromEntries(saved.units.map((unit) => [unit.unit_id, unit.final_text]));
      translationEditsRef.current = {}; translationDirtyRef.current = 0;
      setTranslationEdits({}); setTranslationDirtyCount(0); setTranslationEditing(false); setTranslationShowDraft(true); setTranslationTab("original");
    } catch (error) { if (translationCurrent(context)) setTranslationReviewError(error instanceof Error ? error.message : "Không thể lưu bản nháp. Nội dung đang nhập vẫn được giữ."); }
    finally { setTranslationSaving(false); }
  };

  const approveTranslation = async () => {
    const run = translationRun, editor = translationEditor;
    if (!run || !editor || !selectedProject || !selectedMediaId || !transcriptRevision?.id || translationDirtyCount || !editor.run_is_current) return;
    const context = { projectId: selectedProject, mediaId: selectedMediaId, revisionId: transcriptRevision.id, runId: run.id };
    setTranslationSaving(true); setTranslationReviewError(null);
    try {
      const draft = editor.draft;
      const acceptDraft = !!draft && translationTab === "original" && translationShowDraft;
      const accepted = acceptDraft
        ? await acceptTranslationDraft(context.projectId, context.runId, draft!.id, draft!.lock_version, editor.active_revision_id)
        : await acceptOriginalTranslation(context.projectId, context.runId, editor.active_revision_id);
      if (!translationCurrent(context) || accepted.project_id !== context.projectId || accepted.run_id !== context.runId || accepted.status !== "ACCEPTED") return;
      setTranslationEditor((current) => current?.run_id === context.runId ? { ...current, draft: acceptDraft ? null : current.draft, accepted, latest_accepted: accepted, active_revision_id: accepted.id, active_generation: current.active_generation + 1 } : current);
      setTranslationEditing(false); setTranslationShowDraft(false); setTranslationTab("approved");
    } catch (error) { if (translationCurrent(context)) setTranslationReviewError(error instanceof Error ? error.message : "Không thể duyệt bản dịch."); }
    finally { setTranslationSaving(false); }
  };

  useEffect(() => {
    if (!translationDirtyCount) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [translationDirtyCount]);

  const startASR = async () => {
    const source = mediaAssets.find((asset) => asset.id === selectedMediaId && asset.status === "READY");
    if (!selectedProject || !source) return;
    setStartingASR(true); setAsrError(null);
    try {
      const modelStatus = await fetchASRModels();
      const modelCached = modelStatus.items.find((item) => item.id === "base")?.cached ?? false;
      setAsrModelCached(modelCached);
      const job = await createASRJob(selectedProject, asrLanguage as "auto" | "en" | "zh", !modelCached);
      setAsrJobSourceId(source.id);
      setAsrJob(job);
    } catch (error) { setAsrError(error instanceof Error ? error.message : "Không thể tạo ASR Job."); }
    finally { setStartingASR(false); }
  };

  const cancelASR = async () => {
    if (!asrJob) return;
    setAsrActionBusy(true);
    try { setAsrJob(await cancelLocalJob(asrJob.id, asrJob.project_id)); }
    catch (error) { setAsrError(error instanceof Error ? error.message : "Không thể hủy Job."); }
    finally { setAsrActionBusy(false); }
  };

  const retryASR = async () => {
    if (!asrJob) return;
    setAsrActionBusy(true); setAsrError(null);
    try { setAsrJob(await retryLocalJob(asrJob)); }
    catch (error) { setAsrError(error instanceof Error ? error.message : "Job chưa thể chạy lại."); }
    finally { setAsrActionBusy(false); }
  };

  const editTranscript = () => {
    setTranscriptTab("approved");
    if (transcriptRevision) {
      if (transcriptRevision.status === "ACCEPTED") {
        const existingDraft = transcriptRevisions.find((revision) => revision.run_id === transcriptRevision.run_id && revision.status === "DRAFT");
        if (existingDraft) {
          savedTranscript.current = existingDraft;
          setTranscriptRevision(existingDraft);
          setTranscriptDirty(false);
        }
      }
      setTranscriptEditing(true);
      if (transcriptRevision.status === "DRAFT") setTranscriptDirty(false);
      return;
    }
    if (!selectedRun || selectedRun.source_asset_id !== selectedMediaId || !selectedProject) return;
    const localDraft = sourceTranscriptDraft(selectedRun, selectedProject);
    savedTranscript.current = localDraft;
    cancelToOriginal.current = true;
    setTranscriptRevision(localDraft);
    setTranscriptEditing(true);
    setTranscriptDirty(false);
  };

  const updateTranscriptText = (rowId: string, text: string) => {
    if (!transcriptEditing || !transcriptRevision) return;
    const segments = transcriptRevision.segments.map((segment) => segment.row_id === rowId ? { ...segment, text } : segment);
    setTranscriptRevision({ ...transcriptRevision, segments });
    const baseline = savedTranscript.current?.segments ?? (transcriptRevision.id ? transcriptRevision.segments : []);
    setTranscriptDirty(segments.some((segment, index) => segment.text !== baseline[index]?.text));
  };

  const saveTranscript = async () => {
    if (!selectedProject || !transcriptRevision || !transcriptEditing || !transcriptDirty) return;
    setTranscriptSaving(true); setTranscriptError(null);
    try {
      let target = transcriptRevision;
      if (!target.id) {
        target = await createTranscriptDraft(selectedProject, { source_run_id: target.run_id });
      } else if (target.status === "ACCEPTED") {
        target = await createTranscriptDraft(selectedProject, { parent_revision_id: target.id });
      }
      const textByPosition = new Map(transcriptRevision.segments.map((segment) => [segment.position, segment.text]));
      const toSave = { ...target, segments: target.segments.map((segment) => ({ ...segment, text: textByPosition.get(segment.position) ?? segment.text })) };
      const saved = await saveTranscriptDraft(selectedProject, toSave);
      setTranscriptRevisions(await fetchTranscriptRevisions(selectedProject, selectedMediaId));
      installTranscript(saved);
    } catch (error) { setTranscriptError(error instanceof Error ? error.message : "Không thể lưu nháp. Nội dung đang nhập vẫn được giữ."); }
    finally { setTranscriptSaving(false); }
  };

  const cancelTranscriptEdit = () => {
    const returnToOriginal = cancelToOriginal.current;
    installTranscript(returnToOriginal ? null : savedTranscript.current);
    setTranscriptTab(returnToOriginal ? "original" : "approved");
  };

  const acceptTranscript = async () => {
    if (!selectedProject || transcriptEditing || transcriptDirty) return;
    if (transcriptRevision?.status === "ACCEPTED") return;
    if (!transcriptRevision && (!selectedRun || selectedRun.source_asset_id !== selectedMediaId || !selectedRun.segments.length)) return;
    setTranscriptSaving(true); setTranscriptError(null);
    try {
      const currentAccepted = transcriptRevisions.find((revision) => revision.status === "ACCEPTED" && revision.is_current);
      if (currentAccepted) {
        installTranscript(currentAccepted);
        return;
      }
      const draftIsOriginal = transcriptRevision?.status === "DRAFT" && transcriptRevision.segments.every((segment) => segment.source_segment_ids.length === 1 && segment.text === segment.source_texts[0]);
      const approveOriginal = transcriptTab === "original" && !draftIsOriginal;
      const draft = transcriptRevision?.id && !approveOriginal
        ? transcriptRevision
        : await createTranscriptDraft(selectedProject, { source_run_id: selectedRun!.id });
      const expectedActive = transcriptRevisions.find((revision) => revision.is_active)?.id ?? null;
      const accepted = await acceptTranscriptRevision(selectedProject, draft, expectedActive);
      setTranscriptRevisions(await fetchTranscriptRevisions(selectedProject, selectedMediaId));
      installTranscript(accepted);
      setTranscriptTab("approved");
    } catch (error) { setTranscriptError(error instanceof Error ? error.message : "Không thể duyệt lời thoại. Bản nháp đã lưu vẫn được giữ."); }
    finally { setTranscriptSaving(false); }
  };

  const rememberTranslationConsent = (accepted: boolean) => {
    setTranslationConsent(accepted);
    if (accepted) window.localStorage.setItem("autodub:translation-consent", consentScope);
    else window.localStorage.removeItem("autodub:translation-consent");
  };

  const seekToTimestamp = (assetId: string, startUs: number) => {
    const timeSeconds = startUs / 1_000_000;
    if (playingMediaId === assetId && videoRef.current?.readyState && videoRef.current.readyState >= 1) {
      videoRef.current.currentTime = timeSeconds;
      void videoRef.current.play().catch(() => undefined);
      return;
    }
    pendingSeek.current = { assetId, timeSeconds };
    setPlayingMediaId(assetId);
  };

  const translationSeekRef = useRef<(startUs: number) => void>(() => undefined);
  translationSeekRef.current = (startUs) => { if (translationRun) seekToTimestamp(translationRun.source_asset_id, startUs); };
  const onTranslationSeek = useCallback((startUs: number) => translationSeekRef.current(startUs), []);

  const seekToSegment = (run: ASRRun, startUs: number) => {
    seekToTimestamp(run.source_asset_id, startUs);
  };

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();

    const poll = async () => {
      if (cancelled) return;
      await check(controller.signal);
      if (!cancelled) timer = setTimeout(poll, 4000);
    };

    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [check, retryCount]);

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="AutoDub Local home">
          <span className="brand-mark">AD</span>
          <span>AutoDub <span className="brand-light">Local</span></span>
        </a>
        <span className="local-badge"><span className="local-dot" /> LOCAL WORKSPACE</span>
      </header>

      <section className="hero" aria-labelledby="welcome-title">
        <div className="hero-copy">
          <p className="eyebrow">DUBBING STUDIO · LOCAL FIRST</p>
          <h1 id="welcome-title">Biến video thành<br /><em>câu chuyện của bạn.</em></h1>
          <p className="intro">Không gian làm việc lồng tiếng trên máy của bạn. Các bước xử lý sẽ xuất hiện khi từng phần được triển khai và kiểm chứng.</p>
          <div className={`connection connection-${connection.state}`} role="status" aria-live="polite">
            <span className="connection-indicator" />
            <span className="connection-copy">
              <strong>{connection.state === "online" ? "Đã kết nối" : connection.state === "checking" ? "Đang kết nối" : "Chưa kết nối"}</strong>
              <span>{connection.message}</span>
            </span>
            {connection.state === "online" ? <span className="service-chip">API · OK</span> : null}
          </div>
          {connection.state === "offline" ? (
            <button className="retry-button" onClick={() => setRetryCount((count) => count + 1)}>Thử kết nối lại <span aria-hidden="true">↗</span></button>
          ) : null}
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="orb orb-back" />
          <div className="orb orb-front" />
          <div className="wave-card">
          <div className="wave-top"><span>WORKSPACE</span><span>{projects.length ? `${projects.length} PROJECT` : "EMPTY"}</span></div>
          <div className="wave-summary"><span className="summary-mark" aria-hidden="true">{projects.length ? projects.length : "—"}</span><span className="summary-copy"><strong>{projects.length ? "Project đã lưu" : "Chưa có Project"}</strong><small>Dữ liệu cục bộ</small></span></div>
          <div className="wave-bottom"><span /><span>LOCAL PROJECT DATA</span></div>
          </div>
          <div className="art-caption">YOUR MEDIA<br />STAYS YOURS</div>
        </div>
      </section>

      <section className="workspace-card" aria-label="Workspace status">
        <div className="workspace-heading">
          <div><p className="eyebrow">WORKSPACE</p><h2>Khởi đầu gọn gàng.</h2></div>
          <span className="stage-label">LOCAL DATA · CORE-01A</span>
        </div>
        <div className="stage-list">
          <div className="stage-item stage-current"><span className="stage-icon">01</span><span><strong>Kết nối cục bộ</strong><small>Backend và giao diện đang được kiểm tra</small></span><span className="stage-state">{connection.state === "online" ? "READY" : "CHECKING"}</span></div>
          <div className="project-manager">
            <form className="project-form" onSubmit={(event) => void submitProject(event)}>
              <label htmlFor="project-name">Tên Project</label>
              <div><input id="project-name" value={projectName} onChange={(event) => setProjectName(event.target.value)} maxLength={120} required placeholder="Ví dụ: Video giới thiệu" /><button type="submit" disabled={savingProject || connection.state !== "online"}>{savingProject ? "Đang lưu…" : "Tạo Project"}</button></div>
            </form>
            {projectError ? <p className="project-error" role="alert">{projectError}</p> : null}
            {projects.length ? <>
              <ul className="project-list">{projects.map((project) => <li key={project.id}><span><strong>{project.name}</strong><small>{project.status} · {new Date(project.created_at).toLocaleString()}</small></span><code>{project.id}</code></li>)}</ul>
              <div className="media-import">
                <label htmlFor="media-project">Project nhận video</label>
                <select id="media-project" value={selectedProject} onChange={(event) => { if (!(transcriptDirty || translationDirtyCount > 0) || window.confirm("Bạn còn thay đổi chưa lưu trong lời thoại hoặc bản dịch. Đổi Project sẽ bỏ phần đang nhập chưa lưu. Tiếp tục?")) setSelectedProject(event.target.value); }}><option value="">Chọn Project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
                <label htmlFor="import-mode">Chế độ lưu nguồn</label>
                <select id="import-mode" value={importMode} onChange={(event) => setImportMode(event.target.value as "COPY" | "LINK")}><option value="COPY">COPY · quản lý bản sao</option><option value="LINK">LINK · giữ nguyên file gốc</option></select>
                <button type="button" onClick={() => void startImport()} disabled={!selectedProject || selectingMedia || connection.state !== "online"}>{selectingMedia ? "Đang mở hộp thoại…" : "Thêm video mới"}</button>
                {importJob ? <p role="status">Import {importJob.status}{importJob.error_code ? ` · ${importJob.error_code}` : ""}</p> : null}
                {mediaError ? <p className="project-error" role="alert">{mediaError}</p> : null}
                <h3 id="video-library">Video đã nhập</h3>
                <ul className="media-list">{mediaAssets.map((asset) => <li key={asset.id} data-asset-id={asset.id}>
                  <strong>{asset.display_name}</strong>
                  <span>{asset.status} · {asset.mode} · {(asset.file_size / 1024 / 1024).toFixed(1)} MB · {(asset.metadata.duration_us / 1_000_000).toFixed(2)} s</span>
                  <span>{asset.metadata.container} · {asset.metadata.width}×{asset.metadata.height} · {asset.metadata.video_codec} · {asset.metadata.audio_streams ? `${asset.metadata.audio_streams} audio stream(s)` : "không có audio"}{asset.metadata.frame_rate ? ` · ${asset.metadata.frame_rate} fps` : ""}</span>
                  <div className="media-actions">
                    <button type="button" disabled={asset.status !== "READY"} onClick={() => setPlayingMediaId(asset.id)}>Phát video</button>
                    <button type="button" disabled={asset.status !== "READY" || savingProcessingSource || selectedMediaId === asset.id} onClick={() => void chooseProcessingVideo(asset)}>{selectedMediaId === asset.id ? "Đang sử dụng để xử lý" : "Chọn làm video xử lý"}</button>
                    {asset.mode === "LINK" && asset.status !== "READY" ? <button type="button" disabled={relinkingAssetId === asset.id} onClick={() => void startRelink(asset)}>{relinkingAssetId === asset.id ? "Đang chọn lại…" : "Chọn lại video"}</button> : null}
                    <small>ID: {asset.id}</small>
                  </div>
                </li>)}</ul>
                {playingMediaId && mediaAssets.some((asset) => asset.id === playingMediaId && asset.status === "READY") ? (() => {
                  const asset = mediaAssets.find((item) => item.id === playingMediaId)!;
                  return <section className="media-player" aria-label="Trình phát video">
                    <h3>{asset.display_name}</h3>
                    <video ref={videoRef} key={`${selectedProject}:${asset.id}`} controls preload="metadata" src={mediaStreamUrl(selectedProject, asset.id)} onLoadedMetadata={() => {
                      const pending = pendingSeek.current;
                      if (pending?.assetId === asset.id && videoRef.current) {
                        videoRef.current.currentTime = pending.timeSeconds;
                        pendingSeek.current = null;
                        void videoRef.current.play().catch(() => undefined);
                      }
                    }} onTimeUpdate={() => { if (videoRef.current) setVideoPlayheadUs(Math.round(videoRef.current.currentTime * 1_000_000)); }} onError={() => { setMediaError("Trình duyệt không phát được video này. Codec có thể chưa được hỗ trợ; hãy thử MP4/H.264."); void refreshMedia(selectedProject); }}>
                      Trình duyệt không hỗ trợ phát video.
                    </video>
                    <small>{asset.metadata.width}×{asset.metadata.height} · {(asset.metadata.duration_us / 1_000_000).toFixed(2)} giây · {asset.mode}</small>
                  </section>;
                })() : null}
                <section className="asr-panel" aria-label="Nhận diện lời thoại">
                  <h3>Nhận diện lời thoại</h3>
                  <div className="processing-video">
                    <strong>Video đang xử lý</strong>
                    {selectedMediaId ? (() => {
                      const asset = mediaAssets.find((item) => item.id === selectedMediaId);
                      return asset ? <><span>{asset.display_name}</span><small>{(asset.metadata.duration_us / 1_000_000).toFixed(0)} giây · {asset.metadata.width} × {asset.metadata.height} · ID {asset.id.slice(0, 8)} · {asset.status}</small></> : <span>Đang kiểm tra video…</span>;
                    })() : <span>Chưa có video được chọn</span>}
                    <button type="button" onClick={() => document.getElementById("video-library")?.scrollIntoView({ behavior: "smooth" })}>Đổi video</button>
                  </div>
                  {selectedMediaId && processingSource?.status !== "READY" ? <p className="project-error" role="alert">{processingSource?.status === "MISSING" ? "Không tìm thấy video nguồn. Dùng Chọn lại video nếu đây là file LINK." : "Video đã thay đổi hoặc chưa sẵn sàng. Kiểm tra trạng thái trong danh sách video rồi thử lại."}</p> : null}
                  <label htmlFor="asr-language">Ngôn ngữ lời nói trong video</label>
                  <small>Bản dịch ở bước sau mặc định là Tiếng Việt.</small>
                  <select id="asr-language" value={asrLanguage} onChange={(event) => setAsrLanguage(event.target.value)}><option value="auto">Tự động nhận diện</option><option value="en">Tiếng Anh</option><option value="zh">Tiếng Trung</option></select>
                  {!asrModelCached ? <p role="note">Cần tải dữ liệu nhận diện giọng nói (khoảng 145 MB) cho lần sử dụng đầu tiên.</p> : null}
                  <button type="button" onClick={() => void startASR()} disabled={!selectedProject || !selectedMediaId || processingSource?.status !== "READY" || startingASR || connection.state !== "online" || (!!asrJob && ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(asrJob.status))}>
                    {startingASR ? "Đang chuẩn bị…" : !asrModelCached ? "Tải và tiếp tục" : "Nhận diện lời thoại"}
                  </button>
                  {asrJob && asrJob.project_id === selectedProject ? <div className="asr-job-status" role="status">
                    <p>Job cho {mediaAssets.find((asset) => asset.id === asrJobSourceId)?.display_name ?? "video trước"}: {asrJob.status === "QUEUED" ? "Đang chờ" : asrJob.status === "RUNNING" ? "Đang nhận diện lời thoại..." : asrJob.status === "READY" ? "Nhận diện hoàn tất" : asrJob.status === "FAILED" ? "Nhận diện thất bại" : asrJob.status === "INTERRUPTED" ? "Quá trình bị gián đoạn" : asrJob.status === "CANCELLED" ? "Đã hủy" : asrJob.status === "CANCEL_REQUESTED" ? "Đang hủy" : asrJob.status}{["RUNNING", "CANCEL_REQUESTED"].includes(asrJob.status) && asrJob.progress?.phase ? ` · ${asrPhaseLabel(asrJob.progress.phase)}` : ""}{asrJob.error_code ? ` · ${asrJob.error_code}` : ""}</p>
                    {asrJob.status === "RUNNING" || asrJob.status === "QUEUED" ? <button type="button" disabled={asrActionBusy} onClick={() => void cancelASR()}>{asrActionBusy ? "Đang xử lý…" : "Hủy nhận diện"}</button> : null}
                    {(["FAILED", "INTERRUPTED"].includes(asrJob.status) && asrJob.retry_available && selectedMediaId === asrJobSourceId) ? <button type="button" disabled={asrActionBusy} onClick={() => void retryASR()}>{asrActionBusy ? "Đang xử lý…" : "Thử lại"}</button> : null}
                    {(["FAILED", "INTERRUPTED"].includes(asrJob.status) && asrJob.retry_available && selectedMediaId !== asrJobSourceId) ? <small>Đã đổi video xử lý; Job cũ giữ nguyên nguồn snapshot và không thể kích hoạt kết quả cho video mới.</small> : null}
                    {(["FAILED", "INTERRUPTED"].includes(asrJob.status) && !asrJob.retry_available && asrJob.retry_block_reason === "retry_limit_reached") ? <small>Đã hết số lần thử cho Job này. Bạn có thể chủ động chạy nhận diện mới cho video hiện tại.</small> : null}
                    {(["FAILED", "INTERRUPTED"].includes(asrJob.status) && !asrJob.retry_available && asrJob.retry_block_reason === "job_stale") ? <small>Đầu vào Job đã cũ; hãy chạy nhận diện mới cho video đang chọn.</small> : null}
                  </div> : null}
                  {asrError ? <p className="project-error" role="alert">{asrError}</p> : null}
                  {asrRuns.length ? <>
                    <label htmlFor="asr-run">Transcription Runs</label>
                    <select id="asr-run" value={selectedRun?.id ?? ""} onChange={(event) => {
                      if ((transcriptDirty || translationDirtyCount > 0) && !window.confirm("Bạn còn thay đổi chưa lưu trong lời thoại hoặc bản dịch. Đổi kết quả nhận diện sẽ bỏ phần đang nhập chưa lưu. Tiếp tục?")) return;
                      const run = asrRuns.find((item) => item.id === event.target.value);
                      if (!run) { setSelectedRun(null); return; }
                      void fetchASRRun(selectedProject, run.id).then((detail) => { if (detail.source_asset_id === selectedMediaId) setSelectedRun(detail); });
                    }}>
                      <option value="">Chọn kết quả</option>{asrRuns.map((run) => <option key={run.id} value={run.id}>{run.status}{run.is_active ? " · active" : " · lịch sử"} · {run.model_id.split("/").at(-1)} · {new Date(run.created_at).toLocaleString()}</option>)}
                    </select>
                  </> : null}
                  <section className="transcript-editor" aria-label="Kiểm tra lời thoại">
                    <header><div><h3>Kiểm tra lời thoại</h3><small>{mediaAssets.find((asset) => asset.id === selectedMediaId)?.display_name ?? "Chưa chọn video"}</small></div>
                      {selectedRun?.status === "READY" ? <span className="transcript-state transcript-ready">Đã nhận diện</span> : null}
                    </header>
                    <div className="transcript-tabs" role="tablist" aria-label="Phiên bản lời thoại">
                      <button type="button" role="tab" aria-selected={transcriptTab === "original"} onClick={() => setTranscriptTab("original")}>Bản gốc</button>
                      <button type="button" role="tab" aria-selected={transcriptTab === "approved"} onClick={() => setTranscriptTab("approved")}>Bản đã duyệt</button>
                    </div>
                    {transcriptTab === "original" ? <>
                      {selectedRun?.project_id === selectedProject && selectedRun.source_asset_id === selectedMediaId ? <>
                        <p>{selectedRun.status === "NO_SPEECH" ? "Không tìm thấy lời thoại; không tạo Segment giả." : "Kết quả nhận diện gốc · chỉ đọc"}</p>
                        <ol className="transcript-segments">{selectedRun.segments.map((segment, index) => <li key={segment.id}>
                          <div className="transcript-segment-heading"><button type="button" onClick={() => seekToSegment(selectedRun, segment.start_us)}>{formatSeconds(segment.start_us)}–{formatSeconds(segment.end_us)} s</button><span>Câu {index + 1}</span></div>
                          <p className="transcript-readonly-text">{segment.source_text}</p>
                        </li>)}</ol>
                        {!transcriptRevision?.is_current && !transcriptEditing ? <div className="transcript-footer"><button type="button" disabled={!selectedRun.segments.length || transcriptSaving} onClick={editTranscript}>Sửa lời thoại</button><button type="button" disabled={!selectedRun.segments.length || transcriptSaving} onClick={() => void acceptTranscript()}>{transcriptSaving ? "Đang duyệt…" : "Duyệt bản gốc"}</button></div> : null}
                      </> : <p>Chưa có transcript cho video này. Chạy nhận diện để tạo nội dung cần kiểm tra.</p>}
                    </> : <>
                      {approvedViewRevision?.status === "DRAFT" ? <p role="status">Bản nháp chưa duyệt · {transcriptEditing && transcriptDirty ? "Có thay đổi chưa lưu" : "Thời điểm được giữ nguyên"}</p> : null}
                      {approvedViewRevision?.status === "ACCEPTED" && approvedViewRevision.is_current && !transcriptEditing ? <p role="status">Đã duyệt · Bản hiện hành</p> : null}
                      {approvedViewRevision && (transcriptEditing || approvedViewRevision.status === "ACCEPTED" && approvedViewRevision.is_current) ? <>
                        <ol className="transcript-segments">{approvedViewRevision.segments.map((segment, index) => {
                          const highlighted = playingMediaId === approvedViewRevision.source_asset_id && segment.start_us <= videoPlayheadUs && videoPlayheadUs < segment.end_us;
                          return <li key={segment.row_id} className={highlighted ? "transcript-segment-active" : ""}>
                            <div className="transcript-segment-heading"><button type="button" onClick={() => seekToTimestamp(approvedViewRevision.source_asset_id, segment.start_us)}>{formatSeconds(segment.start_us)}–{formatSeconds(segment.end_us)} s</button><span>Câu {index + 1}</span></div>
                            {transcriptEditing ? <textarea aria-label={`Nội dung câu ${index + 1}`} value={segment.text} rows={2} onChange={(event) => updateTranscriptText(segment.row_id, event.target.value)} /> : <p className="transcript-readonly-text">{segment.text}</p>}
                          </li>;
                        })}</ol>
                        {transcriptEditing ? <div className="transcript-footer"><button type="button" disabled={transcriptSaving} onClick={cancelTranscriptEdit}>Hủy</button><button type="button" disabled={!transcriptDirty || transcriptSaving} onClick={() => void saveTranscript()}>{transcriptSaving ? "Đang lưu…" : "Lưu chỉnh sửa"}</button></div> : <div className="transcript-footer"><button type="button" disabled={transcriptSaving} onClick={editTranscript}>{transcriptRevision?.status === "DRAFT" ? "Tiếp tục sửa bản nháp" : "Sửa lại lời thoại"}</button>{approvedViewRevision.status === "DRAFT" ? <button type="button" disabled={transcriptSaving || approvedViewRevision.segments.length === 0 || approvedViewRevision.segments.some((segment) => segment.coverage_status !== "REVIEWED")} onClick={() => void acceptTranscript()}>{transcriptSaving ? "Đang duyệt…" : "Duyệt lời thoại"}</button> : null}</div>}
                      </> : <><p>Chưa có bản lời thoại được duyệt. Có thể sửa hoặc duyệt nguyên bản trong tab Bản gốc.</p>{transcriptRevision?.status === "DRAFT" ? <button type="button" onClick={editTranscript}>Tiếp tục sửa bản nháp</button> : null}</>}
                    </>}
                    {transcriptError ? <p className="project-error" role="alert">{transcriptError}</p> : null}
                  </section>
                  <section className="translation-panel" aria-label="Dịch lời thoại">
                    <header><div><h3>Dịch lời thoại</h3><small>Ngôn ngữ đích: Tiếng Việt</small></div>
                      {translationRun ? <span className={`transcript-state transcript-${translationRun.status.toLowerCase()}`}>{translationStatusLabel(translationRun.status)}</span> : null}
                    </header>
                    <p>Lời thoại văn bản được gửi đến Gemini để dịch. Video và âm thanh vẫn được lưu trên máy tính.</p>
                    <label className="translation-consent"><input type="checkbox" checked={translationConsent} onChange={(event) => rememberTranslationConsent(event.target.checked)} /> Tôi đồng ý gửi lời thoại văn bản hiện hành đến Gemini để dịch.</label>
                    {translationConsent ? <button type="button" className="text-button" onClick={() => rememberTranslationConsent(false)}>Thu hồi đồng ý</button> : <small>Chỉ cần xác nhận một lần cho phạm vi dịch lời thoại hiện tại. Có thể thu hồi bất cứ lúc nào.</small>}
                    {!transcriptRevision || transcriptRevision.status !== "ACCEPTED" || !transcriptRevision.is_current ? <p>Hãy duyệt lời thoại hiện hành trước khi dịch.</p> : null}
                    {transcriptRevision?.status === "ACCEPTED" && transcriptRevision.segments.length === 0 ? <p>Không có lời thoại cần dịch.</p> : null}
                    {!geminiConfigured ? <p role="note">Chưa cấu hình Gemini credentials ở backend. Hãy thêm `AUTODUB_GEMINI_API_KEY` hoặc `AUTODUB_GEMINI_CREDENTIALS_JSON` vào `.env` cục bộ rồi khởi động lại.</p> : null}
                    <button type="button" disabled={!translationConsent || !geminiConfigured || !transcriptRevision?.id || transcriptRevision.status !== "ACCEPTED" || !transcriptRevision.is_current || transcriptRevision.segments.length === 0 || translationStarting || (!!translationJob && ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(translationJob.status))} onClick={() => void startTranslation()}>
                      {translationStarting ? "Đang chuẩn bị…" : "Dịch sang tiếng Việt"}
                    </button>
                    {translationJob && ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(translationJob.status) ? <div role="status"><p>{translationStatusLabel(translationRun?.status)}{translationJob.progress ? ` · ${translationJob.progress.completed_units ?? 0}/${translationJob.progress.total_units ?? 0} câu` : ""}</p><button type="button" onClick={() => void cancelTranslation()}>Hủy dịch</button></div> : null}
                    {translationJob && ["FAILED", "INTERRUPTED"].includes(translationJob.status) && translationJob.attempt?.retryable ? <button type="button" disabled={translationRetrying || !translationJob.retry_available} title={translationJob.retry_block_reason === "retry_cooldown" ? "Đang trong thời gian chờ." : translationJob.retry_block_reason === "retry_limit_reached" ? "Đã hết số lần thử của Job." : undefined} onClick={() => void retryTranslation()}>{translationRetrying ? "Đang gửi yêu cầu…" : "Thử lại các câu chưa hoàn tất"}</button> : null}
                    {translationJob?.retry_block_reason === "retry_cooldown" && translationRetryDeadline ? <p role="status">Có thể thử lại sau {Math.max(0, Math.ceil((Date.parse(translationRetryDeadline) - clockNow) / 1000))} giây.</p> : null}
                    {translationJob?.retry_block_reason === "retry_limit_reached" ? <p>Job đã hết số lần thử. Hãy kiểm tra lỗi trước khi chủ động tạo Job dịch mới.</p> : null}
                    {translationJob?.status === "RUNNING" && translationRun?.status === "RUNNING" && translationJob.progress?.phase === "retrying_provider" && translationJob.progress.retry_after_at ? <p role="status">Gemini đang bận. Ứng dụng sẽ tự thử lại sau {Math.max(0, Math.ceil((Date.parse(translationJob.progress.retry_after_at) - clockNow) / 1000))} giây.</p> : null}
                    {translationRun?.error_code ? <p role="alert" className="project-error">{translationErrorLabel(translationRun.error_code)}</p> : null}
                    {translationError ? <p role="alert" className="project-error">{translationError}</p> : null}
                    <div className="transcript-tabs" role="tablist" aria-label="Phiên bản bản dịch">
                      <button type="button" role="tab" aria-selected={translationTab === "original"} onClick={() => changeTranslationTab("original")}>Bản dịch gốc</button>
                      <button type="button" role="tab" aria-selected={translationTab === "approved"} onClick={() => changeTranslationTab("approved")}>Bản đã duyệt</button>
                    </div>
                    {translationRun?.status === "READY" && translationEditor ? <>
                      {!translationEditor.run_is_current ? <p role="status">Kết quả thuộc nguồn cũ. Có thể xem, nhưng không thể sửa hoặc duyệt làm đầu vào hiện hành.</p> : null}
                      {showingDraftTranslation ? <p role="status" className="translation-draft-notice">Bản nháp đã lưu · Chưa được duyệt · {translationEditSource === "accepted" ? "Dựa trên bản đã duyệt trước đó" : "Dựa trên kết quả gốc của Gemini"}</p> : null}
                      {showingAcceptedTranslation && !translationEditor.accepted ? <p role="status">Chưa có bản dịch được duyệt cho TranslationRun hiện hành. Bản nháp chưa phải đầu vào chính thức cho bước tạo giọng đọc.</p> : null}
                      {translationTab === "original" && translationEditor.draft && !translationEditing ? <button type="button" className="text-button" onClick={() => setTranslationShowDraft(!translationShowDraft)}>{translationShowDraft ? "Xem bản dịch gốc của Gemini" : "Xem bản nháp đã lưu"}</button> : null}
                      {visibleTranslationUnits.length ? <div className="translation-table-scroll"><table className="translation-table"><thead><tr><th>Thời gian</th><th>Lời thoại gốc</th><th>Bản dịch tiếng Việt</th></tr></thead><tbody>
                        {visibleTranslationUnits.map((unit) => <TranslationEditorRow key={unit.unit_id} unit={unit}
                          text={visibleTranslationEditable ? translationEdits[unit.unit_id] ?? unit.final_text : unit.final_text}
                          editable={visibleTranslationEditable} changed={translationEdits[unit.unit_id] !== undefined}
                          onChange={updateTranslationUnit} onSeek={onTranslationSeek} />)}
                      </tbody></table></div> : showingAcceptedTranslation ? null : <p role="status">Đang tải trạng thái bản dịch…</p>}
                      <div className="translation-editor-toolbar" role="group" aria-label="Thao tác bản dịch">
                        {translationEditing ? <>
                          <span>{translationDirtyCount ? `Đã sửa ${translationDirtyCount} câu` : "Chưa có thay đổi"}</span>
                          <button type="button" disabled={translationSaving} onClick={cancelTranslationEdit}>Hủy chỉnh sửa</button>
                          <button type="button" disabled={!translationDirtyCount || translationSaving} onClick={() => void saveAllTranslationEdits()}>{translationSaving ? "Đang lưu…" : "Lưu tất cả"}</button>
                        </> : translationTab === "approved" ? <>
                          {translationEditor.accepted && translationEditor.run_is_current ? <button type="button" disabled={translationSaving} onClick={() => void beginTranslationEdit("accepted")}>Sửa bản dịch đã duyệt</button> : null}
                          {translationEditor.draft ? <button type="button" onClick={() => changeTranslationTab("original")}>Mở bản nháp chưa duyệt</button> : null}
                        </> : <>
                          {translationEditor.run_is_current ? <>
                            <button type="button" disabled={translationSaving} onClick={() => void beginTranslationEdit(translationShowDraft ? translationEditSource : "original")}>{translationEditor.draft ? "Tiếp tục sửa bản nháp" : "Sửa bản dịch"}</button>
                            <button type="button" disabled={translationSaving} onClick={() => void approveTranslation()}>Duyệt bản dịch</button>
                          </> : null}
                        </>}
                      </div>
                    </> : translationRun && ["FAILED", "REVIEW_REQUIRED", "WAITING_FOR_QUOTA"].includes(translationRun.status) ? <p>Bản dịch chưa hoàn tất. Kết quả đã lưu giữ lại các câu đã xác minh.</p> : translationRun?.status === "READY" ? <p role="status">Đang tải bản dịch gốc và trạng thái bản nháp…</p> : null}
                    {translationReviewError ? <p role="alert" className="project-error">{translationReviewError}</p> : null}
                  </section>
                </section>
              </div>
            </> : <p className="project-empty">{connection.state === "online" ? "Chưa có Project được lưu." : "Danh sách cần kết nối backend."}</p>}
          </div>
          <div className="stage-item stage-disabled"><span className="stage-icon">03</span><span><strong>Pipeline dubbing</strong><small>Chưa được bật trong skeleton</small></span><span className="stage-state">DISABLED</span></div>
        </div>
      </section>

      <footer><span>AutoDub Local <span className="footer-separator">/</span> Development</span><span>Project persistence enabled · Pipeline chưa triển khai.</span></footer>
    </main>
  );
}
