import { useCallback, useEffect, useState, type FormEvent } from "react";
import { fetchBackendHealth, type BackendHealth } from "../api/health";
import { createProject, fetchProjects, type Project } from "../api/projects";
import { establishSession } from "../api/session";

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
            {projects.length ? <ul className="project-list">{projects.map((project) => <li key={project.id}><span><strong>{project.name}</strong><small>{project.status} · {new Date(project.created_at).toLocaleString()}</small></span><code>{project.id}</code></li>)}</ul> : <p className="project-empty">{connection.state === "online" ? "Chưa có Project được lưu." : "Danh sách cần kết nối backend."}</p>}
          </div>
          <div className="stage-item stage-disabled"><span className="stage-icon">03</span><span><strong>Pipeline dubbing</strong><small>Chưa được bật trong skeleton</small></span><span className="stage-state">DISABLED</span></div>
        </div>
      </section>

      <footer><span>AutoDub Local <span className="footer-separator">/</span> Development</span><span>Project persistence enabled · Pipeline chưa triển khai.</span></footer>
    </main>
  );
}
