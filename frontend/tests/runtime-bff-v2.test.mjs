import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { apiFetch } from "../app/api-fetch.ts";
import { proxyV2ApiRequest, V2_ACCESS_COOKIE, V2_REFRESH_COOKIE } from "../app/api/v2/runtime-bff.ts";

for (const status of [200, 206]) {
  test(`v2 ${status} retains private no-store and identity Vary on JSON or asset responses`, async () => {
    const response = await proxyV2ApiRequest(
      request("papers/synthetic/assets/fixture.png", "GET", `${V2_ACCESS_COOKIE}=synthetic-access`),
      ["papers", "synthetic", "assets", "fixture.png"],
      {
        environment: ENVIRONMENT,
        fetchImplementation: async (_url, init) => {
          assert.equal(init.cache, "no-store");
          return new Response(status === 206 ? "fixture-range" : "{}", {
            status, headers: {
              "Cache-Control": "private, no-store",
              Vary: "Cookie, Authorization",
              "Content-Type": status === 206 ? "image/png" : "application/json",
              ...(status === 206 ? { "Content-Range": "bytes 0-12/32" } : {}),
            },
          });
        },
      },
    );
    assert.equal(response.status, status);
    assert.equal(response.headers.get("cache-control"), "private, no-store");
    assert.equal(response.headers.get("vary"), "Cookie, Authorization");
    assert.equal(response.headers.get("content-type"), status === 206 ? "image/png" : "application/json");
    if (status === 206) assert.equal(response.headers.get("content-range"), "bytes 0-12/32");
  });
}

const ENVIRONMENT = {
  API_INTERNAL_URL: "http://server:8000",
  INTERNAL_SERVICE_TOKEN: "test-only-legacy-upgrade-private-hop-000001",
  WEB_PUBLIC_ORIGIN: "http://localhost:3000",
};
const LEGACY = "academate_session=synthetic-legacy-cookie";
const BASE_HEADERS = { Host: "localhost:3000", Origin: "http://localhost:3000" };
const SESSION = {
  authenticated: true, user_id: "synthetic-user", active_workspace_id: "synthetic-workspace",
  session_id: "synthetic-client-session",
  user: { id: "synthetic-user", username: "reader", display_name: "Reader" },
  workspace: { id: "synthetic-workspace", name: "Library" },
  onboarding_required: false, can_manage_users: false, can_manage_invitations: false,
};

test("real HTTP bridge preserves browser Host, JSON, Cookies and Range without Node fetch rewriting", async (context) => {
  const seen = [];
  const backend = createServer(async (incoming, outgoing) => {
    const parts = [];
    for await (const part of incoming) parts.push(part);
    seen.push({ method: incoming.method, path: incoming.url, headers: incoming.headers,
      body: Buffer.concat(parts).toString() });
    outgoing.setHeader("Cache-Control", "private, no-store");
    outgoing.setHeader("Vary", "Cookie, Authorization");
    if (incoming.method === "POST") {
      outgoing.setHeader("Set-Cookie", [
        `${V2_ACCESS_COOKIE}=synthetic-access; Path=/; HttpOnly; SameSite=Strict`,
        `${V2_REFRESH_COOKIE}=synthetic-refresh; Path=/api/v2/auth/refresh; HttpOnly; SameSite=Strict`,
      ]);
      outgoing.setHeader("Content-Type", "application/json");
      outgoing.end('{"authenticated":true}');
    } else {
      outgoing.statusCode = 206;
      outgoing.setHeader("Content-Type", "image/png");
      outgoing.setHeader("Content-Range", "bytes 0-3/8");
      outgoing.end(Buffer.from([137, 80, 78, 71]));
    }
  });
  await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => backend.close(resolve)));
  const environment = { ...ENVIRONMENT, API_INTERNAL_URL: `http://127.0.0.1:${backend.address().port}` };
  const login = await proxyV2ApiRequest(request("auth/sessions", "POST", ""), ["auth", "sessions"], { environment });
  assert.deepEqual(await login.json(), { authenticated: true });
  assert.equal(login.headers.getSetCookie().length, 2);
  const image = await proxyV2ApiRequest(request("papers/synthetic/assets/a.png", "GET",
    `${V2_ACCESS_COOKIE}=synthetic-access`, { Range: "bytes=0-3" }),
  ["papers", "synthetic", "assets", "a.png"], { environment });
  assert.equal(image.status, 206);
  assert.deepEqual(new Uint8Array(await image.arrayBuffer()), new Uint8Array([137, 80, 78, 71]));
  assert.equal(image.headers.get("cache-control"), "private, no-store");
  assert.equal(image.headers.get("vary"), "Cookie, Authorization");
  assert.equal(image.headers.get("content-range"), "bytes 0-3/8");
  assert.equal(seen.length, 2);
  for (const row of seen) {
    assert.equal(row.headers.host, "localhost:3000");
    assert.equal(row.headers.origin, "http://localhost:3000");
    assert.equal(row.headers["x-academate-client-transport"], "web");
    assert.equal(row.headers["x-academate-internal-service-token"], ENVIRONMENT.INTERNAL_SERVICE_TOKEN);
  }
  assert.equal(seen[0].body, "{}");
  assert.equal(seen[1].headers.range, "bytes=0-3");
  assert.equal(seen[1].headers.cookie, `${V2_ACCESS_COOKIE}=synthetic-access`);
});

test("private HTTP bridge forwards browser cancellation without retrying a write", { timeout: 5000 }, async (context) => {
  let calls = 0;
  let received;
  const arrived = new Promise((resolve) => { received = resolve; });
  const backend = createServer((incoming) => {
    calls += 1;
    incoming.resume();
    received();
  });
  await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
  context.after(() => {
    backend.closeAllConnections();
    return new Promise((resolve) => backend.close(resolve));
  });
  const controller = new AbortController();
  const pending = proxyV2ApiRequest(new Request("http://localhost:3000/api/v2/auth/sessions", {
    method: "POST", headers: { ...BASE_HEADERS, "Content-Type": "application/json" },
    body: "{}", signal: controller.signal,
  }), ["auth", "sessions"], {
    environment: { ...ENVIRONMENT, API_INTERNAL_URL: `http://127.0.0.1:${backend.address().port}` },
  });
  await arrived;
  controller.abort();
  assert.equal((await pending).status, 502);
  assert.equal(calls, 1);
});

function request(path, method = "GET", cookie = LEGACY, extra = {}) {
  return new Request(`http://localhost:3000/api/v2/${path}`, {
    method, headers: { ...BASE_HEADERS, Cookie: cookie, "Content-Type": "application/json", ...extra },
    ...(method === "GET" || method === "HEAD" ? {} : { body: "{}" }),
  });
}

for (const path of ["papers", "auth/status", "papers/synthetic/sources/SOURCE_PDF/download"]) {
  test(`legacy-cookie GET ${path} remains v2 and never transmits old credentials`, async () => {
    const response = await proxyV2ApiRequest(request(path), path.split("/"), {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        assert.equal(String(url), `http://server:8000/api/v2/${path}`);
        assert.equal(init.method, "GET");
        assert.equal(init.headers.has("cookie"), false);
        assert.equal(init.headers.get("x-academate-client-transport"), "web");
        return Response.json({ code: "INVALID_CLIENT_AUTHENTICATION" }, { status: 401 });
      },
    });
    assert.equal(response.status, 401);
    assert.equal(response.headers.getSetCookie().length, 0);
  });
}

test("paid retry with an old Cookie cannot bypass the v2 confirmation contract", async () => {
  const response = await proxyV2ApiRequest(request("papers/synthetic/retry", "POST"),
    ["papers", "synthetic", "retry"], {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        assert.equal(String(url), "http://server:8000/api/v2/papers/synthetic/retry");
        assert.equal(init.method, "POST");
        assert.equal(init.headers.has("cookie"), false);
        return Response.json({ code: "INVALID_CLIENT_AUTHENTICATION" }, { status: 401 });
      },
    });
  assert.equal(response.status, 401);
});

test("explicit same-origin POST refresh performs the one legacy-cookie exchange", async () => {
  const headers = new Headers({ "Cache-Control": "no-store" });
  headers.append("Set-Cookie", `${V2_ACCESS_COOKIE}=new-access; Path=/; HttpOnly; SameSite=Strict`);
  headers.append("Set-Cookie", `${V2_REFRESH_COOKIE}=new-refresh; Path=/api/v2/auth/refresh; HttpOnly; SameSite=Strict`);
  headers.append("Set-Cookie", "academate_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict");
  const response = await proxyV2ApiRequest(request("auth/refresh", "POST", `${LEGACY}; ignored=omit`),
    ["auth", "refresh"], {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        assert.equal(String(url), "http://server:8000/api/v2/auth/legacy-exchange");
        assert.equal(init.method, "POST");
        assert.equal(init.headers.get("cookie"), LEGACY);
        assert.equal(init.headers.get("origin"), "http://localhost:3000");
        assert.equal(init.headers.get("host"), "localhost:3000");
        assert.equal(init.headers.get("x-academate-internal-service-token"), ENVIRONMENT.INTERNAL_SERVICE_TOKEN);
        assert.equal(await new Response(init.body).text(), "{}");
        return new Response('{"authenticated":true}', { headers });
      },
    });
  assert.equal(response.status, 200);
  assert.equal(response.headers.getSetCookie().length, 3);
  assert.deepEqual(await response.json(), { authenticated: true });
});

for (const cookie of [
  `${LEGACY}; ${V2_ACCESS_COOKIE}=existing-access`,
  `${LEGACY}; ${V2_REFRESH_COOKIE}=existing-refresh`,
  "academate_local_session=retired-local-cookie",
]) {
  test(`refresh never exchanges legacy when v2 state or only retired local evidence exists: ${cookie.split(";").length}`, async () => {
    const response = await proxyV2ApiRequest(request("auth/refresh", "POST", cookie), ["auth", "refresh"], {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        assert.equal(String(url), "http://server:8000/api/v2/auth/refresh");
        assert.doesNotMatch(init.headers.get("cookie") ?? "", /academate_session=|academate_local_session=/);
        return new Response(null, { status: 401 });
      },
    });
    assert.equal(response.status, 401);
  });
}

test("GET refresh cannot exchange, and callers cannot invoke the private exchange path directly", async () => {
  let calls = 0;
  const options = {
    environment: ENVIRONMENT,
    fetchImplementation: async (url, init) => {
      calls += 1;
      assert.equal(String(url), "http://server:8000/api/v2/auth/refresh");
      assert.equal(init.method, "GET");
      return new Response(null, { status: 405 });
    },
  };
  assert.equal((await proxyV2ApiRequest(request("auth/refresh"), ["auth", "refresh"], options)).status, 405);
  assert.equal((await proxyV2ApiRequest(request("auth/legacy-exchange", "POST"),
    ["auth", "legacy-exchange"], options)).status, 404);
  assert.equal(calls, 1);
});

for (const [extra, suffix, expected] of [
  [{ Origin: "http://evil.test" }, "", 403],
  [{ Origin: "" }, "", 403],
  [{ Host: "evil.test" }, "", 403],
  [{ "Sec-Fetch-Site": "cross-site" }, "", 403],
  [{ "Content-Type": "text/plain" }, "", 400],
  [{}, "?unexpected=query", 400],
  [{ Authorization: "Bearer browser-must-not-send-this" }, "", 400],
]) {
  test(`legacy exchange rejects noncanonical refresh before sending private hop: ${JSON.stringify(extra)}${suffix}`, async () => {
    let called = false;
    const response = await proxyV2ApiRequest(request(`auth/refresh${suffix}`, "POST", LEGACY, extra),
      ["auth", "refresh"], {
        environment: ENVIRONMENT,
        fetchImplementation: async () => { called = true; throw new Error("must not fetch"); },
      });
    assert.equal(response.status, expected);
    assert.equal(called, false);
    assert.equal(response.headers.getSetCookie().length, 0);
  });
}

test("a legacy Web client upgrades on 401 and paid retry stays on v2 after conversion", async () => {
  const original = globalThis.fetch;
  const jar = new Map([["academate_session", "synthetic-legacy-cookie"]]);
  const paths = [];
  let exchanges = 0;
  let paidExecutions = 0;
  globalThis.fetch = async (input, init = {}) => {
    const target = new URL(String(input), "http://localhost:3000");
    const cookie = [...jar].filter(([name]) => name !== V2_REFRESH_COOKIE || target.pathname.endsWith("/auth/refresh"))
      .map(([name, value]) => `${name}=${value}`).join("; ");
    const browserRequest = new Request(target, {
      ...init, headers: { ...BASE_HEADERS, ...Object.fromEntries(new Headers(init.headers)), Cookie: cookie },
    });
    const response = await proxyV2ApiRequest(browserRequest, target.pathname.slice("/api/v2/".length).split("/"), {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, upstream) => {
        const path = new URL(url).pathname;
        paths.push(path);
        const cookie = upstream.headers.get("cookie") ?? "";
        if (path === "/api/v2/auth/legacy-exchange") {
          exchanges += 1;
          assert.equal(cookie, LEGACY);
          const headers = new Headers();
          headers.append("Set-Cookie", `${V2_ACCESS_COOKIE}=upgraded-access; Path=/; HttpOnly`);
          headers.append("Set-Cookie", `${V2_REFRESH_COOKIE}=upgraded-refresh; Path=/api/v2/auth/refresh; HttpOnly`);
          headers.append("Set-Cookie", "academate_session=; Path=/; Max-Age=0; HttpOnly");
          return new Response('{"authenticated":true}', { headers });
        }
        if (!cookie.includes(`${V2_ACCESS_COOKIE}=upgraded-access`)) return new Response(null, { status: 401 });
        if (path === "/api/v2/auth/status") return Response.json(SESSION);
        if (path === "/api/v2/papers/synthetic/retry") {
          const command = await new Response(upstream.body).json();
          if (!command.confirm_paid) return Response.json({ code: "PAID_OPERATION_CONFIRMATION_REQUIRED" }, { status: 409 });
          paidExecutions += 1;
          return Response.json({ retried_tasks: 1 });
        }
        throw new Error("unexpected request");
      },
    });
    for (const cookie of response.headers.getSetCookie()) {
      const [pair] = cookie.split(";");
      const [name, value] = pair.split("=");
      if (/Max-Age=0/i.test(cookie)) jar.delete(name); else jar.set(name, value);
    }
    return response;
  };
  try {
    const initial = await apiFetch("/api/v2/papers/synthetic/retry", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: '{"confirm_paid":false}',
    });
    assert.equal(initial.status, 409);
    assert.equal(exchanges, 1);
    assert.equal(paidExecutions, 0);
    const confirmed = await apiFetch("/api/v2/papers/synthetic/retry", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: '{"confirm_paid":true}',
    });
    assert.equal(confirmed.status, 200);
    assert.equal(paidExecutions, 1);
    assert.ok(paths.every((path) => path.startsWith("/api/v2/")));
    assert.equal(jar.has("academate_session"), false);
  } finally {
    globalThis.fetch = original;
  }
});
