export type BackendHealth = {
  status: "ok";
  service: "autodub-local-backend";
};

export async function fetchBackendHealth(signal?: AbortSignal): Promise<BackendHealth> {
  const response = await fetch("/api/v1/health", {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Phiên cục bộ hết hạn. Hãy khởi động lại launcher.");
    throw new Error(`Backend trả HTTP ${response.status}.`);
  }
  const body: unknown = await response.json();
  if (
    typeof body !== "object" || body === null ||
    (body as Record<string, unknown>).status !== "ok" ||
    (body as Record<string, unknown>).service !== "autodub-local-backend"
  ) {
    throw new Error("Phản hồi health không hợp lệ.");
  }
  return body as BackendHealth;
}
