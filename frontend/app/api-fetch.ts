// Native clients use the Server protocol directly. This adapter renews only same-origin Web
// requests; tokens remain in HttpOnly cookies and are never read by JavaScript.
import { parseWebSessionStatus } from "./auth-status.ts";

const API_PREFIX = "/api/v2";
const NO_REFRESH = new Set([
  "/api/v2/auth/sessions", "/api/v2/auth/refresh", "/api/v2/auth/registrations",
  "/api/v2/setup/owner", "/api/v2/setup/status", "/api/v2/runtime/capabilities",
]);
let refreshInFlight: Promise<boolean> | null = null;

function refreshable(input: RequestInfo | URL): boolean {
  const raw = input instanceof Request ? input.url : String(input);
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  try {
    const url = new URL(raw, origin);
    return url.origin === origin && url.pathname.startsWith(`${API_PREFIX}/`)
      && !NO_REFRESH.has(url.pathname);
  } catch {
    return false;
  }
}

async function renewWebSession(proactive = false): Promise<boolean> {
  if (refreshInFlight === null) {
    const refresh = async (): Promise<boolean> => {
      try {
        // Recheck *inside* the cross-tab lock: another tab may already have rotated cookies.
        const current = await fetch(`${API_PREFIX}/auth/status`, {
          credentials: "same-origin", cache: "no-store",
        });
        if (current.status === 200) {
          const status = parseWebSessionStatus(await current.json());
          if (status === null) return false;
          // Legacy automatic-owner sessions have no v2 access expiry. Never turn their
          // compatibility path into an unsolicited v2 login or refresh loop.
          if (!proactive || !status.expires_at
              || Date.parse(status.expires_at) - Date.now() > 6 * 60 * 1000) return true;
        } else {
          await current.body?.cancel();
          if (current.status !== 401) return false;
        }
        const response = await fetch(`${API_PREFIX}/auth/refresh`, {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json" }, body: "{}",
        });
        await response.body?.cancel();
        return response.ok;
      } catch {
        return false;
      }
    };
    // Cookie rotation must also be serialized between tabs when Web Locks is available.
    // A tab obtains fresh cookies after the preceding lock holder finishes its response.
    refreshInFlight = (async (): Promise<boolean> => {
      if (typeof navigator !== "undefined" && navigator.locks) {
        return await navigator.locks.request("academatepaper-v2-session-refresh", refresh);
      }
      return refresh();
    })().finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  // Preserve request bodies before the first fetch consumes a Request. Unreplayable streaming
  // uploads remain caller-controlled; this Web client currently sends JSON commands.
  const retryInput = input instanceof Request ? input.clone() : input;
  const streamingBody = typeof ReadableStream !== "undefined" && init?.body instanceof ReadableStream;
  const response = await fetch(input, init);
  if (response.status !== 401 || streamingBody || !refreshable(input)) return response;
  if (!await renewWebSession()) return response;
  await response.body?.cancel();
  return fetch(retryInput, init); // exactly one retry; never recursive refresh loops
}


export async function maintainWebSession(): Promise<boolean> {
  return renewWebSession(true);
}
