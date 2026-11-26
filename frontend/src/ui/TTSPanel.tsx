import { useEffect, useMemo, useRef, useState } from "react";
import type { TranslationRevision } from "../api/media";
import { cancelTTSJob, createTTSPreview, createTTSRun, fetchTTSActivity, fetchTTSPreview, fetchTTSRun, fetchTTSVoices, retryTTSJob, ttsAudioUrl, ttsPreviewUrl, type TTSRun, type TTSVoice } from "../api/tts";

const CONSENT_KEY = "autodub:edge-tts-consent-v1";
const activeJobs = new Set(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]);

function seconds(us?: number | null) { return us == null ? "—" : `${(us / 1_000_000).toFixed(2)} s`; }
function unitStatus(status: string) {
  const labels: Record<string, string> = { QUEUED: "Đang chờ", RUNNING: "Đang tạo", READY: "Sẵn sàng", FAILED: "Lỗi", REVIEW_REQUIRED: "Cần kiểm tra", CANCELLED: "Đã hủy", STALE: "Hết hiệu lực" };
  return labels[status] ?? status;
}

export function TTSPanel({ projectId, revision, isCurrent, locked }: { projectId: string; revision: TranslationRevision; isCurrent: boolean; locked: boolean }) {
  const [voices, setVoices] = useState<TTSVoice[]>([]);
  const [voice, setVoice] = useState("");
  const [rate, setRate] = useState(0);
  const [consent, setConsent] = useState(() => window.localStorage.getItem(CONSENT_KEY) === "accepted");
  const [catalogError, setCatalogError] = useState("");
  const [error, setError] = useState("");
  const [run, setRun] = useState<TTSRun | null>(null);
  const [previewArtifact, setPreviewArtifact] = useState("");
  const [previewJob, setPreviewJob] = useState("");
  const [previewNeedsManual, setPreviewNeedsManual] = useState(false);
  const [remoteBatchActive, setRemoteBatchActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const player = useRef<HTMLAudioElement>(null);
  const previewContext = useRef("");
  const activityLocked = busy || !!previewJob || remoteBatchActive || (!!run && activeJobs.has(run.job_status));

  useEffect(() => {
    let stopped = false;
    setRun(null); setPreviewArtifact(""); setPreviewJob(""); setRemoteBatchActive(false); setError(""); previewContext.current="";
    player.current?.pause(); if (player.current) { player.current.removeAttribute("src"); player.current.load(); }
    void Promise.all([fetchTTSVoices(), fetchTTSActivity(projectId)]).then(([catalog, activity]) => {
      if (stopped) return;
      setVoices(catalog.voices);
      const first = catalog.default_voice ?? catalog.voices[0]?.short_name ?? "";
      setVoice((existing) => catalog.voices.some((item) => item.short_name === existing) ? existing : first);
      setRemoteBatchActive(!!activity.run && activeJobs.has(activity.run.job_status));
      if (activity.run) {
        if (activity.run.config.voice) setVoice(activity.run.config.voice);
        setRate(activity.run.config.rate_percent);
        if (activity.run.translation_revision_id === revision.id) setRun(activity.run);
      }
      if (activity.preview) { setPreviewJob(activity.preview.job_id); if (activity.preview.config.voice) setVoice(activity.preview.config.voice); if (Number.isInteger(activity.preview.config.rate_percent)) setRate(activity.preview.config.rate_percent); }
    }).catch((cause) => { if (!stopped) setCatalogError(cause instanceof Error ? cause.message : "Không tải được danh sách giọng đọc."); });
    return () => { stopped = true; };
  }, [projectId, revision.id]);

  useEffect(() => {
    if (!remoteBatchActive && !previewJob && !(run && activeJobs.has(run.job_status))) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        if (stopped) return;
        const activity = await fetchTTSActivity(projectId);
        if (stopped) return;
        const activeRun = activity.run && activeJobs.has(activity.run.job_status);
        setRemoteBatchActive(!!activeRun);
        if (activity.run?.translation_revision_id === revision.id) setRun(activity.run);
        if (activity.run && activeRun) { setVoice(activity.run.config.voice); setRate(activity.run.config.rate_percent); }
        if (activity.preview) {
          setPreviewJob(activity.preview.job_id);
          setVoice(activity.preview.config.voice); setRate(activity.preview.config.rate_percent);
        }
        const observedPreview = previewJob || activity.preview?.job_id;
        if (observedPreview) {
          const result = await fetchTTSPreview(projectId, observedPreview);
          if (stopped) return;
          if (result.job.status === "READY" && result.artifact_id) { previewContext.current=`${projectId}:${voice}:${rate}`; setPreviewArtifact(result.artifact_id); setPreviewJob(""); }
          else if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(result.job.status)) {
            if (result.job.status !== "CANCELLED") setError(`Không tạo được audio nghe thử${result.job.error_code ? ` (${result.job.error_code})` : ""}.`);
            setPreviewJob("");
          }
        }
      } catch (cause) {
        if (!stopped) setError(cause instanceof Error ? cause.message : "Mất kết nối khi theo dõi TTS; trạng thái vẫn được giữ khóa.");
      }
      if (!stopped && (remoteBatchActive || previewJob || (run && activeJobs.has(run.job_status)))) timer = window.setTimeout(() => void poll(), 900);
    };
    timer = window.setTimeout(() => void poll(), 300);
    return () => { stopped = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, [projectId, revision.id, previewJob, remoteBatchActive, run?.id, run?.job_status, voice, rate]);

  const counts = run?.progress;
  const hasWork = !!run && activeJobs.has(run.job_status);
  const canGenerate = consent && !!voice && isCurrent && !locked && revision.status === "ACCEPTED" && revision.units.length > 0 && !hasWork && !busy;
  const voiceOptions = useMemo(() => voices.map((item) => <option key={item.short_name} value={item.short_name}>{item.short_name}</option>), [voices]);
  const rememberConsent = (checked: boolean) => { setConsent(checked); window.localStorage.setItem(CONSENT_KEY, checked ? "accepted" : "revoked"); };

  useEffect(() => {
    if (!previewArtifact || !player.current) return;
    if (previewContext.current !== `${projectId}:${voice}:${rate}`) return;
    const audio = player.current;
    audio.pause(); audio.src = ttsPreviewUrl(projectId, previewArtifact); audio.load();
    void audio.play().then(() => setPreviewNeedsManual(false)).catch(() => setPreviewNeedsManual(true));
  }, [projectId, previewArtifact, voice, rate]);

  function clearPreviewAudio() {
    previewContext.current="";
    setPreviewArtifact(""); setPreviewNeedsManual(false);
    if (player.current) { player.current.pause(); player.current.removeAttribute("src"); player.current.load(); }
  }

  async function preview() {
    if (!consent || !voice || busy) return;
    setBusy(true); setError(""); clearPreviewAudio();
    try {
      const result = await createTTSPreview(projectId, voice, rate);
      if (result.cached && result.artifact_id) { previewContext.current=`${projectId}:${voice}:${rate}`; setPreviewArtifact(result.artifact_id); }
      else if (result.job?.id) setPreviewJob(result.job.id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Không tạo được audio nghe thử."); }
    finally { setBusy(false); }
  }
  async function generate() {
    if (!canGenerate) return;
    setBusy(true); setError("");
    try { const result = await createTTSRun(projectId, revision, voice, rate); setRun(result.status); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Không tạo được giọng đọc."); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (!run) return;
    setBusy(true);
    try { await cancelTTSJob(projectId, run.producer_job_id); setRun(await fetchTTSRun(projectId, run.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Không hủy được Job."); }
    finally { setBusy(false); }
  }
  async function retry() {
    if (!run) return;
    setBusy(true);
    try { if (run.job_status !== "FAILED" && run.job_status !== "INTERRUPTED") return; await retryTTSJob(projectId, run.producer_job_id, run.job_status); setRun(await fetchTTSRun(projectId, run.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Không thử lại được các câu lỗi."); }
    finally { setBusy(false); }
  }
  async function cancelPreview() {
    if (!previewJob) return;
    setBusy(true);
    try { await cancelTTSJob(projectId, previewJob); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Không hủy được nghe thử."); }
    finally { setBusy(false); }
  }
  function play(url: string) {
    const audio = player.current;
    if (!audio) return;
    audio.pause(); audio.src = url; audio.load(); void audio.play().catch(() => setError("Trình duyệt không phát được audio này."));
  }

  return <section className="tts-panel" aria-labelledby="tts-title">
    <h3 id="tts-title">Tạo giọng đọc</h3>
    <p className="tts-consent-message">Bản dịch tiếng Việt được gửi đến dịch vụ Edge-TTS để tạo giọng đọc. Video và âm thanh gốc vẫn được lưu trên máy tính.</p>
    <label className="consent-row"><input type="checkbox" checked={consent} disabled={activityLocked} onChange={(event) => rememberConsent(event.target.checked)} /> Tôi đồng ý gửi bản dịch đã duyệt đến Edge-TTS.</label>
    {consent ? <button type="button" className="text-button" disabled={activityLocked} onClick={() => rememberConsent(false)}>Thu hồi đồng ý</button> : <small>Đồng ý riêng cho Edge-TTS.</small>}
    <div className="tts-controls">
      <label>Giọng đọc <select value={voice} onChange={(event) => { setVoice(event.target.value); setPreviewArtifact(""); }} disabled={!voices.length || activityLocked}>{voiceOptions}</select></label>
      <label>Tốc độ <select value={rate} onChange={(event) => { setRate(Number(event.target.value)); setPreviewArtifact(""); }} disabled={activityLocked}><option value={-20}>Chậm hơn</option><option value={0}>Bình thường</option><option value={20}>Nhanh hơn</option></select></label>
      <button type="button" disabled={!consent || !voice || activityLocked || hasWork} onClick={() => void preview()}>{previewJob ? "Đang tạo nghe thử…" : "Nghe thử"}</button>
      {previewJob ? <button type="button" disabled={busy} onClick={() => void cancelPreview()}>Hủy nghe thử</button> : null}
      <button type="button" disabled={!canGenerate || !!previewJob} onClick={() => void generate()}>{busy && !hasWork ? "Đang chuẩn bị…" : "Tạo giọng đọc"}</button>
    </div>
    {previewArtifact && previewNeedsManual ? <button type="button" className="text-button" onClick={() => play(ttsPreviewUrl(projectId, previewArtifact))}>Phát nghe thử</button> : null}
    {catalogError ? <p role="alert" className="project-error">Không tải được giọng Edge-TTS: {catalogError}</p> : null}
    {!isCurrent ? <p role="status">Bản dịch đã duyệt không còn hiện hành. Hãy tải lại trạng thái bản dịch trước khi tạo giọng.</p> : null}
    {error ? <p role="alert" className="project-error">{error}</p> : null}
    {run ? <>
      <p role="status">{counts?.ready_units ?? 0}/{counts?.total_units ?? 0} câu sẵn sàng · {counts?.reused_units ?? 0} câu dùng lại · {counts?.running_units ?? 0} đang tạo · {counts?.queued_units ?? 0} đang chờ · {counts?.failed_units ?? 0} lỗi{run.job_progress?.current_round ? ` · vòng ${run.job_progress.current_round}/${run.job_progress.round_count ?? 3}` : ""}{run.job_progress?.phase === "retry_wait" ? " · đang chờ trước khi thử lại" : ""}</p>
      {hasWork ? <button type="button" disabled={busy} onClick={() => void cancel()}>Hủy tạo giọng</button> : null}
      {["FAILED", "INTERRUPTED"].includes(run.job_status) && counts && counts.failed_units > 0 && run.retry_available ? <button type="button" disabled={busy} title={run.retry_block_reason ?? undefined} onClick={() => void retry()}>{busy ? "Đang gửi yêu cầu…" : "Thử lại các đoạn lỗi"}</button> : null}
      <div className="tts-table-scroll"><table className="tts-table"><thead><tr><th>Thời gian</th><th>Lời thoại gốc</th><th>Bản dịch đã duyệt</th><th>Thời lượng audio</th><th>Trạng thái</th><th>Nghe</th></tr></thead><tbody>
        {run.units.map((unit) => <tr key={unit.unit_id}><td>{seconds(unit.start_us)}–{seconds(unit.end_us)}</td><td>{unit.source_text}</td><td>{unit.accepted_text}</td><td>{seconds(unit.artifact?.duration_us)}</td><td>{unitStatus(unit.status)}{unit.error_code ? ` · ${unit.error_code}` : ""}</td><td><button type="button" disabled={unit.status !== "READY" || !unit.artifact} onClick={() => play(ttsAudioUrl(projectId, run.id, unit.unit_id))}>Nghe</button></td></tr>)}
      </tbody></table></div>
    </> : null}
    <audio ref={player} className="tts-audio-player" controls preload="none" aria-label="Phát một audio Edge-TTS" />
  </section>;
}
