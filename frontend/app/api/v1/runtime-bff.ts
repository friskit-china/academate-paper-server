export const INTERNAL_SERVICE_TOKEN_HEADER = "x-academate-internal-service-token";
export const LOCAL_SESSION_COOKIE_NAME = "academate_local_session";
export const WEB_SESSION_COOKIE_NAME = "academate_session";

const AUTOMATIC_OWNER_SESSION_BACKEND_PATH = [
  "auth",
  "automatic-owner-session",
] as const;
const AUTOMATIC_OWNER_SESSION_PUBLIC_PATH = "/api/auth/single-user-session";
const ANONYMOUS_REGISTRATION_BACKEND_PATH = ["auth", "register"] as const;
const SETUP_OWNER_BACKEND_PATH = ["setup", "owner"] as const;

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

const UNTRUSTED_BROWSER_IDENTITY_HEADERS = new Set([
  INTERNAL_SERVICE_TOKEN_HEADER,
  "x-user-id",
  "x-workspace-id",
  "x-group-id",
  "x-permissions",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
  "forwarded",
  "x-real-ip",
]);

export type RuntimeEnvironment = Record<string, string | undefined>;
type FetchImplementation = typeof fetch;

export type RuntimeBffOptions = {
  environment?: RuntimeEnvironment;
  fetchImplementation?: FetchImplementation;
};

type RuntimeBffConfiguration = {
  apiInternalUrl: URL;
  browserOrigin: string;
  browserOrigins: readonly string[];
  anyHostHttpsPorts: readonly number[];
  internalServiceToken: string;
};

function parseBrowserOrigin(rawOrigin: string, allowInsecureHttp: boolean): string | null {
  const raw = rawOrigin.trim();
  if (!raw || /[\x00-\x20\x7f\\]/.test(raw)) return null;
  try {
    const browserUrl = new URL(raw);
    if (
      !["http:", "https:"].includes(browserUrl.protocol)
      || !browserUrl.hostname
      || browserUrl.hostname.includes("*")
      || (browserUrl.port !== "" && Number(browserUrl.port) < 1)
      || browserUrl.username
      || browserUrl.password
      || browserUrl.pathname !== "/"
      || browserUrl.search
      || browserUrl.hash
      || (
        browserUrl.protocol === "http:"
        && !isBrowserLoopbackHost(browserUrl.hostname)
        && !allowInsecureHttp
      )
    ) {
      return null;
    }
    return browserUrl.origin;
  } catch {
    return null;
  }
}

export function configuredBrowserOrigins(
  environment: RuntimeEnvironment = process.env,
): readonly string[] | null {
  const optIn = environment.WEB_ALLOW_INSECURE_HTTP?.trim().toLowerCase();
  if (optIn && optIn !== "true" && optIn !== "false") return null;
  const values = [environment.WEB_PUBLIC_ORIGIN ?? "", ...(
    environment.WEB_CLIENT_ORIGINS?.split(",").map((value) => value.trim()).filter(Boolean) ?? []
  )];
  const origins = values.map((value) => parseBrowserOrigin(value, optIn === "true"));
  if (origins.some((origin) => origin === null)) return null;
  return [...new Set(origins as string[])];
}

export function configuredBrowserOrigin(
  environment: RuntimeEnvironment = process.env,
): string | null {
  return configuredBrowserOrigins(environment)?.[0] ?? null;
}

function matchesBrowserAuthority(authority: string, origin: string): boolean {
  if (!authority || authority.length > 512 || /[\x00-\x20\x7f\\/?#@,]/.test(authority)) return false;
  try {
    const configured = new URL(origin);
    const candidate = authority.toLowerCase();
    const defaultPort = configured.protocol === "http:" ? "80" : "443";
    return candidate === configured.host || (configured.port === ""
      && candidate === `${configured.hostname}:${defaultPort}`);
  } catch {
    return false;
  }
}

function httpsOriginForHost(host: string, ports: readonly number[]): string | null {
  try {
    // Next receives private HTTP from the TLS proxy. Only the preserved Host selects the
    // browser origin. Reject implicit URL rewrites such as percent escapes and short IPv4.
    const url = new URL(`https://${host}`);
    if (!matchesBrowserAuthority(host, url.origin) || !ports.includes(Number(url.port || "443"))) {
      return null;
    }
    if (!url.hostname.startsWith("[")) {
      const dnsLabel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
      if (url.hostname.length > 253 || !url.hostname.split(".").every((label) => dnsLabel.test(label))) return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

export function browserOriginForRequest(
  request: Request, origins: readonly string[], anyHostHttpsPorts: readonly number[] = [],
): string | null {
  const host = request.headers.get("host");
  if (host === null) return null;
  const supplied = request.headers.get("origin");
  if (anyHostHttpsPorts.length > 0) {
    const selected = httpsOriginForHost(host, anyHostHttpsPorts);
    return supplied === null || supplied === selected ? selected : null;
  }
  const matches = origins.filter((origin) => matchesBrowserAuthority(host, origin));
  if (supplied !== null) return matches.includes(supplied) ? supplied : null;
  // Do not guess the scheme if an operator configured both HTTP and HTTPS on one authority.
  return matches.length === 1 ? matches[0] : null;
}

function isNumericLoopbackHost(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

function isBrowserLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || isNumericLoopbackHost(hostname);
}

function isLoopbackBrowserOrigin(origin: string): boolean {
  try {
    return isBrowserLoopbackHost(new URL(origin).hostname);
  } catch {
    return false;
  }
}

function hasCanonicalAutomaticSessionTarget(
  request: Request,
  browserOrigin: string,
): boolean {
  try {
    const configuredUrl = new URL(browserOrigin);
    const requestUrl = new URL(request.url);
    // Next standalone deliberately constructs Request.url from its internal listen address
    // (0.0.0.0 in the container) when trustHostHeader is disabled. Validate the preserved inbound
    // Host separately, and use Request.url only for the exact path/query contract.
    return request.headers.get("host") === configuredUrl.host
      && requestUrl.pathname === AUTOMATIC_OWNER_SESSION_PUBLIC_PATH
      && requestUrl.search === ""
      && requestUrl.hash === "";
  } catch {
    return false;
  }
}

function isPrivateApiTarget(url: URL): boolean {
  if (url.protocol !== "http:") return false;

  // Packaged/diagnostic runtimes may select a dynamic numeric loopback port.  Never accept the
  // DNS name `localhost` here: a numeric address avoids resolver/proxy surprises before the BFF
  // attaches its server-only credential.
  if (isNumericLoopbackHost(url.hostname)) return true;

  // In the supported Compose topology the only non-loopback target is the exact service DNS name
  // on the private project network.  Keeping this allowlist deliberately tiny makes a typo or an
  // attacker-controlled public URL fail before fetch sees the internal service token.
  return ["api", "server"].includes(url.hostname) && (url.port === "" || url.port === "8000");
}

const SESSION_EXEMPT_REQUESTS = new Set([
  "GET health/live",
  "HEAD health/live",
  "GET health/ready",
  "HEAD health/ready",
  "GET runtime/capabilities",
  "HEAD runtime/capabilities",
  "GET setup/status",
  "HEAD setup/status",
  "POST setup/owner",
  "POST auth/login",
  "POST local-session/exchange",
]);

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function runtimeConfiguration(environment: RuntimeEnvironment): RuntimeBffConfiguration | null {
  const rawApiUrl = environment.API_INTERNAL_URL?.trim();
  const browserOrigins = configuredBrowserOrigins(environment);
  const browserOrigin = browserOrigins?.[0];
  const internalServiceToken = environment.INTERNAL_SERVICE_TOKEN;
  if (
    !rawApiUrl
    || !browserOrigin
    || !internalServiceToken
    || Buffer.byteLength(internalServiceToken) < 32
  ) {
    return null;
  }
  const anyHostHttpsPorts: number[] = [];
  if (environment.SERVER_HTTPS_ANY_HOST === "true") {
    const ports = environment.SERVER_HTTPS_WEB_PORTS?.split(",") ?? [];
    if (!browserOrigins!.every((origin) => origin.startsWith("https://"))
        || !ports.length || !ports.every((port) => /^[1-9][0-9]{0,4}$/.test(port) && Number(port) <= 65535)) {
      return null;
    }
    anyHostHttpsPorts.push(...ports.map(Number));
  }
  try {
    const apiInternalUrl = new URL(rawApiUrl);
    if (
      !isPrivateApiTarget(apiInternalUrl)
      || apiInternalUrl.username
      || apiInternalUrl.password
      || apiInternalUrl.search
      || apiInternalUrl.hash
    ) {
      return null;
    }
    return {
      apiInternalUrl,
      browserOrigin,
      browserOrigins: browserOrigins!,
      anyHostHttpsPorts,
      internalServiceToken,
    };
  } catch {
    return null;
  }
}

function connectionNamedHeaders(headers: Headers): Set<string> {
  const names = new Set<string>();
  for (const value of headers.get("connection")?.split(",") ?? []) {
    const name = value.trim().toLowerCase();
    if (name) names.add(name);
  }
  return names;
}

export function requestHeadersForBackend(source: Headers, serviceToken: string): Headers {
  const headers = new Headers();
  const connectionHeaders = connectionNamedHeaders(source);
  source.forEach((value, name) => {
    const normalized = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(normalized)
      || connectionHeaders.has(normalized)
      || UNTRUSTED_BROWSER_IDENTITY_HEADERS.has(normalized)
      || normalized.endsWith("-internal-service-identity")
      || normalized === "host"
    ) {
      return;
    }
    headers.append(name, value);
  });
  // Fetch implementations can transparently decompress while retaining origin headers. Asking the
  // private API for identity encoding keeps byte ranges and Content-Length exact end-to-end.
  headers.set("accept-encoding", "identity");
  headers.set(INTERNAL_SERVICE_TOKEN_HEADER, serviceToken);
  return headers;
}

function browserRedirectLocation(
  value: string,
  apiInternalUrl: URL,
  browserOrigin: string,
): string | null {
  let target: URL;
  try {
    target = new URL(value);
  } catch {
    if (!value.startsWith("//")) return value;
    target = new URL(value, apiInternalUrl);
  }
  if (target.origin !== apiInternalUrl.origin) return value;

  const basePath = apiInternalUrl.pathname.replace(/\/$/, "");
  const upstreamApiPath = `${basePath}/api/v1`;
  let publicPath: string;
  if (
    target.pathname === upstreamApiPath
    || target.pathname.startsWith(`${upstreamApiPath}/`)
  ) {
    publicPath = target.pathname.slice(basePath.length);
  } else if (
    target.pathname === "/api/v1"
    || target.pathname.startsWith("/api/v1/")
  ) {
    publicPath = target.pathname;
  } else {
    // Never disclose or navigate to an absolute private-service path outside the BFF surface.
    return null;
  }
  const browserTarget = new URL(browserOrigin);
  browserTarget.pathname = publicPath;
  browserTarget.search = target.search;
  browserTarget.hash = target.hash;
  return browserTarget.toString();
}

export function responseHeadersForBrowser(
  source: Headers,
  apiInternalUrl: URL,
  browserOrigin: string,
): Headers {
  const headers = new Headers();
  const connectionHeaders = connectionNamedHeaders(source);
  source.forEach((value, name) => {
    const normalized = name.toLowerCase();
    if (
      normalized === "set-cookie"
      || HOP_BY_HOP_HEADERS.has(normalized)
      || connectionHeaders.has(normalized)
      || UNTRUSTED_BROWSER_IDENTITY_HEADERS.has(normalized)
      || normalized.endsWith("-internal-service-identity")
    ) {
      return;
    }
    if (normalized === "location") {
      const location = browserRedirectLocation(value, apiInternalUrl, browserOrigin);
      if (location !== null) headers.append(name, location);
      return;
    }
    headers.append(name, value);
  });

  const cookieHeaders = source as Headers & { getSetCookie?: () => string[] };
  const setCookies = cookieHeaders.getSetCookie?.() ?? [];
  if (setCookies.length > 0) {
    for (const cookie of setCookies) headers.append("set-cookie", cookie);
  } else {
    const combinedCookie = source.get("set-cookie");
    if (combinedCookie !== null) headers.append("set-cookie", combinedCookie);
  }
  return headers;
}

function backendUrl(base: URL, pathSegments: readonly string[], incomingUrl: string): URL {
  const target = new URL(base);
  const basePath = target.pathname.replace(/\/$/, "");
  const encodedPath = pathSegments.map((segment) => encodeURIComponent(segment)).join("/");
  target.pathname = `${basePath}/api/v1${encodedPath ? `/${encodedPath}` : ""}`;
  target.search = new URL(incomingUrl).search;
  return target;
}

export function hasResponseBody(method: string, status: number): boolean {
  return method !== "HEAD" && status !== 204 && status !== 205 && status !== 304;
}

function requestKey(method: string, pathSegments: readonly string[]): string {
  return `${method} ${pathSegments.join("/")}`;
}

function isAutomaticOwnerSessionBackendPath(pathSegments: readonly string[]): boolean {
  return pathSegments.length === AUTOMATIC_OWNER_SESSION_BACKEND_PATH.length
    && pathSegments.every(
      (segment, index) => segment === AUTOMATIC_OWNER_SESSION_BACKEND_PATH[index],
    );
}

function isAnonymousRegistrationNamespace(pathSegments: readonly string[]): boolean {
  return pathSegments.length >= ANONYMOUS_REGISTRATION_BACKEND_PATH.length
    && ANONYMOUS_REGISTRATION_BACKEND_PATH.every(
      (segment, index) => pathSegments[index] === segment,
    );
}

function isExactAnonymousRegistrationPath(pathSegments: readonly string[]): boolean {
  return pathSegments.length === ANONYMOUS_REGISTRATION_BACKEND_PATH.length
    && isAnonymousRegistrationNamespace(pathSegments);
}

function isExactSetupOwnerPath(pathSegments: readonly string[]): boolean {
  return pathSegments.length === SETUP_OWNER_BACKEND_PATH.length
    && SETUP_OWNER_BACKEND_PATH.every(
      (segment, index) => pathSegments[index] === segment,
    );
}

export function isJsonContentType(value: string | null): boolean {
  return value?.split(";", 1)[0].trim().toLowerCase() === "application/json";
}

export function isCanonicalSameOriginWrite(
  request: Request,
  method: string,
  browserOrigin: string,
): boolean {
  if (!UNSAFE_METHODS.has(method)) return true;
  try {
    const fetchSite = request.headers.get("sec-fetch-site");
    return matchesBrowserAuthority(request.headers.get("host") ?? "", browserOrigin)
      && request.headers.get("origin") === browserOrigin
      && (fetchSite === null || fetchSite === "same-origin");
  } catch {
    return false;
  }
}

function isCanonicalAnonymousRegistrationRequest(
  request: Request,
  method: string,
  pathSegments: readonly string[],
  browserOrigin: string,
): boolean {
  if (method !== "POST" || !isExactAnonymousRegistrationPath(pathSegments)) return false;
  try {
    const requestUrl = new URL(request.url);
    return isCanonicalSameOriginWrite(request, method, browserOrigin)
      && requestUrl.search === ""
      && requestUrl.hash === ""
      && isJsonContentType(request.headers.get("content-type"));
  } catch {
    return false;
  }
}

function isCanonicalSetupOwnerRequest(
  request: Request,
  method: string,
  pathSegments: readonly string[],
  browserOrigin: string,
): boolean {
  if (method !== "POST" || !isExactSetupOwnerPath(pathSegments)) return false;
  try {
    const requestUrl = new URL(request.url);
    return isCanonicalSameOriginWrite(request, method, browserOrigin)
      && requestUrl.search === ""
      && requestUrl.hash === ""
      && isJsonContentType(request.headers.get("content-type"));
  } catch {
    return false;
  }
}

function hasBrowserSessionCookie(headers: Headers): boolean {
  const candidates = (headers.get("cookie") ?? "")
    .split(";")
    .map((item) => item.trim().split("=", 2))
    .filter(([name, value]) => (
      (name === LOCAL_SESSION_COOKIE_NAME || name === WEB_SESSION_COOKIE_NAME)
      && value !== undefined
    ))
    .map(([, value]) => value);
  return candidates.length === 1
    && candidates[0].length >= 32
    && candidates[0].length <= 512;
}

export async function proxyApiRequest(
  request: Request,
  pathSegments: readonly string[],
  options: RuntimeBffOptions = {},
): Promise<Response> {
  // Automatic owner sessions have a narrower browser boundary than the general API bridge. They
  // may only be requested through the dedicated canonical-origin Route Handler below, never by
  // spelling the private backend path under the browser-visible /api/v1 catch-all.
  if (isAutomaticOwnerSessionBackendPath(pathSegments)) {
    return Response.json(
      { detail: "The requested API route is not available." },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }

  const environment = options.environment ?? process.env;
  const configuration = runtimeConfiguration(environment);
  if (configuration === null) {
    return Response.json(
      { detail: "The local API bridge is not configured." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }

  const selectedOrigin = browserOriginForRequest(request, configuration.browserOrigins, configuration.anyHostHttpsPorts);
  // Legacy server-side status adapters historically omit Host/Origin. Keep that narrow read
  // compatibility; real browser authorities and all writes must match the explicit allowlist.
  const browserOrigin = selectedOrigin ?? (
    !configuration.anyHostHttpsPorts.length && !request.headers.has("host") && !request.headers.has("origin")
      ? configuration.browserOrigin : null
  );
  if (browserOrigin === null) {
    return Response.json({ detail: "A configured browser origin is required." }, {
      status: 403, headers: { "Cache-Control": "no-store" },
    });
  }

  const method = request.method.toUpperCase();
  const registrationRequest = isCanonicalAnonymousRegistrationRequest(
    request,
    method,
    pathSegments,
    browserOrigin,
  );
  const setupOwnerRequest = isCanonicalSetupOwnerRequest(
    request,
    method,
    pathSegments,
    browserOrigin,
  );
  // Registration is the sole anonymous addition to the general /api/v1 bridge. Reject every
  // method, child path, query-bearing request, non-canonical Host/Origin, and non-JSON variant
  // before a private fetch can receive the server-only service credential.
  if (isAnonymousRegistrationNamespace(pathSegments) && !registrationRequest) {
    return Response.json(
      { detail: "The requested registration route is not available." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (isExactSetupOwnerPath(pathSegments) && !setupOwnerRequest) {
    return Response.json(
      { detail: "The requested setup route is not available." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (!isCanonicalSameOriginWrite(request, method, browserOrigin)) {
    return Response.json(
      { detail: "A same-origin request is required." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (
    !registrationRequest
    && !SESSION_EXEMPT_REQUESTS.has(requestKey(method, pathSegments))
    && !hasBrowserSessionCookie(request.headers)
  ) {
    return Response.json(
      { detail: "A valid browser session is required." },
      { status: 401, headers: { "Cache-Control": "no-store" } },
    );
  }
  const headers = requestHeadersForBackend(
    request.headers,
    configuration.internalServiceToken,
  );
  const init: RequestInit & { duplex?: "half" } = {
    method,
    headers,
    redirect: "manual",
    cache: "no-store",
    signal: request.signal,
  };
  if (method !== "GET" && method !== "HEAD" && request.body !== null) {
    init.body = request.body;
    init.duplex = "half";
  }

  try {
    const targetUrl = backendUrl(configuration.apiInternalUrl, pathSegments, request.url);
    const upstream = await (options.fetchImplementation ?? fetch)(
      targetUrl,
      init,
    );
    const body = hasResponseBody(method, upstream.status) ? upstream.body : null;
    return new Response(body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeadersForBrowser(
        upstream.headers,
        configuration.apiInternalUrl,
        browserOrigin,
      ),
    });
  } catch {
    return Response.json(
      { detail: "The local API is unavailable." },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}


/**
 * Create the passwordless browser session used only by an explicitly enabled single-user runtime.
 *
 * This boundary deliberately accepts no browser identity, cookie, body, query, or target path. The
 * private service credential and backend URL come only from the Next server environment. FastAPI
 * independently verifies the persisted installation mode and unique-owner invariants before it
 * returns a Set-Cookie response.
 */
export async function proxyAutomaticOwnerSessionRequest(
  request: Request,
  options: RuntimeBffOptions = {},
): Promise<Response> {
  const environment = options.environment ?? process.env;
  const configuration = runtimeConfiguration(environment);
  if (configuration === null) {
    return Response.json(
      { detail: "The automatic session bridge is not configured." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }

  // The general BFF also supports authenticated HTTPS deployments. Passwordless owner sessions
  // are deliberately narrower: re-check the frontend's own runtime configuration here so a
  // split-deployment or environment drift cannot publish this privileged bridge on a public host,
  // even when the private API was started with otherwise-valid loopback settings.
  if (configuration.anyHostHttpsPorts.length > 0 || !isLoopbackBrowserOrigin(configuration.browserOrigin)) {
    return Response.json(
      { detail: "Automatic owner sessions require a loopback browser origin." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  if (
    request.method.toUpperCase() !== "POST"
    || !hasCanonicalAutomaticSessionTarget(request, configuration.browserOrigin)
    || request.headers.get("origin") !== configuration.browserOrigin
    || (fetchSite !== null && fetchSite !== "same-origin")
  ) {
    return Response.json(
      { detail: "A canonical same-origin request is required." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }

  const headers = new Headers({
    Accept: "application/json",
    [INTERNAL_SERVICE_TOKEN_HEADER]: configuration.internalServiceToken,
  });
  const targetUrl = backendUrl(
    configuration.apiInternalUrl,
    AUTOMATIC_OWNER_SESSION_BACKEND_PATH,
    new URL(
      AUTOMATIC_OWNER_SESSION_PUBLIC_PATH,
      configuration.browserOrigin,
    ).toString(),
  );

  try {
    const upstream = await (options.fetchImplementation ?? fetch)(targetUrl, {
      method: "POST",
      headers,
      redirect: "manual",
      cache: "no-store",
      signal: request.signal,
    });
    return new Response(hasResponseBody("POST", upstream.status) ? upstream.body : null, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeadersForBrowser(
        upstream.headers,
        configuration.apiInternalUrl,
        configuration.browserOrigin,
      ),
    });
  } catch {
    return Response.json(
      { detail: "The automatic owner session service is unavailable." },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
