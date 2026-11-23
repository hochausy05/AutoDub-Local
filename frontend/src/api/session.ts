let csrfToken: string | null = null;
let sessionPromise: Promise<void> | null = null;

type SessionResponse = { csrf_token: string };

async function readCsrfResponse(response: Response): Promise<string> {
  if (!response.ok) {
    throw new Error(response.status === 401 ? "Phiên cục bộ hết hạn. Hãy khởi động lại launcher." : `HTTP ${response.status}`);
  }
  const body = (await response.json()) as Partial<SessionResponse>;
  if (typeof body.csrf_token !== "string" || body.csrf_token.length < 32) {
    throw new Error("Phản hồi phiên không hợp lệ.");
  }
  csrfToken = body.csrf_token;
  return csrfToken;
}

export async function establishSession(): Promise<void> {
  if (csrfToken) return;
  if (sessionPromise) return sessionPromise;
  sessionPromise = establishSessionOnce();
  try {
    await sessionPromise;
  } catch (error) {
    sessionPromise = null;
    throw error;
  }
}

async function establishSessionOnce(): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrapSecret = fragment.get("bootstrap");
  if (bootstrapSecret) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    await readCsrfResponse(
      await fetch("/api/v1/session/bootstrap", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Local-Bootstrap": bootstrapSecret,
        },
        body: "{}",
      }),
    );
    return;
  }

  await readCsrfResponse(
    await fetch("/api/v1/session/csrf", {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    }),
  );
}

export function currentCsrfToken(): string | null {
  return csrfToken;
}
