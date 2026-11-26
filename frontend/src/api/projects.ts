import { currentCsrfToken } from "./session";

export type Project = {
  id: string;
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  lock_version: number;
  schema_version: number;
};

type ProjectList = { items: Project[]; limit: number; offset: number };
type ProjectCreated = { id: string; lock_version: number };

async function check(response: Response): Promise<void> {
  if (response.ok) return;
  let message = `API trả HTTP ${response.status}.`;
  try {
    const body = (await response.json()) as { error?: { message?: string } };
    if (body.error?.message) message = body.error.message;
  } catch {
    // Keep the status message when the response is not JSON.
  }
  throw new Error(message);
}

export async function fetchProjects(signal?: AbortSignal): Promise<Project[]> {
  const response = await fetch("/api/v1/projects", {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  await check(response);
  const body = (await response.json()) as ProjectList;
  if (!Array.isArray(body.items)) throw new Error("Danh sách Project từ API không hợp lệ.");
  return body.items;
}

export async function createProject(name: string): Promise<ProjectCreated> {
  const csrfToken = currentCsrfToken();
  if (!csrfToken) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response = await fetch("/api/v1/projects", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify({ name }),
  });
  await check(response);
  return (await response.json()) as ProjectCreated;
}

export async function deleteProject(project: Project): Promise<void> {
  const csrf=currentCsrfToken(); if(!csrf) throw new Error("Phiên cục bộ chưa sẵn sàng.");
  const response=await fetch(`/api/v1/projects/${project.id}`,{method:"DELETE",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,"Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({expected_lock_version:project.lock_version})});
  await check(response);
}
