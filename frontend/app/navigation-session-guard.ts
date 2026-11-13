import {
  LOCAL_SESSION_COOKIE_NAME,
  proxyApiRequest,
  type RuntimeBffOptions,
  WEB_SESSION_COOKIE_NAME,
  configuredBrowserOrigins,
  browserOriginForRequest,
} from "./api/v1/runtime-bff.ts";
import { proxyV2ApiRequest, V2_ACCESS_COOKIE } from "./api/v2/runtime-bff.ts";
import { parseWebSessionStatus } from "./auth-status.ts";
import { parseRuntimeCapabilities } from "./runtime-capabilities.ts";

const AUTH_STATUS_PATH = ["auth", "status"] as const;
const RUNTIME_CAPABILITIES_PATH = ["runtime", "capabilities"] as const;
const SESSION_COOKIE_NAMES = [
  V2_ACCESS_COOKIE,
  WEB_SESSION_COOKIE_NAME,
  LOCAL_SESSION_COOKIE_NAME,
] as const;

function requestHasSessionCookie(request: Request): boolean {
  return (request.headers.get("cookie") ?? "")
    .split(";")
    .map((item) => item.trim().split("=", 1)[0])
    .some((name) => SESSION_COOKIE_NAMES.includes(name as (typeof SESSION_COOKIE_NAMES)[number]));
}

function expiredCookie(name: string, secure: boolean): string {
  return [
    `${name}=`,
    "Path=/",
    "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    "Max-Age=0",
    "HttpOnly",
    "SameSite=Strict",
    ...(secure ? ["Secure"] : []),
  ].join("; ");
}

function navigationRedirect(
  browserOrigin: string,
  destination: "/setup" | "/login" | "/onboarding" | "/library",
  clearSessionCookies = false,
): Response {
  const headers = new Headers({
    "Cache-Control": "no-store",
    Location: new URL(destination, browserOrigin).toString(),
  });
  if (clearSessionCookies) {
    const secure = new URL(browserOrigin).protocol === "https:";
    for (const name of SESSION_COOKIE_NAMES) {
      headers.append("Set-Cookie", expiredCookie(name, secure));
    }
  }

  return new Response(null, { status: 307, headers });
}

function authenticationUnavailable(): Response {
  return new Response(
    "认证服务暂时不可用，请稍后重试。AcadeMate Paper 未加载任何受保护内容。",
    {
      status: 503,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": "text/plain; charset=utf-8",
      },
    },
  );
}

/**
 * Gate a protected page before it renders. The cookie remains opaque to Next: FastAPI validates
 * the database-backed session and still derives the RequestPrincipal for every API request.
 */
async function releaseResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // Connection cleanup is best-effort and never changes the authentication decision.
  }
}

export async function guardAuthenticatedNavigation(
  request: Request,
  options: RuntimeBffOptions = {},
): Promise<Response | null> {
  const headers = new Headers();
  // Next may build Request.url from the container listener. Preserve the browser Host.
  headers.set("host", request.headers.get("host") ?? new URL(request.url).host);
  const origin = request.headers.get("origin");
  if (origin !== null) headers.set("origin", origin);
  const origins = configuredBrowserOrigins(options.environment ?? process.env);
  if (origins === null) return authenticationUnavailable();
  const browserOrigin = browserOriginForRequest(new Request(request.url, { headers }), origins);
  if (browserOrigin === null) {
    return new Response("不支持此访问地址。", { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  const cookie = request.headers.get("cookie");
  if (cookie !== null) headers.set("cookie", cookie);
  const cookieNames = (cookie ?? "").split(";").map((item) => item.trim().split("=", 1)[0]);
  const legacy = !cookieNames.includes(V2_ACCESS_COOKIE)
    && cookieNames.some((name) => name === WEB_SESSION_COOKIE_NAME || name === LOCAL_SESSION_COOKIE_NAME);
  const proxy = legacy ? proxyApiRequest : proxyV2ApiRequest;
  const version = legacy ? "v1" : "v2";

  try {
    // Installation readiness precedes authentication. This extra, small no-store request is a
    // navigation admission check only; FastAPI remains the authorization boundary for every API.
    const capabilityResponse = await proxy(
      new Request(new URL(`/api/${version}/runtime/capabilities`, request.url), {
        cache: "no-store",
        headers,
        method: "GET",
        signal: request.signal,
      }),
      RUNTIME_CAPABILITIES_PATH,
      options,
    );
    if (capabilityResponse.status !== 200) {
      await releaseResponseBody(capabilityResponse);
      return authenticationUnavailable();
    }
    const capabilities = parseRuntimeCapabilities(await capabilityResponse.json());
    if (capabilities === null) return authenticationUnavailable();
    if (capabilities.setup_state !== "READY") {
      return navigationRedirect(browserOrigin, "/setup");
    }

    if (!requestHasSessionCookie(request)) {
      return navigationRedirect(browserOrigin, "/login");
    }

    const statusResponse = await proxy(
      new Request(new URL(`/api/${version}/auth/status`, request.url), {
        cache: "no-store",
        headers,
        method: "GET",
        signal: request.signal,
      }),
      AUTH_STATUS_PATH,
      options,
    );
    const status = statusResponse.status;
    if (status === 401) {
      await releaseResponseBody(statusResponse);
      return navigationRedirect(browserOrigin, "/login", true);
    }
    if (status !== 200) {
      await releaseResponseBody(statusResponse);
      return authenticationUnavailable();
    }
    const session = parseWebSessionStatus(await statusResponse.json());
    if (session === null) return authenticationUnavailable();

    const pathname = new URL(request.url).pathname;
    if (session.onboarding_required) {
      return pathname === "/onboarding"
        ? null
        : navigationRedirect(browserOrigin, "/onboarding");
    }
    return pathname === "/onboarding"
      ? navigationRedirect(browserOrigin, "/library")
      : null;
  } catch {
    return authenticationUnavailable();
  }
}
