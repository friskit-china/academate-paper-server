import {
  configuredBrowserOrigin,
  proxyApiRequest,
  type RuntimeBffOptions,
} from "../../v1/runtime-bff.ts";
import { sendSupervisorQuit, type SupervisorQuitOptions } from "./supervisor-control.ts";

type QuitRequestOptions = RuntimeBffOptions & SupervisorQuitOptions & {
  sendQuit?: (options: SupervisorQuitOptions) => Promise<void>;
};

function isSameOrigin(request: Request, environment?: RuntimeBffOptions["environment"]): boolean {
  const origin = request.headers.get("origin");
  const expected = configuredBrowserOrigin(environment);
  if (origin === null || expected === null) return false;
  try {
    return new URL(origin).origin === expected;
  } catch {
    return false;
  }
}

function noStoreResponse(detail: string, status: number, headers?: HeadersInit): Response {
  return Response.json(
    { detail },
    {
      status,
      headers: { "Cache-Control": "no-store", ...headers },
    },
  );
}

export async function requestLocalRuntimeQuit(
  request: Request,
  options: QuitRequestOptions = {},
): Promise<Response> {
  if (request.method.toUpperCase() !== "POST") {
    return noStoreResponse("Method not allowed.", 405, { Allow: "POST" });
  }
  if (!isSameOrigin(request, options.environment)) {
    return noStoreResponse("A same-origin request is required.", 403);
  }

  const timeout = options.timeoutMilliseconds ?? 2000;
  if (!Number.isFinite(timeout) || timeout <= 0) {
    return noStoreResponse("The local session could not be verified.", 503);
  }

  const statusHeaders = new Headers();
  const cookie = request.headers.get("cookie");
  if (cookie !== null) statusHeaders.set("cookie", cookie);
  const sessionStatus = await proxyApiRequest(
    new Request(new URL("/api/v1/local-session/status", request.url), {
      method: "GET",
      headers: statusHeaders,
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(timeout)]),
    }),
    ["local-session", "status"],
    {
      environment: options.environment,
      fetchImplementation: options.fetchImplementation,
    },
  );
  const sessionStatusCode = sessionStatus.status;
  if (sessionStatusCode === 401 || sessionStatusCode === 403) {
    await sessionStatus.body?.cancel();
    return noStoreResponse("A valid local browser session is required.", 401);
  }
  if (sessionStatusCode !== 200) {
    await sessionStatus.body?.cancel();
    return noStoreResponse("The local session could not be verified.", 503);
  }
  let sessionPayload: unknown;
  try {
    sessionPayload = await sessionStatus.json();
  } catch {
    return noStoreResponse("The local session could not be verified.", 503);
  }
  if (
    sessionPayload === null
    || typeof sessionPayload !== "object"
    || !("authenticated" in sessionPayload)
    || sessionPayload.authenticated !== true
    || !("setup_state" in sessionPayload)
    || sessionPayload.setup_state !== "READY"
  ) {
    return noStoreResponse("The local session could not be verified.", 503);
  }

  try {
    await (options.sendQuit ?? sendSupervisorQuit)({
      environment: options.environment,
      timeoutMilliseconds: timeout,
    });
  } catch {
    return noStoreResponse("The local Supervisor is unavailable.", 503);
  }
  return noStoreResponse("The local application is shutting down.", 202);
}
