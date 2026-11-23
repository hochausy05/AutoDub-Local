import { useCallback, useEffect, useState, type FormEvent } from "react";
import { fetchBackendHealth, type BackendHealth } from "../api/health";
import { createProject, fetchProjects, type Project } from "../api/projects";
import { establishSession } from "../api/session";
import { chooseSource, fetchMedia, getImportJob, importSelected, mediaStreamUrl, relinkMedia, type ImportJob, type MediaAsset } from "../api/media";

type Connection =
  | { state: "checking"; message: string; health: null }
  | { state: "online"; message: string; health: BackendHealth }
  | { state: "offline"; message: string; health: null };

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
  const [relinkingAssetId, setRelinkingAssetId] = useState("");

  const check = useCallback(async (signal?: AbortSignal) => {
    try {
      await establishSession();
      const health = await fetchBackendHealth(signal);
      setConnection({ state: "online", message: "Backend đang phản hồi", health });
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

  useEffect(() => { void refreshMedia(selectedProject); }, [selectedProject, refreshMedia]);

  useEffect(() => {
    if (!importJob || !["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(importJob.status)) return;
    let stopped = false;
    const poll = async () => {
      try {
        const next = await getImportJob(importJob.id, importJob.project_id);
        if (stopped) return;
        setImportJob(next);
        if (next.status === "READY") void refreshMedia(next.project_id);
        else if (["FAILED", "CANCELLED", "INTERRUPTED"].includes(next.status)) setMediaError(next.error_code ? `Import dừng: ${next.error_code.replaceAll("_", " ")}.` : "Import thất bại.");
        else window.setTimeout(poll, 800);
      } catch (error) { if (!stopped) setMediaError(error instanceof Error ? error.message : "Không theo dõi được Import Job."); }
    };
    const timer = window.setTimeout(poll, 500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [importJob, refreshMedia]);

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
      setSelectedMediaId(asset.id);
    } catch (error) { setMediaError(error instanceof Error ? error.message : "Không thể chọn lại video."); }
    finally { setRelinkingAssetId(""); }
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
                <select id="media-project" value={selectedProject} onChange={(event) => setSelectedProject(event.target.value)}><option value="">Chọn Project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
                <label htmlFor="import-mode">Chế độ lưu nguồn</label>
                <select id="import-mode" value={importMode} onChange={(event) => setImportMode(event.target.value as "COPY" | "LINK")}><option value="COPY">COPY · quản lý bản sao</option><option value="LINK">LINK · giữ nguyên file gốc</option></select>
                <button type="button" onClick={() => void startImport()} disabled={!selectedProject || selectingMedia || connection.state !== "online"}>{selectingMedia ? "Đang mở hộp thoại…" : "Chọn video"}</button>
                {importJob ? <p role="status">Import {importJob.status}{importJob.error_code ? ` · ${importJob.error_code}` : ""}</p> : null}
                {mediaError ? <p className="project-error" role="alert">{mediaError}</p> : null}
                <ul className="media-list">{mediaAssets.map((asset) => <li key={asset.id}>
                  <strong>{asset.display_name}</strong>
                  <span>{asset.status} · {asset.mode} · {(asset.file_size / 1024 / 1024).toFixed(1)} MB · {(asset.metadata.duration_us / 1_000_000).toFixed(2)} s</span>
                  <span>{asset.metadata.container} · {asset.metadata.width}×{asset.metadata.height} · {asset.metadata.video_codec} · {asset.metadata.audio_streams ? `${asset.metadata.audio_streams} audio stream(s)` : "không có audio"}{asset.metadata.frame_rate ? ` · ${asset.metadata.frame_rate} fps` : ""}</span>
                  <div className="media-actions">
                    <button type="button" disabled={asset.status !== "READY"} onClick={() => setSelectedMediaId(asset.id)}>Phát video</button>
                    {asset.mode === "LINK" && asset.status !== "READY" ? <button type="button" disabled={relinkingAssetId === asset.id} onClick={() => void startRelink(asset)}>{relinkingAssetId === asset.id ? "Đang chọn lại…" : "Chọn lại video"}</button> : null}
                  </div>
                </li>)}</ul>
                {selectedMediaId && mediaAssets.some((asset) => asset.id === selectedMediaId && asset.status === "READY") ? (() => {
                  const asset = mediaAssets.find((item) => item.id === selectedMediaId)!;
                  return <section className="media-player" aria-label="Trình phát video">
                    <h3>{asset.display_name}</h3>
                    <video key={`${selectedProject}:${asset.id}`} controls preload="metadata" src={mediaStreamUrl(selectedProject, asset.id)} onError={() => { setMediaError("Trình duyệt không phát được video này. Codec có thể chưa được hỗ trợ; hãy thử MP4/H.264."); void refreshMedia(selectedProject); }}>
                      Trình duyệt không hỗ trợ phát video.
                    </video>
                    <small>{asset.metadata.width}×{asset.metadata.height} · {(asset.metadata.duration_us / 1_000_000).toFixed(2)} giây · {asset.mode}</small>
                  </section>;
                })() : null}
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
