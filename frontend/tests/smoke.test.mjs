import assert from "node:assert/strict";
import { test } from "node:test";

const origin = "http://127.0.0.1:5173";
const apiHeaders = {
  Origin: origin,
  "Sec-Fetch-Site": "same-origin",
};

test("development UI and proxied backend session are reachable", async () => {
  const page = await fetch(origin);
  assert.equal(page.status, 200);
  const html = await page.text();
  assert.match(html, /AutoDub Local/);
  assert.match(html, /src\/main\.tsx/);

  const bootstrap = await fetch(`${origin}/api/v1/session/bootstrap`, {
    method: "POST",
    headers: {
      ...apiHeaders,
      "Content-Type": "application/json",
      "X-Local-Bootstrap": process.env.AUTODUB_SMOKE_BOOTSTRAP_SECRET ?? "",
    },
    body: "{}",
  });
  assert.equal(bootstrap.status, 200, "bootstrap secret should be supplied by dev launcher");

  const sessionCookie = bootstrap.headers.get("set-cookie")?.split(";", 1)[0];
  assert.ok(sessionCookie, "backend should issue a host-only HttpOnly session cookie");
  assert.match(bootstrap.headers.get("set-cookie") ?? "", /HttpOnly/i);
  assert.match(bootstrap.headers.get("set-cookie") ?? "", /SameSite=Strict/i);

  const health = await fetch(`${origin}/api/v1/health`, {
    headers: { ...apiHeaders, Cookie: sessionCookie },
  });
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { status: "ok", service: "autodub-local-backend" });
  assert.notEqual(health.headers.get("access-control-allow-origin"), "*", "the dev proxy must not enable wildcard CORS");

  const unimplemented = await fetch(`${origin}/api/v1/projects`, {
    headers: { ...apiHeaders, Cookie: sessionCookie },
  });
  assert.equal(unimplemented.status, 404);
});

test("foreign origins and missing sessions are rejected", async () => {
  const foreign = await fetch(`${origin}/api/v1/health`, {
    headers: { ...apiHeaders, Origin: "http://evil.example" },
  });
  assert.equal(foreign.status, 403);

  const anonymous = await fetch(`${origin}/api/v1/health`, { headers: apiHeaders });
  assert.equal(anonymous.status, 401);
});
