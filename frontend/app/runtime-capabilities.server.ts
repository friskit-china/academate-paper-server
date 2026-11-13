import "server-only";

import {
  parseRuntimeCapabilities,
  type RuntimeCapabilities,
} from "./runtime-capabilities";

const RUNTIME_CAPABILITY_PATH = "/api/v2/runtime/capabilities";

function runtimeCapabilityEndpoint(): string {
  const apiInternalUrl = process.env.API_INTERNAL_URL;
  if (!apiInternalUrl) throw new Error("The runtime API endpoint is not configured.");
  return `${apiInternalUrl.replace(/\/$/, "")}${RUNTIME_CAPABILITY_PATH}`;
}

export async function loadRuntimeCapabilities(): Promise<RuntimeCapabilities | null> {
  try {
    const response = await fetch(runtimeCapabilityEndpoint(), { cache: "no-store" });
    if (!response.ok) return null;
    return parseRuntimeCapabilities(await response.json() as unknown);
  } catch {
    return null;
  }
}
