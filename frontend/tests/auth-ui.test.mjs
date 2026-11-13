import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  INTERNAL_SERVICE_TOKEN_HEADER,
  LOCAL_SESSION_COOKIE_NAME,
  WEB_SESSION_COOKIE_NAME,
} from "../app/api/v1/runtime-bff.ts";
import {
  applyUnauthorizedNavigation,
  PRESERVE_UNAUTHORIZED_RESPONSE,
} from "../app/api-response-policy.ts";
import { ApiRequestError } from "../app/api.ts";
import {
  authenticatedLandingPath,
  parseWebSessionStatus,
} from "../app/auth-status.ts";
import { establishAutomaticOwnerSession } from "../app/login/automatic-owner-session.ts";
import { guardAuthenticatedNavigation } from "../app/navigation-session-guard.ts";

const SERVICE_TOKEN = "test-only-next-service-token-000000000001";
const SESSION_TOKEN = "test-only-browser-session-token-000000000001";
const ENVIRONMENT = {
  API_INTERNAL_URL: "http://api:8000",
  INTERNAL_SERVICE_TOKEN: SERVICE_TOKEN,
  WEB_PUBLIC_ORIGIN: "http://localhost:3000",
};

const READY_CAPABILITIES = {
  schema_version: 4,
  edition: "SERVER",
  identity_mode: "AUTHENTICATED",
  installation_mode: "MULTI_USER",
  setup_state: "READY",
  capabilities: {
    auth_ui: true,
    automatic_owner_session: false,
    registration: true,
    invitations: true,
    user_management: true,
    group_management: false,
    workspace_switching: false,
    system_status: false,
    desktop_supervisor: false,
  },
  version: { app: "0.1.0", api: "v1" },
};

const COMPLETED_SESSION = {
  authenticated: true,
  user_id: "user-a",
  active_workspace_id: "workspace-a",
  user: { id: "user-a", username: "reader", display_name: "读者" },
  workspace: { id: "workspace-a", name: "论文文库" },
  onboarding_required: false,
  can_manage_users: false,
  can_manage_invitations: false,
};

function statusError(status) {
  return new ApiRequestError(status, `HTTP ${status}`);
}

function navigationRequest(pathname, cookie = SESSION_TOKEN) {
  return new Request(`http://localhost:3000${pathname}`, {
    headers: cookie === null ? {} : { Cookie: `${WEB_SESSION_COOKIE_NAME}=${cookie}` },
  });
}

function guardFetch({ capabilities = READY_CAPABILITIES, session = COMPLETED_SESSION } = {}) {
  const calls = [];
  return {
    calls,
    fetchImplementation: async (url, init) => {
      calls.push({ url: String(url), init });
      const pathname = new URL(String(url)).pathname;
      if (pathname.endsWith("/runtime/capabilities")) return Response.json(capabilities);
      if (pathname.endsWith("/auth/status")) {
        return session instanceof Response ? session : Response.json(session);
      }
      throw new Error(`Unexpected guard URL: ${url}`);
    },
  };
}

test("authenticated Server runtime provides a Chinese login flow with onboarding routing", async () => {
  const page = await readFile(new URL("../app/login/page.tsx", import.meta.url), "utf8");
  const form = await readFile(new URL("../app/login/login-form.tsx", import.meta.url), "utf8");
  const automaticEntry = await readFile(
    new URL("../app/login/single-user-session-entry.tsx", import.meta.url),
    "utf8",
  );
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");

  assert.match(page, /登录 AcadeMate Paper/);
  assert.match(page, /capabilities === null/);
  assert.match(page, /capabilities\.setup_state !== "READY"/);
  assert.match(page, /capabilities\.capabilities\.automatic_owner_session/);
  assert.match(form, /autoComplete="username"/);
  assert.match(form, /autoComplete="current-password"/);
  assert.match(form, /loginWebSession\(personal \? \{ password \}/);
  assert.match(form, /personal \? null : <label>/);
  assert.equal((form.match(/getWebSessionStatus\(\)/g) ?? []).length, 2);
  assert.match(form, /authenticatedLandingPath\(status\)/);
  assert.match(form, /setPassword\(""\)/);
  assert.doesNotMatch(form, /localStorage|sessionStorage|document\.cookie/);
  assert.match(api, /auth\/sessions/);
  assert.match(api, /auth\/status/);
  assert.match(api, /auth\/sessions\/current/);
  assert.match(automaticEntry, /establishAutomaticOwnerSession\(\)/);
  assert.match(automaticEntry, /window\.location\.replace\("\/library"\)/);
});

test("strict session decoder and landing helper fail closed around onboarding", () => {
  const completed = parseWebSessionStatus(COMPLETED_SESSION);
  assert.notEqual(completed, null);
  assert.equal(authenticatedLandingPath(completed), "/library");
  const pending = parseWebSessionStatus({ ...COMPLETED_SESSION, onboarding_required: true });
  assert.notEqual(pending, null);
  assert.equal(authenticatedLandingPath(pending), "/onboarding");

  for (const malformed of [
    { ...COMPLETED_SESSION, authenticated: false },
    { ...COMPLETED_SESSION, onboarding_required: "false" },
    { ...COMPLETED_SESSION, user_id: "other" },
    { ...COMPLETED_SESSION, workspace: { ...COMPLETED_SESSION.workspace, extra: true } },
    { ...COMPLETED_SESSION, role: "OWNER" },
  ]) assert.equal(parseWebSessionStatus(malformed), null);
});

test("automatic owner entry creates a session only after 401 and verifies the cookie once", async () => {
  let checks = 0;
  let creates = 0;
  const authenticated = await establishAutomaticOwnerSession({
    checkSession: async () => {
      checks += 1;
      if (checks === 1) throw statusError(401);
      return COMPLETED_SESSION;
    },
    createSession: async () => {
      creates += 1;
    },
  });
  assert.equal(authenticated, "authenticated");
  assert.equal(checks, 2);
  assert.equal(creates, 1);

  checks = 0;
  creates = 0;
  const reused = await establishAutomaticOwnerSession({
    checkSession: async () => {
      checks += 1;
      return COMPLETED_SESSION;
    },
    createSession: async () => {
      creates += 1;
    },
  });
  assert.equal(reused, "authenticated");
  assert.equal(checks, 1);
  assert.equal(creates, 0);
});

test("automatic owner entry stops when cookies are rejected or status is unavailable", async () => {
  let creates = 0;
  const cookieRejected = await establishAutomaticOwnerSession({
    checkSession: async () => {
      throw statusError(401);
    },
    createSession: async () => {
      creates += 1;
    },
  });
  assert.equal(cookieRejected, "cookie-rejected");
  assert.equal(creates, 1);

  await assert.rejects(
    establishAutomaticOwnerSession({
      checkSession: async () => {
        throw statusError(503);
      },
      createSession: async () => {
        throw new Error("must not create");
      },
    }),
    (error) => error instanceof Error && error.status === 503,
  );
});

test("navigation guard orders setup, login, onboarding, then the target", async () => {
  const notReadyFetch = guardFetch({
    capabilities: {
      ...READY_CAPABILITIES,
      installation_mode: null,
      setup_state: "AWAITING_OWNER",
      capabilities: {
        ...READY_CAPABILITIES.capabilities,
        auth_ui: false,
        registration: false,
        invitations: false,
        user_management: false,
      },
    },
  });
  const notReady = await guardAuthenticatedNavigation(navigationRequest("/library", null), {
    environment: ENVIRONMENT,
    fetchImplementation: notReadyFetch.fetchImplementation,
  });
  assert.equal(notReady.status, 307);
  assert.equal(notReady.headers.get("location"), "http://localhost:3000/setup");
  assert.equal(notReadyFetch.calls.length, 1);

  const missingFetch = guardFetch();
  const missing = await guardAuthenticatedNavigation(navigationRequest("/library", null), {
    environment: ENVIRONMENT,
    fetchImplementation: missingFetch.fetchImplementation,
  });
  assert.equal(missing.status, 307);
  assert.equal(missing.headers.get("location"), "http://localhost:3000/login");
  assert.equal(missingFetch.calls.length, 1);

  const pendingFetch = guardFetch({
    session: { ...COMPLETED_SESSION, onboarding_required: true },
  });
  const pending = await guardAuthenticatedNavigation(navigationRequest("/settings"), {
    environment: ENVIRONMENT,
    fetchImplementation: pendingFetch.fetchImplementation,
  });
  assert.equal(pending.status, 307);
  assert.equal(pending.headers.get("location"), "http://localhost:3000/onboarding");
  assert.equal(pendingFetch.calls.length, 2);

  const onboardingFetch = guardFetch({
    session: { ...COMPLETED_SESSION, onboarding_required: true },
  });
  assert.equal(await guardAuthenticatedNavigation(navigationRequest("/onboarding"), {
    environment: ENVIRONMENT,
    fetchImplementation: onboardingFetch.fetchImplementation,
  }), null);

  const completedFetch = guardFetch();
  const completedOnboarding = await guardAuthenticatedNavigation(
    navigationRequest("/onboarding"),
    { environment: ENVIRONMENT, fetchImplementation: completedFetch.fetchImplementation },
  );
  assert.equal(completedOnboarding.status, 307);
  assert.equal(completedOnboarding.headers.get("location"), "http://localhost:3000/library");

  const targetFetch = guardFetch();
  assert.equal(await guardAuthenticatedNavigation(navigationRequest("/library"), {
    environment: ENVIRONMENT,
    fetchImplementation: targetFetch.fetchImplementation,
  }), null);
});

test("invalid sessions clear both cookies while malformed authority responses return 503", async () => {
  const invalidFetch = guardFetch({ session: new Response(null, { status: 401 }) });
  const invalid = await guardAuthenticatedNavigation(navigationRequest("/papers/guessed"), {
    environment: ENVIRONMENT,
    fetchImplementation: invalidFetch.fetchImplementation,
  });
  assert.equal(invalid.status, 307);
  assert.equal(invalid.headers.get("location"), "http://localhost:3000/login");
  const expiredCookies = invalid.headers.getSetCookie().join("\n");
  assert.match(expiredCookies, new RegExp(`${WEB_SESSION_COOKIE_NAME}=;`));
  assert.match(expiredCookies, new RegExp(`${LOCAL_SESSION_COOKIE_NAME}=;`));
  assert.match(expiredCookies, /Max-Age=0/);

  for (const malformedSession of [
    { ...COMPLETED_SESSION, onboarding_required: "no" },
    { ...COMPLETED_SESSION, unexpected: true },
  ]) {
    const malformedFetch = guardFetch({ session: malformedSession });
    const malformed = await guardAuthenticatedNavigation(navigationRequest("/library"), {
      environment: ENVIRONMENT,
      fetchImplementation: malformedFetch.fetchImplementation,
    });
    assert.equal(malformed.status, 503);
    assert.equal(malformed.headers.getSetCookie().length, 0);
    assert.equal(malformed.headers.get("cache-control"), "no-store");
  }
});

test("valid guard hops carry only server service authority and the opaque cookie", async () => {
  const operation = guardFetch();
  const response = await guardAuthenticatedNavigation(navigationRequest("/settings"), {
    environment: ENVIRONMENT,
    fetchImplementation: operation.fetchImplementation,
  });
  assert.equal(response, null);
  assert.deepEqual(operation.calls.map((call) => call.url), [
    "http://api:8000/api/v1/runtime/capabilities",
    "http://api:8000/api/v1/auth/status",
  ]);
  for (const call of operation.calls) {
    assert.equal(call.init.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
    assert.equal(call.init.headers.get("cookie"), `${WEB_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`);
  }
});

test("proxy matches every protected route including users and onboarding", async () => {
  const proxy = await readFile(new URL("../proxy.ts", import.meta.url), "utf8");
  assert.match(proxy, /guardAuthenticatedNavigation\(request\)/);
  assert.match(proxy, /NextResponse\.next\(\)/);
  assert.doesNotMatch(proxy, /x-user-id|x-workspace-id|RequestPrincipal\(/);
  for (const path of ["library", "papers", "arxiv", "settings", "users", "onboarding"]) {
    assert.match(proxy, new RegExp(`/${path}/:path\\*`));
  }
});

test("public proof and login failures preserve their forms instead of redirecting", async () => {
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
  for (const functionName of [
    "setupLocalOwner",
    "getSetupStatus",
    "loginWebSession",
    "registerWebSession",
    "getWebSessionStatus",
  ]) {
    const start = api.indexOf(`export async function ${functionName}`);
    const next = api.indexOf("export ", start + 10);
    assert.match(api.slice(start, next === -1 ? undefined : next), /PRESERVE_UNAUTHORIZED_RESPONSE/);
  }

  const redirects = [];
  const navigation = {
    pathname: "/register",
    replace(path) {
      redirects.push(path);
    },
  };
  assert.equal(
    applyUnauthorizedNavigation(401, PRESERVE_UNAUTHORIZED_RESPONSE, navigation),
    false,
  );
  assert.deepEqual(redirects, []);
  navigation.pathname = "/library";
  assert.equal(applyUnauthorizedNavigation(401, {}, navigation), true);
  assert.deepEqual(redirects, ["/login"]);
});

test("authenticated navigation exposes server-backed logout without JavaScript cookie access", async () => {
  const actions = await readFile(new URL("../app/session-actions.tsx", import.meta.url), "utf8");
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /<SessionActions \/>/);
  assert.match(actions, /getWebSessionStatus\(\)/);
  assert.match(actions, /await logoutWebSession\(\)/);
  assert.match(actions, /window\.location\.pathname !== "\/register"/);
  assert.doesNotMatch(actions, /document\.cookie|localStorage|sessionStorage/);
});

test("API error parsing never coerces structured backend details into UI text", async () => {
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
  assert.match(api, /typeof payload\.detail === "string"/);
  assert.doesNotMatch(api, /payload\.detail \?\?/);
});
