import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  getRegisteredNavigation,
  isDesktopSupervisorAvailable,
  isRouteRegistered,
  isServerOnlyUiRegistered,
  parseRuntimeCapabilities,
  serverOnlyUiRegistrations,
} from "../app/runtime-capabilities.ts";

const localResponse = {
  schema_version: 4,
  edition: "LOCAL",
  identity_mode: "TRUSTED_LOCAL",
  installation_mode: "SINGLE_USER",
  setup_state: "READY",
  capabilities: {
    auth_ui: false,
    automatic_owner_session: false,
    registration: false,
    invitations: false,
    user_management: false,
    group_management: false,
    workspace_switching: false,
    system_status: false,
    desktop_supervisor: true,
  },
  version: { app: "0.1.0", api: "v1" },
  runtime: { ready: true, state: "READY" },
};

test("LOCAL registers current routes and no server-only UI", () => {
  const manifest = parseRuntimeCapabilities(localResponse);
  assert.notEqual(manifest, null);
  const navigation = getRegisteredNavigation(manifest);
  assert.deepEqual(
    navigation.map(({ href, label }) => ({ href, label })),
    [
      { href: "/library", label: "Library" },
      { href: "/settings", label: "Settings" },
    ],
  );
  const renderedNavigation = navigation.map(({ label }) => label).join(" ");
  for (const forbiddenLabel of [
    "Login",
    "Register",
    "Invite",
    "Users",
    "Groups",
    "Workspace switcher",
    "Admin Console",
  ]) {
    assert.equal(renderedNavigation.includes(forbiddenLabel), false, forbiddenLabel);
  }
  assert.equal(isRouteRegistered(manifest, "/library"), true);
  assert.equal(isRouteRegistered(manifest, "/settings"), true);
  assert.equal(isRouteRegistered(manifest, "/local-launch"), true);
  assert.equal(isRouteRegistered(manifest, "/papers/2fa8922a"), true);
  assert.equal(isRouteRegistered(manifest, "/arxiv/1706.03762v7"), true);
  for (const serverOnlyRoute of [
    "/login",
    "/register",
    "/onboarding",
    "/invite",
    "/users",
    "/groups",
    "/workspaces",
    "/admin",
  ]) {
    assert.equal(isRouteRegistered(manifest, serverOnlyRoute), false, serverOnlyRoute);
  }
  for (const registration of serverOnlyUiRegistrations) {
    assert.equal(isServerOnlyUiRegistered(manifest, registration), false, registration);
  }
});

test("desktop Supervisor controls are exposed only by a packaged LOCAL capability", () => {
  const local = parseRuntimeCapabilities(localResponse);
  assert.equal(isDesktopSupervisorAvailable(local), true);
  const compose = parseRuntimeCapabilities({
    ...localResponse,
    capabilities: { ...localResponse.capabilities, desktop_supervisor: false },
  });
  assert.equal(isDesktopSupervisorAvailable(compose), false);
  const server = parseRuntimeCapabilities({
    ...localResponse,
    edition: "SERVER",
    identity_mode: "AUTHENTICATED",
    installation_mode: "SINGLE_USER",
    capabilities: {
      ...localResponse.capabilities,
      auth_ui: true,
      desktop_supervisor: false,
    },
  });
  assert.equal(isDesktopSupervisorAvailable(server), false);
});

test("setup state fail-closes application routes and registers only first-run flow", () => {
  const manifest = parseRuntimeCapabilities({
    ...localResponse,
    installation_mode: null,
    setup_state: "AWAITING_OWNER",
  });
  assert.notEqual(manifest, null);
  assert.equal(isRouteRegistered(manifest, "/setup"), true);
  assert.equal(isRouteRegistered(manifest, "/local-launch"), true);
  assert.equal(isRouteRegistered(manifest, "/library"), false);
  assert.equal(isRouteRegistered(manifest, "/settings"), false);
  assert.deepEqual(getRegisteredNavigation(manifest), []);

  const ready = parseRuntimeCapabilities(localResponse);
  assert.equal(isRouteRegistered(ready, "/setup"), true);
});

test("authenticated Server registers login only after a persistent installation mode exists", () => {
  const awaiting = parseRuntimeCapabilities({
    ...localResponse,
    edition: "SERVER",
    identity_mode: "AUTHENTICATED",
    installation_mode: null,
    setup_state: "AWAITING_OWNER",
    capabilities: { ...localResponse.capabilities, desktop_supervisor: false },
  });
  assert.notEqual(awaiting, null);
  assert.equal(isRouteRegistered(awaiting, "/setup"), true);
  assert.equal(isRouteRegistered(awaiting, "/login"), false);
  assert.equal(isRouteRegistered(awaiting, "/local-launch"), false);

  const ready = parseRuntimeCapabilities({
    ...localResponse,
    edition: "SERVER",
    identity_mode: "AUTHENTICATED",
    installation_mode: "MULTI_USER",
    capabilities: {
      ...localResponse.capabilities,
      auth_ui: true,
      registration: true,
      invitations: true,
      user_management: true,
      desktop_supervisor: false,
    },
  });
  assert.notEqual(ready, null);
  assert.equal(isRouteRegistered(ready, "/login"), true);
  assert.equal(isRouteRegistered(ready, "/register"), true);
  assert.equal(isRouteRegistered(ready, "/onboarding"), true);
  assert.equal(isRouteRegistered(ready, "/library"), true);
  assert.equal(isRouteRegistered(ready, "/users"), true);
  assert.equal(isRouteRegistered(ready, "/local-launch"), false);

  const automaticSingleUser = parseRuntimeCapabilities({
    ...localResponse,
    edition: "SERVER",
    identity_mode: "AUTHENTICATED",
    installation_mode: "SINGLE_USER",
    capabilities: {
      ...localResponse.capabilities,
      auth_ui: true,
      automatic_owner_session: true,
      desktop_supervisor: false,
    },
  });
  assert.notEqual(automaticSingleUser, null);
  assert.equal(automaticSingleUser.capabilities.automatic_owner_session, true);
  assert.equal(isRouteRegistered(automaticSingleUser, "/login"), true);
});

test("capability decoder rejects old schemas and incoherent persisted modes", () => {
  for (const invalid of [
    { ...localResponse, schema_version: 3 },
    { ...localResponse, schema_version: 2 },
    { ...localResponse, schema_version: 1 },
    { ...localResponse, installation_mode: "MULTI_USER" },
    { ...localResponse, installation_mode: null },
    {
      ...localResponse,
      edition: "SERVER",
      identity_mode: "AUTHENTICATED",
      installation_mode: "SINGLE_USER",
      capabilities: {
        ...localResponse.capabilities,
        auth_ui: true,
        user_management: true,
        desktop_supervisor: false,
      },
    },
    {
      ...localResponse,
      edition: "SERVER",
      identity_mode: "AUTHENTICATED",
      installation_mode: "MULTI_USER",
      capabilities: {
        ...localResponse.capabilities,
        auth_ui: true,
        registration: true,
        invitations: true,
        automatic_owner_session: true,
        user_management: true,
        desktop_supervisor: false,
      },
    },
    {
      ...localResponse,
      edition: "SERVER",
      identity_mode: "AUTHENTICATED",
      installation_mode: null,
      setup_state: "AWAITING_OWNER",
      capabilities: { ...localResponse.capabilities, auth_ui: true, desktop_supervisor: false },
    },
  ]) assert.equal(parseRuntimeCapabilities(invalid), null);
});

test("registration, invitations, and user management are an atomic READY MULTI_USER capability set", () => {
  const enabled = parseRuntimeCapabilities({
    ...localResponse,
    edition: "SERVER",
    identity_mode: "AUTHENTICATED",
    installation_mode: "MULTI_USER",
    capabilities: {
      ...localResponse.capabilities,
      auth_ui: true,
      registration: true,
      invitations: true,
      user_management: true,
      desktop_supervisor: false,
    },
  });
  assert.notEqual(enabled, null);
  assert.equal(isServerOnlyUiRegistered(enabled, "register"), true);
  assert.equal(isServerOnlyUiRegistered(enabled, "invite"), true);
  assert.equal(isServerOnlyUiRegistered(enabled, "users"), true);

  for (const key of ["registration", "invitations", "user_management"]) {
    assert.equal(parseRuntimeCapabilities({
      ...enabled,
      capabilities: { ...enabled.capabilities, [key]: false },
    }), null, key);
  }
  assert.equal(parseRuntimeCapabilities({
    ...enabled,
    installation_mode: "SINGLE_USER",
  }), null);
  assert.equal(parseRuntimeCapabilities({
    ...enabled,
    installation_mode: null,
    setup_state: "AWAITING_OWNER",
    capabilities: {
      ...enabled.capabilities,
      auth_ui: false,
      registration: false,
      invitations: false,
      user_management: false,
    },
  })?.capabilities.registration, false);
});

test("setup form keeps proofs and passwords out of URLs and browser storage", async () => {
  const form = await readFile(
    new URL("../app/setup/setup-owner-form.tsx", import.meta.url),
    "utf8",
  );
  const api = await readFile(new URL("../app/api.ts", import.meta.url), "utf8");
  assert.match(form, /type="password"/);
  assert.match(api, /setup\/owner/);
  assert.match(api, /JSON\.stringify/);
  assert.doesNotMatch(form, /localStorage|sessionStorage|URLSearchParams|searchParams/);
});

test("browser-controlled strings cannot enable capabilities", () => {
  const manifest = parseRuntimeCapabilities({
    ...localResponse,
    auth_ui: true,
    registration: true,
    searchParams: "?auth_ui=true&registration=true&workspace_switching=true",
    localStorage: {
      auth_ui: "true",
      registration: "true",
      workspace_switching: "true",
    },
    window: { edition: "SERVER" },
  });
  assert.notEqual(manifest, null);
  for (const registration of serverOnlyUiRegistrations) {
    assert.equal(isServerOnlyUiRegistered(manifest, registration), false, registration);
  }

  assert.equal(parseRuntimeCapabilities({
    ...localResponse,
    capabilities: { ...localResponse.capabilities, registration: "true" },
  }), null);
  assert.equal(parseRuntimeCapabilities({
    ...localResponse,
    capabilities: { ...localResponse.capabilities, auth_ui: true },
  }), null);
});

test("capability loading has no browser-derived authority", async () => {
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  const serverLoader = await readFile(
    new URL("../app/runtime-capabilities.server.ts", import.meta.url),
    "utf8",
  );
  const provider = await readFile(
    new URL("../app/runtime-capability-provider.tsx", import.meta.url),
    "utf8",
  );
  const authoritySource = `${serverLoader}\n${provider}`;

  assert.match(serverLoader, /\/api\/v2\/runtime\/capabilities/);
  assert.match(serverLoader, /cache: "no-store"/);
  assert.match(layout, /export const dynamic = "force-dynamic"/);
  assert.doesNotMatch(authoritySource, /NEXT_PUBLIC_API_URL/);
  assert.doesNotMatch(authoritySource, /localStorage|searchParams|URLSearchParams|window\./);
});
