import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  INTERNAL_SERVICE_TOKEN_HEADER,
  LOCAL_SESSION_COOKIE_NAME,
  WEB_SESSION_COOKIE_NAME,
  proxyApiRequest,
  proxyAutomaticOwnerSessionRequest,
} from "../app/api/v1/runtime-bff.ts";

const SERVICE_TOKEN = "test-only-next-service-token-000000000001";
const SESSION_TOKEN = "test-only-browser-session-token-000000000001";
const ENVIRONMENT = {
  API_INTERNAL_URL: "http://api:8000/runtime-root/",
  INTERNAL_SERVICE_TOKEN: SERVICE_TOKEN,
  WEB_PUBLIC_ORIGIN: "http://localhost:3000",
};

test("runtime BFF preserves query, range status, headers, cookies, and streaming body", async () => {
  const upstreamHeaders = new Headers({
    "Accept-Ranges": "bytes",
    "Connection": "x-remove-response",
    "Content-Disposition": "attachment; filename=paper.pdf",
    "Content-Length": "6",
    "Content-Range": "bytes 2-7/20",
    "Content-Type": "application/pdf",
    "X-Academate-Internal-Service-Token": "must-not-reach-browser",
    "X-Remove-Response": "hop-by-hop",
  });
  upstreamHeaders.append("Set-Cookie", "owner_session=one; HttpOnly; SameSite=Strict");
  upstreamHeaders.append("Set-Cookie", "ui_session=two; HttpOnly; SameSite=Strict");

  const encoder = new TextEncoder();
  let releaseSecondChunk = () => {};
  const secondChunkReady = new Promise((resolve) => {
    releaseSecondChunk = resolve;
  });
  const body = new ReadableStream({
    async start(controller) {
      controller.enqueue(encoder.encode("abc"));
      await secondChunkReady;
      controller.enqueue(encoder.encode("def"));
      controller.close();
    },
  });
  const upstream = new Response(body, { status: 206, headers: upstreamHeaders });
  let forwardedUrl = "";
  let forwardedInit;
  const request = new Request(
    "http://localhost:3000/api/v1/papers/paper%20id/sources/SOURCE_PDF/download?raw=1&x=a%20b",
    {
      headers: {
        Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
        Connection: "x-drop-request",
        "If-Range": '"etag-v1"',
        Range: "bytes=2-7",
        [INTERNAL_SERVICE_TOKEN_HEADER]: "browser-forged-token",
        "X-Drop-Request": "hop-by-hop",
        "X-PaperManager-Internal-Service-Identity": "browser-forged-identity",
        "X-User-Id": "browser-user",
        "X-Workspace-Id": "browser-workspace",
      },
    },
  );

  const response = await proxyApiRequest(
    request,
    ["papers", "paper id", "sources", "SOURCE_PDF", "download"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        forwardedUrl = String(url);
        forwardedInit = init;
        return upstream;
      },
    },
  );

  assert.equal(
    forwardedUrl,
    "http://api:8000/runtime-root/api/v1/papers/paper%20id/sources/SOURCE_PDF/download?raw=1&x=a%20b",
  );
  assert.equal(forwardedInit.method, "GET");
  assert.equal(forwardedInit.redirect, "manual");
  assert.equal(forwardedInit.headers.get("range"), "bytes=2-7");
  assert.equal(forwardedInit.headers.get("if-range"), '"etag-v1"');
  assert.equal(
    forwardedInit.headers.get("cookie"),
    `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
  );
  assert.equal(forwardedInit.headers.get("accept-encoding"), "identity");
  assert.equal(forwardedInit.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
  assert.equal(forwardedInit.headers.has("x-drop-request"), false);
  assert.equal(forwardedInit.headers.has("x-user-id"), false);
  assert.equal(forwardedInit.headers.has("x-workspace-id"), false);
  assert.equal(forwardedInit.headers.has("x-papermanager-internal-service-identity"), false);

  assert.equal(response.status, 206);
  assert.equal(response.headers.get("content-type"), "application/pdf");
  assert.equal(response.headers.get("content-disposition"), "attachment; filename=paper.pdf");
  assert.equal(response.headers.get("content-range"), "bytes 2-7/20");
  assert.equal(response.headers.get("accept-ranges"), "bytes");
  assert.equal(response.headers.get("content-length"), "6");
  assert.equal(response.headers.has("x-remove-response"), false);
  assert.equal(response.headers.has(INTERNAL_SERVICE_TOKEN_HEADER), false);
  assert.deepEqual(response.headers.getSetCookie(), [
    "owner_session=one; HttpOnly; SameSite=Strict",
    "ui_session=two; HttpOnly; SameSite=Strict",
  ]);

  const reader = response.body.getReader();
  const first = await reader.read();
  assert.equal(new TextDecoder().decode(first.value), "abc");
  releaseSecondChunk();
  const second = await reader.read();
  assert.equal(new TextDecoder().decode(second.value), "def");
  assert.equal((await reader.read()).done, true);
});

test("runtime BFF streams request bodies and strips browser service-token claims", async () => {
  const request = new Request("http://localhost:3000/api/v1/setup/owner", {
    method: "POST",
    headers: {
      Host: "localhost:3000",
      "Content-Type": "application/json",
      Origin: "http://localhost:3000",
      [INTERNAL_SERVICE_TOKEN_HEADER]: "forged-browser-token",
      "X-Academate-Internal-Service-Identity": "forged-browser-identity",
    },
    body: JSON.stringify({ bootstrap_token: "opaque-proof-reference" }),
  });
  let forwardedInit;

  const response = await proxyApiRequest(request, ["setup", "owner"], {
    environment: ENVIRONMENT,
    fetchImplementation: async (_url, init) => {
      forwardedInit = init;
      return new Response(null, {
        status: 201,
        headers: { "Content-Type": "application/json" },
      });
    },
  });

  assert.equal(response.status, 201);
  assert.equal(forwardedInit.body, request.body);
  assert.equal(forwardedInit.duplex, "half");
  assert.equal(forwardedInit.headers.get("content-type"), "application/json");
  assert.equal(forwardedInit.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
  assert.equal(forwardedInit.headers.has("x-academate-internal-service-identity"), false);
});

test("runtime BFF preserves HEAD, 416, and manual redirect semantics", async () => {
  const head = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers/paper-a/assets/figure.svg", {
      method: "HEAD",
      headers: { Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["papers", "paper-a", "assets", "figure.svg"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (_url, init) => {
        assert.equal(init.method, "HEAD");
        return new Response("upstream-must-not-be-buffered", {
          status: 200,
          headers: {
            "Content-Length": "27",
            "Content-Type": "image/svg+xml",
            "Content-Security-Policy": (
              "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            ),
          },
        });
      },
    },
  );
  assert.equal(head.status, 200);
  assert.equal(head.body, null);
  assert.equal(head.headers.get("content-length"), "27");
  assert.equal(head.headers.get("content-type"), "image/svg+xml");
  assert.equal(
    head.headers.get("content-security-policy"),
    "default-src 'none'; style-src 'unsafe-inline'; sandbox",
  );

  const unsatisfied = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers/paper-a/source", {
      headers: {
        Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
        Range: "bytes=999-1000",
      },
    }),
    ["papers", "paper-a", "source"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async () => new Response(null, {
        status: 416,
        headers: { "Content-Range": "bytes */42", "Content-Type": "application/problem+json" },
      }),
    },
  );
  assert.equal(unsatisfied.status, 416);
  assert.equal(unsatisfied.headers.get("content-range"), "bytes */42");
  assert.equal(unsatisfied.headers.get("content-type"), "application/problem+json");

  let redirectMode;
  const redirect = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/redirect-probe", {
      headers: { Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["redirect-probe"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (_url, init) => {
        redirectMode = init.redirect;
        return new Response(null, {
          status: 307,
          headers: { Location: "/api/v1/runtime/capabilities" },
        });
      },
    },
  );
  assert.equal(redirectMode, "manual");
  assert.equal(redirect.status, 307);
  assert.equal(redirect.headers.get("location"), "/api/v1/runtime/capabilities");
});

test("runtime BFF rewrites absolute private redirects onto the browser BFF", async () => {
  const privateRedirect = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers/?q=source", {
      headers: { Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["papers", ""],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async () => new Response(null, {
        status: 307,
        headers: {
          Location: (
            "http://api:8000/runtime-root/api/v1/papers?q=redirected#section"
          ),
        },
      }),
    },
  );
  assert.equal(privateRedirect.status, 307);
  assert.equal(
    privateRedirect.headers.get("location"),
    "http://localhost:3000/api/v1/papers?q=redirected#section",
  );
  assert.doesNotMatch(privateRedirect.headers.get("location"), /api:8000/);

  const externalRedirect = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/external", {
      headers: { Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["external"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async () => new Response(null, {
        status: 302,
        headers: { Location: "https://docs.example.invalid/help" },
      }),
    },
  );
  assert.equal(
    externalRedirect.headers.get("location"),
    "https://docs.example.invalid/help",
  );
});

test("runtime BFF fails closed without exposing private configuration", async () => {
  const missing = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers"),
    ["papers"],
    { environment: {} },
  );
  assert.equal(missing.status, 503);
  assert.doesNotMatch(await missing.clone().text(), /token|api:8000/i);

  const unavailable = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers", {
      headers: { Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["papers"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async () => {
        throw new Error(`never expose ${SERVICE_TOKEN} or http://api:8000`);
      },
    },
  );
  assert.equal(unavailable.status, 502);
  const errorBody = await unavailable.text();
  assert.equal(errorBody.includes(SERVICE_TOKEN), false);
  assert.equal(errorBody.includes("api:8000"), false);

  for (const invalidOrigin of [
    "",
    "not-a-url",
    "ftp://localhost",
    "http://localhost/path",
    "http://paper.example.com",
  ]) {
    const invalid = await proxyApiRequest(
      new Request("http://localhost:3000/api/v1/runtime/capabilities"),
      ["runtime", "capabilities"],
      { environment: { ...ENVIRONMENT, WEB_PUBLIC_ORIGIN: invalidOrigin } },
    );
    assert.equal(invalid.status, 503);
  }
});

test("runtime BFF never sends its service token to a public or ambiguous API target", async () => {
  let fetchCalls = 0;
  for (const apiUrl of [
    "https://collector.example.invalid",
    "http://api.example.invalid:8000",
    "http://localhost:8000",
    "http://api:8123",
  ]) {
    const response = await proxyApiRequest(
      new Request("http://localhost:3000/api/v1/runtime/capabilities"),
      ["runtime", "capabilities"],
      {
        environment: { ...ENVIRONMENT, API_INTERNAL_URL: apiUrl },
        fetchImplementation: async () => {
          fetchCalls += 1;
          return new Response(null, { status: 204 });
        },
      },
    );
    assert.equal(response.status, 503);
  }
  assert.equal(fetchCalls, 0);
});

test("runtime BFF accepts a numeric loopback API target", async () => {
  let forwardedToken = null;
  const response = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/runtime/capabilities"),
    ["runtime", "capabilities"],
    {
      environment: { ...ENVIRONMENT, API_INTERNAL_URL: "http://127.0.0.1:43117" },
      fetchImplementation: async (_url, init) => {
        forwardedToken = init.headers.get(INTERNAL_SERVICE_TOKEN_HEADER);
        return new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );
  assert.equal(response.status, 200);
  assert.equal(forwardedToken, SERVICE_TOKEN);
});

test("runtime BFF accepts exactly one authenticated Server session cookie", async () => {
  let forwardedCookie = null;
  const response = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers", {
      headers: { Cookie: `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}` },
    }),
    ["papers"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (_url, init) => {
        forwardedCookie = init.headers.get("cookie");
        return new Response("[]", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );
  assert.equal(response.status, 200);
  assert.equal(forwardedCookie, `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`);
});

test("runtime BFF permits login without a session but protects session status", async () => {
  let fetchCalls = 0;
  const login = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/auth/login", {
      method: "POST",
      headers: {
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ username: "owner", password: "not-logged" }),
    }),
    ["auth", "login"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (_url, init) => {
        fetchCalls += 1;
        assert.equal(init.headers.has("cookie"), false);
        assert.equal(init.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
        return Response.json({ authenticated: true }, {
          headers: { "Set-Cookie": `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}; HttpOnly` },
        });
      },
    },
  );
  assert.equal(login.status, 200);
  assert.match(login.headers.get("set-cookie"), /academate_session=/);

  const status = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/auth/status"),
    ["auth", "status"],
    { environment: ENVIRONMENT, fetchImplementation: async () => {
      fetchCalls += 1;
      return new Response(null, { status: 204 });
    } },
  );
  assert.equal(status.status, 401);
  assert.equal(fetchCalls, 1);
});

test("anonymous registration exposes only the exact canonical JSON POST", async () => {
  let fetchCalls = 0;
  let forwardedUrl = "";
  let forwardedInit;
  const valid = new Request("http://0.0.0.0:3000/api/v1/auth/register", {
    method: "POST",
    headers: {
      Host: "localhost:3000",
      Origin: "http://localhost:3000",
      "Sec-Fetch-Site": "same-origin",
      "Content-Type": "application/json; charset=utf-8",
      [INTERNAL_SERVICE_TOKEN_HEADER]: "forged",
    },
    body: JSON.stringify({
      invite_token: "invite-proof-not-logged-or-persisted-000000000001",
      username: "reader",
      display_name: "读者",
      password: "test-password-only",
    }),
  });
  const accepted = await proxyApiRequest(valid, ["auth", "register"], {
    environment: ENVIRONMENT,
    fetchImplementation: async (url, init) => {
      fetchCalls += 1;
      forwardedUrl = String(url);
      forwardedInit = init;
      return Response.json(
        { authenticated: true },
        { headers: { "Set-Cookie": `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}; HttpOnly` } },
      );
    },
  });
  assert.equal(accepted.status, 200);
  assert.equal(forwardedUrl, "http://api:8000/runtime-root/api/v1/auth/register");
  assert.equal(forwardedInit.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
  assert.equal(forwardedInit.headers.has("cookie"), false);
  assert.equal(forwardedInit.body, valid.body);
  assert.match(accepted.headers.get("set-cookie"), /academate_session=/);
  assert.equal(fetchCalls, 1);

  const canonicalHeaders = {
    Host: "localhost:3000",
    Origin: "http://localhost:3000",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
  };
  const invalid = [
    [new Request("http://localhost:3000/api/v1/auth/register", {
      headers: canonicalHeaders,
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "PUT", headers: canonicalHeaders, body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register?invite=forbidden", {
      method: "POST", headers: canonicalHeaders, body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register/extra", {
      method: "POST", headers: canonicalHeaders, body: "{}",
    }), ["auth", "register", "extra"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { ...canonicalHeaders, Host: "127.0.0.1:3000" },
      body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { Origin: canonicalHeaders.Origin, "Content-Type": "application/json" },
      body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { ...canonicalHeaders, Origin: "https://attacker.invalid" },
      body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { ...canonicalHeaders, Origin: "http://localhost:3000/" },
      body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { ...canonicalHeaders, "Sec-Fetch-Site": "cross-site" },
      body: "{}",
    }), ["auth", "register"]],
    [new Request("http://localhost:3000/api/v1/auth/register", {
      method: "POST",
      headers: { ...canonicalHeaders, "Content-Type": "text/plain" },
      body: "{}",
    }), ["auth", "register"]],
  ];
  for (const [request, path] of invalid) {
    const rejected = await proxyApiRequest(request, path, {
      environment: ENVIRONMENT,
      fetchImplementation: async () => {
        fetchCalls += 1;
        return new Response(null, { status: 204 });
      },
    });
    assert.equal(rejected.status, 403);
    assert.equal(rejected.headers.get("cache-control"), "no-store");
    assert.equal(rejected.headers.getSetCookie().length, 0);
  }
  assert.equal(fetchCalls, 1, "rejected registration variants must make zero upstream calls");
});

test("setup owner proof accepts only the exact no-query canonical JSON POST", async () => {
  let fetchCalls = 0;
  const baseHeaders = {
    Host: "localhost:3000",
    Origin: "http://localhost:3000",
    "Content-Type": "application/json",
  };
  const invalid = [
    new Request("http://localhost:3000/api/v1/setup/owner?attempt=1", {
      method: "POST", headers: baseHeaders, body: "{}",
    }),
    new Request("http://localhost:3000/api/v1/setup/owner", {
      method: "POST", headers: { ...baseHeaders, Host: "127.0.0.1:3000" }, body: "{}",
    }),
    new Request("http://localhost:3000/api/v1/setup/owner", {
      method: "POST", headers: { Host: "localhost:3000", "Content-Type": "application/json" }, body: "{}",
    }),
    new Request("http://localhost:3000/api/v1/setup/owner", {
      method: "POST", headers: { ...baseHeaders, "Content-Type": "text/plain" }, body: "{}",
    }),
    new Request("http://localhost:3000/api/v1/setup/owner", {
      method: "POST", headers: { ...baseHeaders, "Sec-Fetch-Site": "cross-site" }, body: "{}",
    }),
    new Request("http://localhost:3000/api/v1/setup/owner", { headers: baseHeaders }),
  ];
  for (const request of invalid) {
    const rejected = await proxyApiRequest(request, ["setup", "owner"], {
      environment: ENVIRONMENT,
      fetchImplementation: async () => {
        fetchCalls += 1;
        return new Response(null, { status: 204 });
      },
    });
    assert.equal(rejected.status, 403);
    assert.equal(rejected.headers.getSetCookie().length, 0);
  }
  assert.equal(fetchCalls, 0);
});

test("every authenticated unsafe BFF request binds Host, Origin, and Fetch Metadata", async () => {
  let fetchCalls = 0;
  const endpoints = [
    { method: "POST", url: "/api/v1/admin/invitations", path: ["admin", "invitations"] },
    { method: "POST", url: "/api/v1/admin/users/user-a/disable", path: ["admin", "users", "user-a", "disable"] },
    { method: "PUT", url: "/api/v1/credentials/default", path: ["credentials", "default"] },
  ];
  const canonicalHeaders = {
    Cookie: `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
    Host: "localhost:3000",
    Origin: "http://localhost:3000",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
  };
  for (const endpoint of endpoints) {
    const accepted = await proxyApiRequest(
      new Request(`http://0.0.0.0:3000${endpoint.url}`, {
        method: endpoint.method,
        headers: canonicalHeaders,
        body: "{}",
      }),
      endpoint.path,
      {
        environment: ENVIRONMENT,
        fetchImplementation: async () => {
          fetchCalls += 1;
          return new Response(null, { status: 204 });
        },
      },
    );
    assert.equal(accepted.status, 204);
  }
  assert.equal(fetchCalls, endpoints.length);

  const badHeaderSets = [
    { ...canonicalHeaders, Host: "127.0.0.1:3000" },
    Object.fromEntries(Object.entries(canonicalHeaders).filter(([name]) => name !== "Host")),
    Object.fromEntries(Object.entries(canonicalHeaders).filter(([name]) => name !== "Origin")),
    { ...canonicalHeaders, Origin: "https://attacker.invalid" },
    { ...canonicalHeaders, "Sec-Fetch-Site": "cross-site" },
  ];
  for (const endpoint of endpoints) {
    for (const headers of badHeaderSets) {
      const rejected = await proxyApiRequest(
        new Request(`http://localhost:3000${endpoint.url}`, {
          method: endpoint.method,
          headers,
          body: "{}",
        }),
        endpoint.path,
        {
          environment: ENVIRONMENT,
          fetchImplementation: async () => {
            fetchCalls += 1;
            return new Response(null, { status: 204 });
          },
        },
      );
      assert.equal(rejected.status, 403, `${endpoint.url} ${JSON.stringify(headers)}`);
      assert.equal(rejected.headers.getSetCookie().length, 0);
    }
  }
  assert.equal(fetchCalls, endpoints.length, "rejected unsafe variants must not reach upstream");
});

test("dedicated automatic owner session bridge uses only canonical same-origin server credentials", async () => {
  let forwardedUrl = "";
  let forwardedInit;
  const upstreamHeaders = new Headers({
    "Cache-Control": "no-store",
  });
  upstreamHeaders.append(
    "Set-Cookie",
    `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict`,
  );
  upstreamHeaders.append("Set-Cookie", "session_hint=ready; Path=/; SameSite=Strict");

  const response = await proxyAutomaticOwnerSessionRequest(
    new Request("http://0.0.0.0:3000/api/auth/single-user-session", {
      method: "POST",
      headers: {
        Cookie: `${WEB_SESSION_COOKIE_NAME}=browser-controlled-cookie`,
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
        "Sec-Fetch-Site": "same-origin",
        [INTERNAL_SERVICE_TOKEN_HEADER]: "browser-forged-token",
        "X-User-Id": "browser-forged-user",
      },
    }),
    {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        forwardedUrl = String(url);
        forwardedInit = init;
        return new Response(null, {
          status: 204,
          headers: upstreamHeaders,
        });
      },
    },
  );

  assert.equal(
    forwardedUrl,
    "http://api:8000/runtime-root/api/v1/auth/automatic-owner-session",
  );
  assert.equal(forwardedInit.method, "POST");
  assert.equal(forwardedInit.redirect, "manual");
  assert.equal(forwardedInit.body, undefined);
  assert.equal(forwardedInit.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
  assert.equal(forwardedInit.headers.has("cookie"), false);
  assert.equal(forwardedInit.headers.has("origin"), false);
  assert.equal(forwardedInit.headers.has("x-user-id"), false);
  assert.equal(response.status, 204);
  assert.equal(response.body, null);
  assert.deepEqual(response.headers.getSetCookie(), [
    `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict`,
    "session_hint=ready; Path=/; SameSite=Strict",
  ]);
});

test("automatic owner session bridge rejects noncanonical and cross-site requests before fetch", async () => {
  let fetchCalls = 0;
  const fetchImplementation = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };
  const requests = [
    new Request("http://localhost:3000/api/auth/single-user-session", {
      method: "POST",
      headers: { Host: "localhost:3000" },
    }),
    new Request("http://localhost:3000/api/auth/single-user-session", {
      method: "POST",
      headers: { Origin: "http://localhost:3000" },
    }),
    new Request("http://127.0.0.1:3000/api/auth/single-user-session", {
      method: "POST",
      headers: {
        Host: "127.0.0.1:3000",
        Origin: "http://localhost:3000",
      },
    }),
    new Request("http://localhost:3000/api/auth/single-user-session?retry=1", {
      method: "POST",
      headers: {
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
      },
    }),
    new Request("http://localhost:3000/api/auth/single-user-session", {
      method: "POST",
      headers: {
        Host: "localhost:3000",
        Origin: "https://attacker.example",
      },
    }),
    new Request("http://localhost:3000/api/auth/single-user-session", {
      method: "POST",
      headers: {
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
        "Sec-Fetch-Site": "cross-site",
      },
    }),
    new Request("http://localhost:3000/api/auth/single-user-session", {
      method: "GET",
      headers: {
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
      },
    }),
  ];
  for (const request of requests) {
    const response = await proxyAutomaticOwnerSessionRequest(request, {
      environment: ENVIRONMENT,
      fetchImplementation,
    });
    assert.equal(response.status, 403);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }

  const publicOrigin = "https://papers.example";
  const publicDeployment = await proxyAutomaticOwnerSessionRequest(
    new Request(`${publicOrigin}/api/auth/single-user-session`, {
      method: "POST",
      headers: {
        Host: "papers.example",
        Origin: publicOrigin,
        "Sec-Fetch-Site": "same-origin",
      },
    }),
    {
      environment: { ...ENVIRONMENT, WEB_PUBLIC_ORIGIN: publicOrigin },
      fetchImplementation,
    },
  );
  assert.equal(publicDeployment.status, 403);
  assert.equal(publicDeployment.headers.get("cache-control"), "no-store");
  assert.equal(fetchCalls, 0);
});

test("general /api/v1 bridge cannot reach the automatic owner session endpoint", async () => {
  let fetchCalls = 0;
  const response = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/auth/automatic-owner-session", {
      method: "POST",
      headers: {
        Cookie: `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
        Origin: "http://localhost:3000",
      },
    }),
    ["auth", "automatic-owner-session"],
    {
      environment: ENVIRONMENT,
      fetchImplementation: async () => {
        fetchCalls += 1;
        return new Response(null, { status: 204 });
      },
    },
  );
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(fetchCalls, 0);

  const route = await readFile(
    new URL("../app/api/auth/single-user-session/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(route, /export async function POST/);
  assert.match(route, /proxyAutomaticOwnerSessionRequest\(request\)/);
  assert.doesNotMatch(route, /export const (?:GET|PUT|PATCH|DELETE)/);
});

test("runtime BFF requires a local cookie and rejects cross-origin writes before proxying", async () => {
  let fetchCalls = 0;
  const fetchImplementation = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };

  const missing = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers"),
    ["papers"],
    { environment: ENVIRONMENT, fetchImplementation },
  );
  assert.equal(missing.status, 401);

  const crossOrigin = await proxyApiRequest(
    new Request("http://localhost:3000/api/v1/papers/import", {
      method: "POST",
      headers: {
        Cookie: `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
        Origin: "https://attacker.example",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ source: "2609.00001" }),
    }),
    ["papers", "import"],
    { environment: ENVIRONMENT, fetchImplementation },
  );
  assert.equal(crossOrigin.status, 403);
  assert.equal(fetchCalls, 0);
});

test("runtime BFF exchanges a launch proof without an existing session and forwards only cookie", async () => {
  let forwardedInit;
  const request = new Request(
    "http://localhost:3000/api/v1/local-session/exchange",
    {
      method: "POST",
      headers: {
        Host: "localhost:3000",
        Origin: "http://localhost:3000",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ launch_token: "one-use-proof-00000000000000000000000001" }),
    },
  );
  const response = await proxyApiRequest(request, ["local-session", "exchange"], {
    environment: ENVIRONMENT,
    fetchImplementation: async (_url, init) => {
      forwardedInit = init;
      return new Response(null, {
        status: 204,
        headers: {
          "Set-Cookie": (
            `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict`
          ),
        },
      });
    },
  });

  assert.equal(response.status, 204);
  assert.equal(response.body, null);
  assert.equal(forwardedInit.headers.has("cookie"), false);
  assert.equal(forwardedInit.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
  assert.match(response.headers.get("set-cookie"), /HttpOnly/);
  assert.match(response.headers.get("set-cookie"), /SameSite=Strict/);
  assert.doesNotMatch(await response.clone().text(), /one-use-proof|browser-session-token/);
});

test("runtime Route Handler has no body-buffering or build-time rewrite path", async () => {
  const route = await readFile(
    new URL("../app/api/v1/[[...path]]/route.ts", import.meta.url),
    "utf8",
  );
  const proxy = await readFile(
    new URL("../app/api/v1/runtime-bff.ts", import.meta.url),
    "utf8",
  );
  const nextConfig = await readFile(new URL("../next.config.ts", import.meta.url), "utf8");
  const dockerfile = await readFile(new URL("../Dockerfile", import.meta.url), "utf8");
  const implementation = `${route}\n${proxy}`;

  assert.doesNotMatch(
    implementation,
    /\b(?:request|upstream)\.(?:arrayBuffer|formData|json|text)\(/,
  );
  assert.match(proxy, /request\.body/);
  assert.match(proxy, /upstream\.body/);
  assert.match(proxy, /redirect: "manual"/);
  assert.match(route, /export const HEAD = proxy/);
  assert.match(route, /export const OPTIONS = proxy/);
  assert.doesNotMatch(nextConfig, /rewrites|API_INTERNAL_URL/);
  assert.doesNotMatch(dockerfile, /ARG API_INTERNAL_URL|ARG INTERNAL_SERVICE_TOKEN/);
  assert.doesNotMatch(dockerfile, /ENV API_INTERNAL_URL|ENV INTERNAL_SERVICE_TOKEN/);
  assert.match(dockerfile, /\.next\/standalone/);
  assert.match(dockerfile, /CMD \["node", "server\.js"\]/);
});
