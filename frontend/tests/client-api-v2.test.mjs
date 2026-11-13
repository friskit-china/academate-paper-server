import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { apiFetch, maintainWebSession } from "../app/api-fetch.ts";
import { parseWebSessionStatus } from "../app/auth-status.ts";
import { guardAuthenticatedNavigation } from "../app/navigation-session-guard.ts";
import { proxyV2ApiRequest, V2_ACCESS_COOKIE, V2_REFRESH_COOKIE } from "../app/api/v2/runtime-bff.ts";

const ENVIRONMENT = {
  API_INTERNAL_URL: "http://api:8000",
  INTERNAL_SERVICE_TOKEN: "test-only-next-v2-service-token-000000000001",
  WEB_PUBLIC_ORIGIN: "http://localhost:3000",
};
const CAPABILITIES = {
  schema_version: 4, edition: "SERVER", identity_mode: "AUTHENTICATED",
  installation_mode: "MULTI_USER", setup_state: "READY",
  capabilities: {
    auth_ui: true, automatic_owner_session: false, registration: true, invitations: true,
    user_management: true, group_management: false, workspace_switching: false,
    system_status: false, desktop_supervisor: false,
  },
  version: { app: "0.1.0", api: "v2" },
};
const STATUS = {
  authenticated: true, user_id: "reader", active_workspace_id: "workspace",
  user: { id: "reader", username: "reader", display_name: "Reader" },
  workspace: { id: "workspace", name: "Shared Library" }, onboarding_required: false,
  can_manage_users: false, can_manage_invitations: false, session_id: "client-session",
};

async function mockedFetch(implementation, run) {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  try { await run(); } finally { globalThis.fetch = original; }
}

test("native image, PDF download and derived-render URLs stay on v2 without apiFetch", async () => {
  const {
    paperAssetUrl, paperSourceDownloadUrl, paperBlockRenderAssetUrl, paperTableRenderAssetUrl,
  } = await import("../app/api.ts");
  const paperId = "synthetic paper/segment";
  assert.equal(paperAssetUrl(paperId, "figures/plot #1.svg"),
    "/api/v2/papers/synthetic%20paper%2Fsegment/assets/figures/plot%20%231.svg");
  assert.equal(paperSourceDownloadUrl(paperId, "SOURCE_PDF"),
    "/api/v2/papers/synthetic%20paper%2Fsegment/sources/SOURCE_PDF/download");
  const expected = "/api/v2/papers/synthetic%20paper%2Fsegment/blocks/block%2Fone/render-assets/table%20%231.png";
  assert.equal(paperBlockRenderAssetUrl(paperId, "block/one", "table #1.png"), expected);
  assert.equal(paperTableRenderAssetUrl(paperId, "block/one", "table #1.png"), expected);
});

test("Web requests renew an expired v2 session once for concurrent 401s", async () => {
  let renewals = 0;
  const calls = [];
  let renewed = false;
  await mockedFetch(async (url, init) => {
    calls.push(String(url));
    if (String(url).endsWith("/auth/refresh")) {
      renewals += 1;
      assert.equal(init.method, "POST");
      assert.equal(init.credentials, "same-origin");
      assert.equal(init.body, "{}");
      await new Promise((resolve) => setTimeout(resolve, 10));
      renewed = true;
      return Response.json({ authenticated: true });
    }
    return new Response(renewed ? "ready" : "expired", { status: renewed ? 200 : 401 });
  }, async () => {
    const responses = await Promise.all([
      apiFetch("/api/v2/papers"), apiFetch("/api/v2/settings/status"),
    ]);
    assert.deepEqual(responses.map((response) => response.status), [200, 200]);
    assert.equal(renewals, 1);
    assert.equal(calls.length, 6);
  });
});

test("failed refresh preserves original unauthorized response and never loops", async () => {
  const calls = [];
  await mockedFetch(async (url) => {
    calls.push(String(url));
    return new Response("expired", { status: 401 });
  }, async () => {
    const response = await apiFetch("/api/v2/papers");
    assert.equal(response.status, 401);
    assert.equal(await response.text(), "expired");
    assert.deepEqual(calls, ["/api/v2/papers", "/api/v2/auth/status", "/api/v2/auth/refresh"]);
  });
});

test("public auth failures, legacy paths, external URLs and 500s do not renew", async () => {
  const calls = [];
  await mockedFetch(async (url) => {
    calls.push(String(url));
    return new Response(null, { status: 401 });
  }, async () => {
    for (const path of [
      "/api/v2/auth/sessions", "/api/v2/auth/registrations", "/api/v2/auth/refresh",
      "/api/v2/setup/owner", "/api/v1/papers", "https://outside.test/api/v2/papers",
    ]) assert.equal((await apiFetch(path)).status, 401);
    assert.equal(calls.length, 6);
  });
  await mockedFetch(async () => new Response(null, { status: 500 }), async () => {
    assert.equal((await apiFetch("/api/v2/papers")).status, 500);
  });
});

test("retry preserves a Request body without exposing credentials or reading cookies", async () => {
  let requests = 0;
  const bodies = [];
  await mockedFetch(async (input) => {
    if (String(input).endsWith("/auth/refresh")) return new Response("{}", { status: 200 });
    if (String(input).endsWith("/auth/status")) return new Response(null, { status: 401 });
    requests += 1;
    bodies.push(await input.text());
    return new Response(null, { status: requests === 1 ? 401 : 200 });
  }, async () => {
    const response = await apiFetch(new Request("http://localhost/api/v2/papers/import", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: '{"source":"fixture"}',
    }));
    assert.equal(response.status, 200);
    assert.deepEqual(bodies, ['{"source":"fixture"}', '{"source":"fixture"}']);
  });
  const source = await readFile(new URL("../app/api-fetch.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /document\.cookie|localStorage|sessionStorage/);
});

test("navigation validates v2 sessions with browser Host even under a container URL", async () => {
  const calls = [];
  const response = await guardAuthenticatedNavigation(new Request("http://0.0.0.0:3000/library", {
    headers: { Host: "localhost:3000", Cookie: `${V2_ACCESS_COOKIE}=opaque` },
  }), {
    environment: ENVIRONMENT,
    fetchImplementation: async (url, init) => {
      calls.push([String(url), init]);
      return Response.json(String(url).endsWith("/auth/status") ? STATUS : CAPABILITIES);
    },
  });
  assert.equal(response, null);
  assert.deepEqual(calls.map(([url]) => url), [
    "http://api:8000/api/v2/runtime/capabilities", "http://api:8000/api/v2/auth/status",
  ]);
  for (const [, init] of calls) {
    assert.equal(init.headers.get("host"), "localhost:3000");
    assert.equal(init.headers.get("x-academate-client-transport"), "web");
  }
});

test("expired v2 navigation clears access but preserves refresh for LoginForm renewal", async () => {
  const response = await guardAuthenticatedNavigation(new Request("http://localhost:3000/library", {
    headers: { Cookie: `${V2_ACCESS_COOKIE}=expired` },
  }), {
    environment: ENVIRONMENT,
    fetchImplementation: async (url) => String(url).endsWith("/auth/status")
      ? new Response(null, { status: 401 }) : Response.json(CAPABILITIES),
  });
  assert.equal(response.status, 307);
  assert.match(response.headers.get("location"), /\/login$/);
  const removed = response.headers.getSetCookie().join("\n");
  assert.match(removed, new RegExp(`${V2_ACCESS_COOKIE}=;`));
  assert.doesNotMatch(removed, new RegExp(V2_REFRESH_COOKIE));
});

test("v2 session decoder accepts known session id but rejects unknown authority fields", () => {
  assert.equal(parseWebSessionStatus(STATUS).session_id, "client-session");
  assert.equal(parseWebSessionStatus({ ...STATUS, session_id: "" }), null);
  assert.equal(parseWebSessionStatus({ ...STATUS, role: "OWNER" }), null);
});

test("v2 BFF strips native credentials and preserves the two server Cookie headers", async () => {
  const upstream = new Headers({ "Content-Type": "application/json" });
  upstream.append("Set-Cookie", `${V2_ACCESS_COOKIE}=opaque; Path=/; HttpOnly; SameSite=Strict`);
  upstream.append("Set-Cookie", `${V2_REFRESH_COOKIE}=opaque; Path=/api/v2/auth/refresh; HttpOnly; SameSite=Strict`);
  const response = await proxyV2ApiRequest(new Request("http://localhost:3000/api/v2/auth/sessions", {
    method: "POST", headers: { Host: "localhost:3000", Origin: "http://localhost:3000",
      "Content-Type": "application/json", "X-Academate-Client-Transport": "native" },
    body: '{"username":"synthetic","password":"synthetic"}',
  }), ["auth", "sessions"], {
    environment: ENVIRONMENT,
    fetchImplementation: async (url, init) => {
      assert.equal(String(url), "http://api:8000/api/v2/auth/sessions");
      assert.equal(init.headers.get("x-academate-client-transport"), "web");
      assert.equal(init.headers.has("authorization"), false);
      return new Response('{"authenticated":true}', { headers: upstream });
    },
  });
  assert.equal(response.headers.getSetCookie().length, 2);
});

test("browser API uses v2 session lifecycle while retaining explicit automatic-owner endpoint", async () => {
  const source = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
  assert.match(source, /API_BASE = "\/api\/v2"/);
  assert.match(source, /auth\/sessions\/current.*method: "DELETE"/);
  assert.match(source, /auth\/registrations/);
  assert.match(source, /\/api\/auth\/single-user-session/);
  assert.doesNotMatch(source, /await fetch\(/);
});

test("available Web Locks serialize Cookie refresh across browser tabs", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const locks = [];
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { locks: { request: async (name, action) => { locks.push(name); return action(); } } },
  });
  let renewed = false;
  try {
    await mockedFetch(async (url) => {
      if (String(url).endsWith("/auth/refresh")) {
        renewed = true;
        return new Response("{}", { status: 200 });
      }
      return new Response(null, { status: renewed ? 200 : 401 });
    }, async () => {
      assert.equal((await apiFetch("/api/v2/papers")).status, 200);
    });
    assert.deepEqual(locks, ["academatepaper-v2-session-refresh"]);
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else delete globalThis.navigator;
  }
});

test("two independent browser tabs recheck auth under the same Web Lock before rotating", async () => {
  const tabA = await import("../app/api-fetch.ts?tab=first");
  const tabB = await import("../app/api-fetch.ts?tab=second");
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let queue = Promise.resolve();
  let renewals = 0;
  let cookieRotated = false;
  const lockNames = [];
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { locks: { request(name, action) {
      lockNames.push(name);
      const result = queue.then(action);
      queue = result.catch(() => {});
      return result;
    } } },
  });
  try {
    await mockedFetch(async (url) => {
      if (String(url).endsWith("/auth/status")) {
        return cookieRotated ? Response.json(STATUS) : new Response(null, { status: 401 });
      }
      if (String(url).endsWith("/auth/refresh")) {
        renewals += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        cookieRotated = true;
        return Response.json({ authenticated: true });
      }
      return new Response(null, { status: cookieRotated ? 200 : 401 });
    }, async () => {
      const responses = await Promise.all([
        tabA.apiFetch("/api/v2/papers"), tabB.apiFetch("/api/v2/papers"),
      ]);
      assert.deepEqual(responses.map((response) => response.status), [200, 200]);
    });
    assert.equal(renewals, 1);
    assert.deepEqual(lockNames, ["academatepaper-v2-session-refresh", "academatepaper-v2-session-refresh"]);
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else delete globalThis.navigator;
  }
});

test("keepalive renews a near-expiry access token but not a fresh or legacy session", async () => {
  let refreshes = 0;
  let status = { ...STATUS, expires_at: new Date(Date.now() + 5 * 60 * 1000).toISOString() };
  await mockedFetch(async (url) => {
    if (String(url).endsWith("/auth/status")) return Response.json(status);
    assert.equal(String(url), "/api/v2/auth/refresh");
    refreshes += 1;
    status = { ...STATUS, expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString() };
    return Response.json({ authenticated: true });
  }, async () => {
    assert.equal(await maintainWebSession(), true);
    assert.equal(refreshes, 1);
    assert.equal(await maintainWebSession(), true);
    assert.equal(refreshes, 1);
    status = STATUS; // legacy BFF-compatible status intentionally has no v2 expiry
    assert.equal(await maintainWebSession(), true);
    assert.equal(refreshes, 1);
  });
});

test("keepalive component checks visible protected pages on mount, interval and tab return", async () => {
  const component = await readFile(new URL("../app/session-keep-alive.tsx", import.meta.url), "utf8");
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.match(component, /^"use client"/);
  assert.match(component, /5 \* 60 \* 1000/);
  assert.match(component, /visibilitychange/);
  assert.match(component, /identity_mode !== "AUTHENTICATED"/);
  assert.match(component, /maintainWebSession\(\)/);
  assert.match(component, /clearInterval/);
  assert.doesNotMatch(component, /document\.cookie|localStorage|sessionStorage|createAutomaticOwnerSession/);
  assert.match(layout, /<SessionKeepAlive \/>/);
});

test("API errors retain readable v2 messages and metadata without coercing structured details", async () => {
  const { listPapers, ApiRequestError } = await import("../app/api.ts");
  await mockedFetch(async () => Response.json({
    code: "REVISION_CONFLICT", message: "The paper changed; reload before saving.",
    request_id: "safe-request-id", details: { secret: "never-coerce-details" },
  }, { status: 409 }), async () => {
    await assert.rejects(listPapers(), (error) => {
      assert.ok(error instanceof ApiRequestError);
      assert.equal(error.status, 409);
      assert.equal(error.message, "The paper changed; reload before saving.");
      assert.equal(error.code, "REVISION_CONFLICT");
      assert.equal(error.requestId, "safe-request-id");
      assert.doesNotMatch(error.message, /never-coerce-details/);
      return true;
    });
  });
  await mockedFetch(async () => Response.json({ detail: "Legacy message" }, { status: 400 }), async () => {
    await assert.rejects(listPapers(), (error) => error.message === "Legacy message" && error.code === null);
  });
  for (const payload of [null, [], { message: { unsafe: true }, detail: { unsafe: true } }]) {
    await mockedFetch(async () => Response.json(payload, { status: 400 }), async () => {
      await assert.rejects(listPapers(), (error) => error.message === "Request failed with HTTP 400.");
    });
  }
});

test("paper retry asks about model charges only after the Server requires confirmation", async () => {
  const { retryPaper } = await import("../app/api.ts");
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  const bodies = [];
  let prompts = 0;
  let approved = false;
  Object.defineProperty(globalThis, "window", {
    configurable: true, value: {
      location: { pathname: "/papers/synthetic", origin: "http://localhost", replace() {} },
      confirm(message) { prompts += 1; assert.match(message, /模型 API 费用/); return approved; },
    },
  });
  try {
    await mockedFetch(async (_url, init) => {
      const body = JSON.parse(init.body);
      bodies.push(body);
      return body.confirm_paid ? Response.json({ retried_tasks: 1, paper: {} }) : Response.json({
        code: "PAID_OPERATION_CONFIRMATION_REQUIRED", message: "Explicit confirmation is required.",
      }, { status: 409 });
    }, async () => {
      await assert.rejects(retryPaper("synthetic"), (error) => error.code === "PAID_OPERATION_CONFIRMATION_REQUIRED");
      assert.deepEqual(bodies, [{ confirm_paid: false }]);
      assert.equal(prompts, 1);
      approved = true;
      assert.equal((await retryPaper("synthetic")).retried_tasks, 1);
      assert.deepEqual(bodies.slice(1), [{ confirm_paid: false }, { confirm_paid: true }]);
      assert.equal(prompts, 2);
    });
    await mockedFetch(async (_url, init) => {
      assert.equal(JSON.parse(init.body).confirm_paid, false);
      return Response.json({ retried_tasks: 2, paper: {} });
    }, async () => {
      assert.equal((await retryPaper("synthetic")).retried_tasks, 2);
      assert.equal(prompts, 2); // deterministic jobs require no model-cost dialog
    });
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else delete globalThis.window;
  }
});
