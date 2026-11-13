import assert from "node:assert/strict";
import test from "node:test";

import {
  browserOriginForRequest, configuredBrowserOrigins, runtimeConfiguration,
  proxyApiRequest, proxyAutomaticOwnerSessionRequest,
} from "../app/api/v1/runtime-bff.ts";
import { proxyV2ApiRequest } from "../app/api/v2/runtime-bff.ts";
import { guardAuthenticatedNavigation } from "../app/navigation-session-guard.ts";

const ORIGINS = [
  "http://paper-lan", "http://paper-lan:3000", "http://127.0.0.1:3000",
  "http://localhost:3000", "http://10.20.30.40", "http://10.20.30.40:3000",
  "http://100.90.80.70", "http://100.90.80.70:3000",
  "http://paper-server.local", "http://paper-server.local:3000",
  "http://future.example.invalid:8080",
];
const ENVIRONMENT = {
  API_INTERNAL_URL: "http://127.0.0.1:8000",
  INTERNAL_SERVICE_TOKEN: "synthetic-multi-origin-private-hop-00000001",
  WEB_PUBLIC_ORIGIN: ORIGINS[0], WEB_CLIENT_ORIGINS: ORIGINS.slice(1).join(","),
  WEB_ALLOW_INSECURE_HTTP: "true",
};
const CAPABILITIES = {
  schema_version: 4, edition: "SERVER", identity_mode: "AUTHENTICATED",
  installation_mode: "MULTI_USER", setup_state: "READY",
  capabilities: {
    auth_ui: true, automatic_owner_session: false, registration: true, invitations: true,
    user_management: true, group_management: false, workspace_switching: false,
    system_status: false, desktop_supervisor: false,
  }, version: { app: "0.1.0", api: "v2" },
};

function request(origin, { host = new URL(origin).host, suppliedOrigin = origin, method = "POST", extra = {} } = {}) {
  const headers = { Host: host, "Content-Type": "application/json", ...extra };
  if (suppliedOrigin !== null) headers.Origin = suppliedOrigin;
  return new Request("http://0.0.0.0:3000/api/v2/auth/sessions", {
    method, headers, ...(method === "GET" || method === "HEAD" ? {} : { body: "{}" }),
  });
}

test("external HTTP needs explicit opt-in while the existing loopback default remains valid", () => {
  assert.equal(runtimeConfiguration({ ...ENVIRONMENT, WEB_ALLOW_INSECURE_HTTP: undefined }), null);
  assert.equal(runtimeConfiguration({ ...ENVIRONMENT, WEB_ALLOW_INSECURE_HTTP: "false" }), null);
  const loopback = { ...ENVIRONMENT, WEB_PUBLIC_ORIGIN: "http://127.0.0.1:3000", WEB_CLIENT_ORIGINS: "", WEB_ALLOW_INSECURE_HTTP: undefined };
  assert.ok(runtimeConfiguration(loopback));
  assert.deepEqual(configuredBrowserOrigins(ENVIRONMENT), ORIGINS);
  assert.ok(runtimeConfiguration({ ...ENVIRONMENT, WEB_PUBLIC_ORIGIN: "https://reader.example.invalid", WEB_CLIENT_ORIGINS: "", WEB_ALLOW_INSECURE_HTTP: undefined }));
});

for (const origin of ORIGINS) {
  test(`explicit Web origin preserves its own authority and fixed API target: ${origin}`, async () => {
    let calls = 0;
    const response = await proxyV2ApiRequest(request(origin), ["auth", "sessions"], {
      environment: ENVIRONMENT,
      fetchImplementation: async (url, init) => {
        calls += 1;
        assert.equal(String(url), "http://127.0.0.1:8000/api/v2/auth/sessions");
        assert.equal(init.headers.get("host"), new URL(origin).host);
        assert.equal(init.headers.get("origin"), origin);
        assert.equal(init.headers.get("x-academate-client-transport"), "web");
        assert.equal(init.headers.get("x-academate-internal-service-token"), ENVIRONMENT.INTERNAL_SERVICE_TOKEN);
        return Response.json({ authenticated: true }, { headers: {
          "Set-Cookie": "academate_v2_access=synthetic; Path=/; HttpOnly; SameSite=Strict",
        } });
      },
    });
    assert.equal(response.status, 200);
    assert.equal(calls, 1);
    assert.match(response.headers.get("set-cookie"), /HttpOnly; SameSite=Strict/);
  });
}

for (const variation of [
  { host: "not-listed.example.invalid" },
  { host: "not-listed.example.invalid", extra: { "X-Forwarded-Host": "paper-lan", Forwarded: "host=paper-lan;proto=http" } },
  { host: "paper-lan.attacker.invalid" },
  { host: "%6dac-studio" },
  { host: "0x0a.0x14.0x1e.0x28", suppliedOrigin: "http://10.20.30.40" },
  { suppliedOrigin: "http://10.20.30.40:3000" },
  { suppliedOrigin: "http://paper-lan:3000" },
  { suppliedOrigin: null },
  { suppliedOrigin: "http://paper-lan/" },
  { extra: { "Sec-Fetch-Site": "cross-site" } },
  { extra: { "Sec-Fetch-Site": "same-site" } },
]) {
  test(`Host and Origin must match the same allowed entry: ${JSON.stringify(variation)}`, async () => {
    let calls = 0;
    const response = await proxyV2ApiRequest(request("http://paper-lan", variation), ["auth", "sessions"], {
      environment: ENVIRONMENT, fetchImplementation: async () => { calls += 1; return new Response(); },
    });
    assert.equal(response.status, 403);
    assert.equal(calls, 0);
  });
}

for (const invalid of [
  "http://*.example.invalid", "http://user:password@paper-lan", "http://paper-lan/path",
  "http://paper-lan?query=value", "http://paper-lan#fragment", "http://paper-lan:0",
  "http://paper-lan:70000", "http://mac-\tstudio", "http://paper-lan\\path", "file:///tmp/example",
]) {
  test(`invalid configured origins fail closed: ${JSON.stringify(invalid)}`, () => {
    assert.equal(runtimeConfiguration({ ...ENVIRONMENT, WEB_CLIENT_ORIGINS: invalid }), null);
  });
}

test("HTTP opt-in does not permit a caller-controlled upstream or automatic-owner login", async () => {
  assert.equal(runtimeConfiguration({ ...ENVIRONMENT, API_INTERNAL_URL: "http://future.example.invalid:8000" }), null);
  let calls = 0;
  const response = await proxyAutomaticOwnerSessionRequest(new Request("http://paper-lan/api/auth/single-user-session", {
    method: "POST", headers: { Host: "paper-lan", Origin: "http://paper-lan" },
  }), { environment: ENVIRONMENT, fetchImplementation: async () => { calls += 1; return new Response(); } });
  assert.equal(response.status, 403);
  assert.equal(calls, 0);
});

test("safe reads without Origin select one explicit Host and reject a mismatched supplied Origin", () => {
  const origins = configuredBrowserOrigins(ENVIRONMENT);
  assert.equal(browserOriginForRequest(request("http://paper-lan", { method: "GET", suppliedOrigin: null }), origins), "http://paper-lan");
  assert.equal(browserOriginForRequest(request("http://paper-lan", { method: "GET", suppliedOrigin: "http://paper-lan:3000" }), origins), null);
  assert.equal(browserOriginForRequest(request("http://paper-lan", { method: "GET", host: "paper-lan:80", suppliedOrigin: null }), origins), "http://paper-lan");
});

test("API redirects stay on the selected browser origin including default port 80", async () => {
  const response = await proxyV2ApiRequest(request("http://paper-lan"), ["auth", "sessions"], {
    environment: ENVIRONMENT, fetchImplementation: async () => new Response(null, {
      status: 307, headers: { Location: "http://127.0.0.1:8000/api/v2/auth/status" },
    }),
  });
  assert.equal(response.headers.get("location"), "http://paper-lan/api/v2/auth/status");
});

test("existing localhost v2 Cookies remain usable when the primary becomes paper-lan", async () => {
  const response = await proxyV2ApiRequest(new Request("http://0.0.0.0:3000/api/v2/auth/status", {
    headers: { Host: "localhost:3000", Cookie: "academate_v2_access=existing-synthetic-session" },
  }), ["auth", "status"], { environment: ENVIRONMENT, fetchImplementation: async (url, init) => {
    assert.equal(String(url), "http://127.0.0.1:8000/api/v2/auth/status");
    assert.equal(init.headers.get("host"), "localhost:3000");
    assert.equal(init.headers.get("cookie"), "academate_v2_access=existing-synthetic-session");
    return Response.json({ authenticated: true });
  } });
  assert.equal(response.status, 200);
});

test("a localhost legacy Cookie exchanges only through explicit same-origin refresh", async () => {
  const response = await proxyV2ApiRequest(new Request("http://0.0.0.0:3000/api/v2/auth/refresh", {
    method: "POST", headers: {
      Host: "localhost:3000", Origin: "http://localhost:3000", "Content-Type": "application/json",
      Cookie: "academate_session=existing-synthetic-legacy-session",
    }, body: "{}",
  }), ["auth", "refresh"], { environment: ENVIRONMENT, fetchImplementation: async (url, init) => {
    assert.equal(String(url), "http://127.0.0.1:8000/api/v2/auth/legacy-exchange");
    assert.equal(init.headers.get("host"), "localhost:3000");
    assert.equal(init.headers.get("origin"), "http://localhost:3000");
    assert.equal(init.headers.get("cookie"), "academate_session=existing-synthetic-legacy-session");
    return Response.json({ authenticated: true });
  } });
  assert.equal(response.status, 200);
});

test("navigation redirects use the validated browser authority rather than Next's container URL", async () => {
  for (const origin of ["http://paper-lan", "http://10.20.30.40:3000", "http://127.0.0.1:3000"]) {
    const response = await guardAuthenticatedNavigation(new Request("http://0.0.0.0:3000/library", {
      headers: { Host: new URL(origin).host },
    }), { environment: ENVIRONMENT, fetchImplementation: async () => Response.json(CAPABILITIES) });
    assert.equal(response.status, 307);
    assert.equal(response.headers.get("location"), `${origin}/login`);
  }
});

test("v1 compatibility writes also select the matching explicit browser origin", async () => {
  const response = await proxyApiRequest(new Request("http://0.0.0.0:3000/api/v1/auth/login", {
    method: "POST", headers: { Host: "10.20.30.40:3000", Origin: "http://10.20.30.40:3000", "Content-Type": "application/json" }, body: "{}",
  }), ["auth", "login"], { environment: ENVIRONMENT, fetchImplementation: async (url, init) => {
    assert.equal(String(url), "http://127.0.0.1:8000/api/v1/auth/login");
    assert.equal(init.headers.get("origin"), "http://10.20.30.40:3000");
    return Response.json({ authenticated: true });
  } });
  assert.equal(response.status, 200);
});

const allAddresses = {
  ...ENVIRONMENT,
  WEB_PUBLIC_ORIGIN: "https://127.0.0.1:8443", WEB_CLIENT_ORIGINS: "",
  WEB_ALLOW_INSECURE_HTTP: "false", SERVER_HTTPS_ANY_HOST: "true", SERVER_HTTPS_WEB_PORTS: "8443,443",
};

test("HTTPS on all addresses supports login, setup and navigation on new hosts", async () => {
  for (const origin of ["https://100.80.70.60:8443", "https://device.example.invalid", "https://[fd00::1]:8443"]) {
    for (const path of [["auth", "sessions"], ["setup", "owner"]]) {
      const response = await proxyV2ApiRequest(request(origin, { extra: {
        Forwarded: "host=elsewhere.invalid;proto=http", "X-Forwarded-Host": "elsewhere.invalid",
      } }), path, { environment: allAddresses, fetchImplementation: async (url, init) => {
        assert.equal(String(url), `http://127.0.0.1:8000/api/v2/${path.join("/")}`);
        assert.equal(init.headers.get("host"), new URL(origin).host);
        assert.equal(init.headers.get("origin"), origin);
        assert.equal(init.headers.get("forwarded"), null);
        assert.equal(init.headers.get("x-forwarded-host"), null);
        return new Response(null, { status: 204 });
      } });
      assert.equal(response.status, 204);
    }
    const response = await guardAuthenticatedNavigation(new Request("http://0.0.0.0:3000/library", {
      headers: { Host: new URL(origin).host },
    }), { environment: allAddresses, fetchImplementation: async () => Response.json(CAPABILITIES) });
    assert.equal(response.headers.get("location"), `${origin}/login`);
  }
});

test("HTTPS on all addresses verifies Host, Web port, and same-origin writes before forwarding", async () => {
  const origin = "https://new.example.invalid:8443";
  for (const variation of [
    { suppliedOrigin: "https://other.example.invalid:8443" }, { suppliedOrigin: null },
    { suppliedOrigin: "http://new.example.invalid:8443" },
    { host: "new.example.invalid:9443", suppliedOrigin: "https://new.example.invalid:9443" },
    { extra: { "Sec-Fetch-Site": "same-site" } },
    { host: "new..invalid:8443", method: "GET", suppliedOrigin: null },
    { host: "%6eew.example.invalid:8443", method: "GET", suppliedOrigin: null },
    { host: "127.1:8443", method: "GET", suppliedOrigin: null },
    { host: "new.example.invalid:8443/extra", method: "GET", suppliedOrigin: null },
  ]) {
    const req = request(origin, variation);
    const response = await proxyV2ApiRequest(req, ["auth", "sessions"], { environment: allAddresses,
      fetchImplementation: () => assert.fail("Unvalidated browser authority reached the backend") });
    assert.equal(response.status, 403, JSON.stringify(variation));
  }
  assert.equal(runtimeConfiguration({ ...allAddresses, WEB_PUBLIC_ORIGIN: "http://localhost:8443" }), null);
  assert.equal(runtimeConfiguration({ ...allAddresses, SERVER_HTTPS_WEB_PORTS: undefined }), null);
  const automatic = await proxyAutomaticOwnerSessionRequest(new Request("http://0.0.0.0:3000/api/auth/single-user-session", {
    method: "POST", headers: { Host: "127.0.0.1:8443", Origin: "https://127.0.0.1:8443" },
  }), { environment: allAddresses, fetchImplementation: () => assert.fail("Unexpected automatic session") });
  assert.equal(automatic.status, 403);
});
