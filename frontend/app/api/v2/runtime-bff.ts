import {
  runtimeConfiguration, requestHeadersForBackend, responseHeadersForBrowser,
  hasResponseBody, isCanonicalSameOriginWrite, isJsonContentType,
  browserOriginForRequest,
  type RuntimeBffOptions,
} from "../v1/runtime-bff.ts";
import { requestPrivateApi } from "./private-api-request.ts";

export const V2_ACCESS_COOKIE = "academate_v2_access";
export const V2_REFRESH_COOKIE = "academate_v2_refresh";

function denied(status: number, code: string): Response {
  return Response.json({ code, message: code, details: null }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

export async function proxyV2ApiRequest(
  request: Request, path: readonly string[], options: RuntimeBffOptions = {},
): Promise<Response> {
  const config = runtimeConfiguration(options.environment ?? process.env);
  if (!config) return denied(503, "SERVER_BRIDGE_UNAVAILABLE");
  const method = request.method.toUpperCase();
  const url = new URL(request.url);
  // No native/Bearer path is exposed through a browser proxy; users cannot select an upstream
  // or inject a transport label. The fixed API service remains the authorization boundary.
  if (path.some((p) => !p || p === "." || p === ".." || /[\\/\x00-\x1f]/.test(p))) {
    return denied(400, "INVALID_API_PATH");
  }
  if (request.headers.has("authorization")) return denied(400, "AMBIGUOUS_CREDENTIALS");
  const browserOrigin = browserOriginForRequest(request, config.browserOrigins);
  if (browserOrigin === null || !isCanonicalSameOriginWrite(request, method, browserOrigin)) {
    return denied(403, "SAME_ORIGIN_REQUIRED");
  }
  const authPath = path.join("/");
  // The compatibility endpoint is private to the bridge. Browser callers request refresh.
  if (authPath === "auth/legacy-exchange") return denied(404, "NOT_FOUND");
  if (["auth/sessions", "auth/refresh", "auth/registrations", "setup/owner"].includes(authPath)
      && method === "POST"
      && (url.search || !isJsonContentType(request.headers.get("content-type")))) {
    return denied(400, "INVALID_AUTH_REQUEST");
  }
  const rawCookie = request.headers.get("cookie") ?? "";
  const cookies = rawCookie.split(";").map((v) => v.trim());
  const hasV2 = cookies.some((v) => v.startsWith(`${V2_ACCESS_COOKIE}=`));
  const hasRefresh = cookies.some((v) => v.startsWith(`${V2_REFRESH_COOKIE}=`));
  const hasLegacy = cookies.some((v) => v.startsWith("academate_session="));
  // Only an explicit same-origin POST can upgrade a legacy browser session. Resource requests
  // never downgrade to v1: doing so would bypass v2 authorization/paid-command contracts.
  const legacyExchange = method === "POST" && authPath === "auth/refresh"
    && !hasV2 && !hasRefresh && hasLegacy;
  const targetPath = legacyExchange ? ["auth", "legacy-exchange"] : path;
  const headers = requestHeadersForBackend(request.headers, config.internalServiceToken);
  headers.delete("authorization");
  headers.set("x-academate-client-transport", "web");
  headers.set("host", new URL(browserOrigin).host);
  headers.delete("cookie");
  const safeCookies = cookies.filter((v) => legacyExchange
    ? v.startsWith("academate_session=")
    : (v.startsWith(`${V2_ACCESS_COOKIE}=`) || v.startsWith(`${V2_REFRESH_COOKIE}=`)));

  if (safeCookies.length) headers.set("cookie", safeCookies.join("; "));
  const target = new URL(config.apiInternalUrl);
  target.pathname = `${target.pathname.replace(/\/$/, "")}/api/v2/${targetPath.map(encodeURIComponent).join("/")}`;
  target.search = url.search;
  const init: RequestInit & { duplex?: "half" } = {
    method, headers, redirect: "manual", cache: "no-store", signal: request.signal,
  };
  if (method !== "GET" && method !== "HEAD" && request.body !== null) {
    init.body = request.body;
    init.duplex = "half";
  }
  try {
    const response = await (options.fetchImplementation ?? requestPrivateApi)(target, init);
    const outgoing = responseHeadersForBrowser(response.headers, config.apiInternalUrl, browserOrigin);
    const location = response.headers.get("location");
    if (location) {
      const parsed = new URL(location, target);
      outgoing.delete("location");
      if (parsed.origin === target.origin && parsed.pathname.startsWith("/api/v2/")) {
        outgoing.set("location", new URL(parsed.pathname + parsed.search, browserOrigin).toString());
      }
    }
    return new Response(hasResponseBody(method, response.status) ? response.body : null, {
      status: response.status, statusText: response.statusText, headers: outgoing,
    });
  } catch {
    return denied(502, "SERVER_UNAVAILABLE");
  }
}
