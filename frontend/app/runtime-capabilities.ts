export const RUNTIME_CAPABILITY_SCHEMA_VERSION = 4 as const;

export const runtimeEditions = ["LOCAL", "SERVER"] as const;
export type RuntimeEdition = (typeof runtimeEditions)[number];

export const identityModes = ["TRUSTED_LOCAL", "AUTHENTICATED"] as const;
export type IdentityMode = (typeof identityModes)[number];

export const installationModes = ["SINGLE_USER", "MULTI_USER"] as const;
export type InstallationMode = (typeof installationModes)[number];

export const setupStates = [
  "NEW",
  "MIGRATING",
  "AWAITING_OWNER",
  "CONFIGURING",
  "VERIFYING_RUNTIME",
  "READY",
  "RECOVERABLE_FAILURE",
  "BLOCKED",
] as const;
export type SetupState = (typeof setupStates)[number];

export const runtimeUiCapabilityKeys = [
  "auth_ui",
  "automatic_owner_session",
  "registration",
  "invitations",
  "user_management",
  "group_management",
  "workspace_switching",
  "system_status",
  "desktop_supervisor",
] as const;
export type RuntimeUiCapabilityKey = (typeof runtimeUiCapabilityKeys)[number];

export type RuntimeUiCapabilities = Readonly<
  Record<RuntimeUiCapabilityKey, boolean>
>;

export type RuntimeVersion = Readonly<{
  app: string;
  api: string;
}>;

export type RuntimeStatusValue = string | number | boolean | null;
export type RuntimeStatusSummary = Readonly<Record<string, RuntimeStatusValue>>;

export type RuntimeCapabilities = Readonly<{
  schema_version: typeof RUNTIME_CAPABILITY_SCHEMA_VERSION;
  edition: RuntimeEdition;
  identity_mode: IdentityMode;
  installation_mode: InstallationMode | null;
  setup_state: SetupState;
  capabilities: RuntimeUiCapabilities;
  version: RuntimeVersion;
  runtime?: RuntimeStatusSummary;
}>;

export const serverOnlyUiRegistrations = [
  "login",
  "register",
  "invite",
  "users",
  "groups",
  "workspaceSwitcher",
  "adminConsole",
] as const;
export type ServerOnlyUiRegistration = (typeof serverOnlyUiRegistrations)[number];

export type NavigationRegistration = Readonly<{
  id: "library" | "settings" | "users";
  href: "/library" | "/settings" | "/users";
  label: "Library" | "Settings" | "用户";
}>;

export type RouteRegistration = Readonly<{
  id: "home" | "setup" | "login" | "register" | "onboarding" | "localLaunch" | "library" | "settings" | "users" | "paper" | "arxivImport";
  path: string;
  match: "exact" | "single-segment" | "catch-all";
  navigation?: NavigationRegistration;
}>;

const homeRoute: RouteRegistration = { id: "home", path: "/", match: "exact" };
const setupRoute: RouteRegistration = { id: "setup", path: "/setup", match: "exact" };
const setupRouteRegistry: readonly RouteRegistration[] = [homeRoute, setupRoute] as const;

const localLaunchRoute: RouteRegistration = {
  id: "localLaunch",
  path: "/local-launch",
  match: "exact",
};

const loginRoute: RouteRegistration = { id: "login", path: "/login", match: "exact" };
const registerRoute: RouteRegistration = { id: "register", path: "/register", match: "exact" };
const onboardingRoute: RouteRegistration = {
  id: "onboarding",
  path: "/onboarding",
  match: "exact",
};
const usersRoute: RouteRegistration = {
  id: "users",
  path: "/users",
  match: "exact",
  navigation: { id: "users", href: "/users", label: "用户" },
};

const readyRouteRegistry: readonly RouteRegistration[] = [
  homeRoute,
  // READY keeps this route registered only so its server component can redirect to /library.
  // No setup mutation surface is rendered once the backend has closed bootstrap.
  setupRoute,
  {
    id: "library",
    path: "/library",
    match: "exact",
    navigation: { id: "library", href: "/library", label: "Library" },
  },
  {
    id: "settings",
    path: "/settings",
    match: "exact",
    navigation: { id: "settings", href: "/settings", label: "Settings" },
  },
  { id: "paper", path: "/papers/", match: "single-segment" },
  { id: "arxivImport", path: "/arxiv/", match: "catch-all" },
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOneOf<const T extends readonly string[]>(
  values: T,
  value: unknown,
): value is T[number] {
  return typeof value === "string" && values.includes(value);
}

function parseUiCapabilities(value: unknown): RuntimeUiCapabilities | null {
  if (!isRecord(value)) return null;

  const parsed = {} as Record<RuntimeUiCapabilityKey, boolean>;
  for (const key of runtimeUiCapabilityKeys) {
    if (typeof value[key] !== "boolean") return null;
    parsed[key] = value[key];
  }
  return Object.freeze(parsed);
}

function parseVersion(value: unknown): RuntimeVersion | null {
  if (!isRecord(value)) return null;
  if (typeof value.app !== "string" || typeof value.api !== "string") return null;
  if (value.app.length === 0 || value.api.length === 0) return null;
  return Object.freeze({ app: value.app, api: value.api });
}

function parseRuntimeSummary(
  value: unknown,
): { valid: true; summary?: RuntimeStatusSummary } | { valid: false } {
  if (value === undefined) return { valid: true };
  if (!isRecord(value)) return { valid: false };

  const summary: Record<string, RuntimeStatusValue> = {};
  for (const [key, item] of Object.entries(value)) {
    if (
      item !== null
      && typeof item !== "string"
      && typeof item !== "number"
      && typeof item !== "boolean"
    ) return { valid: false };
    summary[key] = item;
  }
  return { valid: true, summary: Object.freeze(summary) };
}

function isEditionIdentityPairValid(
  edition: RuntimeEdition,
  identityMode: IdentityMode,
): boolean {
  return (
    (edition === "LOCAL" && identityMode === "TRUSTED_LOCAL")
    || (edition === "SERVER" && identityMode === "AUTHENTICATED")
  );
}

function localManifestEnablesServerUi(capabilities: RuntimeUiCapabilities): boolean {
  return capabilities.auth_ui
    || capabilities.automatic_owner_session
    || capabilities.registration
    || capabilities.invitations
    || capabilities.user_management
    || capabilities.group_management
    || capabilities.workspace_switching;
}

function isInstallationStateValid(
  edition: RuntimeEdition,
  setupState: SetupState,
  installationMode: InstallationMode | null,
  capabilities: RuntimeUiCapabilities,
): boolean {
  if (setupState === "READY" && installationMode === null) return false;
  if (edition === "LOCAL" && installationMode === "MULTI_USER") return false;
  if (
    installationMode === null
    && (
      capabilities.auth_ui
      || capabilities.automatic_owner_session
      || capabilities.user_management
    )
  ) {
    return false;
  }
  if (
    capabilities.automatic_owner_session
    && (
      edition !== "SERVER"
      || setupState !== "READY"
      || installationMode !== "SINGLE_USER"
    )
  ) return false;
  const multiUserFeaturesEnabled = edition === "SERVER"
    && setupState === "READY"
    && installationMode === "MULTI_USER";
  if (
    capabilities.registration !== multiUserFeaturesEnabled
    || capabilities.invitations !== multiUserFeaturesEnabled
    || capabilities.user_management !== multiUserFeaturesEnabled
  ) return false;
  if (edition === "SERVER" && installationMode !== null) {
    if (!capabilities.auth_ui) return false;
  }
  return true;
}

/**
 * Decode the versioned server response without coercing browser-controlled strings.
 * Unknown response properties are ignored, but every property used for registration is
 * validated and LOCAL manifests that enable server-only UI are rejected fail-closed.
 */
export function parseRuntimeCapabilities(value: unknown): RuntimeCapabilities | null {
  if (!isRecord(value)) return null;
  if (value.schema_version !== RUNTIME_CAPABILITY_SCHEMA_VERSION) return null;
  if (!isOneOf(runtimeEditions, value.edition)) return null;
  if (!isOneOf(identityModes, value.identity_mode)) return null;
  if (value.installation_mode !== null && !isOneOf(installationModes, value.installation_mode)) {
    return null;
  }
  if (!isOneOf(setupStates, value.setup_state)) return null;

  const capabilities = parseUiCapabilities(value.capabilities);
  const version = parseVersion(value.version);
  const runtime = parseRuntimeSummary(value.runtime);
  if (capabilities === null || version === null || !runtime.valid) return null;
  if (!isEditionIdentityPairValid(value.edition, value.identity_mode)) return null;
  if (value.edition === "LOCAL" && localManifestEnablesServerUi(capabilities)) return null;
  if (!isInstallationStateValid(
    value.edition,
    value.setup_state,
    value.installation_mode,
    capabilities,
  )) return null;

  return Object.freeze({
    schema_version: RUNTIME_CAPABILITY_SCHEMA_VERSION,
    edition: value.edition,
    identity_mode: value.identity_mode,
    installation_mode: value.installation_mode,
    setup_state: value.setup_state,
    capabilities,
    version,
    ...(runtime.summary === undefined ? {} : { runtime: runtime.summary }),
  });
}

export function isServerOnlyUiRegistered(
  manifest: RuntimeCapabilities | null,
  registration: ServerOnlyUiRegistration,
): boolean {
  if (manifest === null || manifest.edition !== "SERVER") return false;

  switch (registration) {
    case "login":
      return manifest.capabilities.auth_ui;
    case "register":
      return manifest.capabilities.auth_ui && manifest.capabilities.registration;
    case "invite":
      return manifest.capabilities.invitations;
    case "users":
      return manifest.capabilities.user_management || manifest.capabilities.invitations;
    case "groups":
      return manifest.capabilities.group_management;
    case "workspaceSwitcher":
      return manifest.capabilities.workspace_switching;
    case "adminConsole":
      return manifest.capabilities.user_management && manifest.capabilities.group_management;
  }
}

export function isDesktopSupervisorAvailable(
  manifest: RuntimeCapabilities | null,
): boolean {
  return manifest?.edition === "LOCAL" && manifest.capabilities.desktop_supervisor;
}

export function getRegisteredRoutes(
  manifest: RuntimeCapabilities | null,
): readonly RouteRegistration[] {
  if (manifest === null) return [];
  if (manifest.setup_state !== "READY") {
    return manifest.edition === "LOCAL"
      ? [...setupRouteRegistry, localLaunchRoute]
      : setupRouteRegistry;
  }
  return [
    ...readyRouteRegistry,
    ...(manifest.edition === "LOCAL" ? [localLaunchRoute] : []),
    ...(manifest.capabilities.auth_ui ? [loginRoute, onboardingRoute] : []),
    ...(manifest.capabilities.registration ? [registerRoute] : []),
    ...(
      manifest.capabilities.user_management || manifest.capabilities.invitations
        ? [usersRoute]
        : []
    ),
  ];
}

export function getRegisteredNavigation(
  manifest: RuntimeCapabilities | null,
): readonly NavigationRegistration[] {
  return getRegisteredRoutes(manifest).flatMap((route) => (
    route.navigation === undefined ? [] : [route.navigation]
  ));
}

function routeMatches(registration: RouteRegistration, pathname: string): boolean {
  if (registration.match === "exact") return pathname === registration.path;
  if (!pathname.startsWith(registration.path)) return false;

  const remainder = pathname.slice(registration.path.length);
  if (remainder.length === 0) return false;
  return registration.match === "catch-all" || !remainder.includes("/");
}

export function isRouteRegistered(
  manifest: RuntimeCapabilities | null,
  pathname: string,
): boolean {
  return getRegisteredRoutes(manifest).some((route) => routeMatches(route, pathname));
}
