export type ExternalToolchainComponentState = "PRESENT" | "MISSING" | "INVALID";

export type InstallationMode = "SINGLE_USER" | "MULTI_USER";

export type ExternalToolchainStatus = {
  kind: "MACTEX";
  state: "AVAILABLE" | "MISSING" | "INVALID";
  official_url: "https://www.tug.org/mactex/";
  restart_required: boolean;
  components: {
    mactex: ExternalToolchainComponentState;
    ghostscript: ExternalToolchainComponentState;
    ghostscript_extras: ExternalToolchainComponentState;
  };
};

export type SetupStatus = {
  setup_state:
    | "NEW"
    | "MIGRATING"
    | "AWAITING_OWNER"
    | "CONFIGURING"
    | "VERIFYING_RUNTIME"
    | "READY"
    | "RECOVERABLE_FAILURE"
    | "BLOCKED";
  owner_required: boolean;
  mutations_open: boolean;
  state_version: number;
  installation_mode: InstallationMode | null;
  allowed_installation_modes: InstallationMode[];
  external_toolchain: ExternalToolchainStatus | null;
};

const SETUP_STATES = new Set<SetupStatus["setup_state"]>([
  "NEW",
  "MIGRATING",
  "AWAITING_OWNER",
  "CONFIGURING",
  "VERIFYING_RUNTIME",
  "READY",
  "RECOVERABLE_FAILURE",
  "BLOCKED",
]);
const TOOLCHAIN_STATES = new Set<ExternalToolchainStatus["state"]>([
  "AVAILABLE",
  "MISSING",
  "INVALID",
]);
const COMPONENT_STATES = new Set<ExternalToolchainComponentState>([
  "PRESENT",
  "MISSING",
  "INVALID",
]);
const INSTALLATION_MODES = new Set<InstallationMode>([
  "SINGLE_USER",
  "MULTI_USER",
]);
const OFFICIAL_MACTEX_URL = "https://www.tug.org/mactex/" as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}

function parseExternalToolchainComponents(
  value: unknown,
): ExternalToolchainStatus["components"] | undefined {
  if (!isRecord(value) || !hasExactKeys(value, [
    "ghostscript",
    "ghostscript_extras",
    "mactex",
  ])) return undefined;
  if (
    !COMPONENT_STATES.has(value.mactex as ExternalToolchainComponentState) ||
    !COMPONENT_STATES.has(value.ghostscript as ExternalToolchainComponentState) ||
    !COMPONENT_STATES.has(value.ghostscript_extras as ExternalToolchainComponentState)
  ) return undefined;
  return {
    mactex: value.mactex as ExternalToolchainComponentState,
    ghostscript: value.ghostscript as ExternalToolchainComponentState,
    ghostscript_extras: value.ghostscript_extras as ExternalToolchainComponentState,
  };
}

function parseExternalToolchain(value: unknown): ExternalToolchainStatus | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !hasExactKeys(value, [
    "components",
    "kind",
    "official_url",
    "restart_required",
    "state",
  ])) return undefined;
  const state = value.state;
  const components = parseExternalToolchainComponents(value.components);
  if (
    value.kind !== "MACTEX" ||
    !TOOLCHAIN_STATES.has(state as ExternalToolchainStatus["state"]) ||
    value.official_url !== OFFICIAL_MACTEX_URL ||
    typeof value.restart_required !== "boolean" ||
    components === undefined ||
    (state === "AVAILABLE" && value.restart_required) ||
    (state !== "AVAILABLE" && !value.restart_required) ||
    (state === "AVAILABLE" && Object.values(components).some((item) => item !== "PRESENT"))
  ) return undefined;
  return {
    kind: "MACTEX",
    state: state as ExternalToolchainStatus["state"],
    official_url: OFFICIAL_MACTEX_URL,
    restart_required: value.restart_required,
    components,
  };
}

export function parseSetupStatus(value: unknown): SetupStatus | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "allowed_installation_modes",
    "external_toolchain",
    "installation_mode",
    "mutations_open",
    "owner_required",
    "setup_state",
    "state_version",
  ])) return null;
  const externalToolchain = parseExternalToolchain(value.external_toolchain);
  const allowedInstallationModes = value.allowed_installation_modes;
  const installationMode = value.installation_mode;
  if (
    !SETUP_STATES.has(value.setup_state as SetupStatus["setup_state"]) ||
    typeof value.owner_required !== "boolean" ||
    typeof value.mutations_open !== "boolean" ||
    !Number.isSafeInteger(value.state_version) ||
    (value.state_version as number) < 0 ||
    !Array.isArray(allowedInstallationModes) ||
    allowedInstallationModes.some(
      (mode) => !INSTALLATION_MODES.has(mode as InstallationMode),
    ) ||
    new Set(allowedInstallationModes).size !== allowedInstallationModes.length ||
    (installationMode !== null && !INSTALLATION_MODES.has(installationMode as InstallationMode)) ||
    (installationMode === null && allowedInstallationModes.length === 0) ||
    (installationMode !== null && allowedInstallationModes.length !== 0) ||
    externalToolchain === undefined
  ) return null;
  return {
    setup_state: value.setup_state as SetupStatus["setup_state"],
    owner_required: value.owner_required,
    mutations_open: value.mutations_open,
    state_version: value.state_version as number,
    installation_mode: installationMode as InstallationMode | null,
    allowed_installation_modes: allowedInstallationModes as InstallationMode[],
    external_toolchain: externalToolchain,
  };
}
