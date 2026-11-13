import "server-only";

import { parseSetupStatus, type SetupStatus } from "./setup-status";

const SETUP_STATUS_PATH = "/api/v2/setup/status";

function setupStatusEndpoint(): string {
  const apiInternalUrl = process.env.API_INTERNAL_URL;
  if (!apiInternalUrl) throw new Error("The runtime API endpoint is not configured.");
  return `${apiInternalUrl.replace(/\/$/, "")}${SETUP_STATUS_PATH}`;
}

export async function loadSetupStatus(): Promise<SetupStatus | null> {
  try {
    const response = await fetch(setupStatusEndpoint(), { cache: "no-store" });
    if (!response.ok) return null;
    return parseSetupStatus(await response.json() as unknown);
  } catch {
    return null;
  }
}
