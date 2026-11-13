import { request as httpRequest } from "node:http";
import { Readable } from "node:stream";

// Node fetch replaces Host with its target authority. The private Web adapter must preserve the
// validated browser authority, so use HTTP directly on this already-allowlisted fixed target.
// Stream both directions; no credentials or paper content are buffered or logged here.
export function requestPrivateApi(target: URL, init: RequestInit): Promise<Response> {
  return new Promise((resolve, reject) => {
    if (target.protocol !== "http:") {
      reject(new Error("INVALID_PRIVATE_API_PROTOCOL"));
      return;
    }
    const method = init.method ?? "GET";
    const outgoing = httpRequest(target, {
      method,
      headers: Object.fromEntries(new Headers(init.headers).entries()),
    }, (incoming) => {
      const headers = new Headers();
      for (let index = 0; index < incoming.rawHeaders.length; index += 2) {
        headers.append(incoming.rawHeaders[index], incoming.rawHeaders[index + 1]);
      }
      const status = incoming.statusCode ?? 502;
      const hasBody = method !== "HEAD" && ![204, 205, 304].includes(status);
      if (!hasBody) incoming.resume();
      resolve(new Response(hasBody ? Readable.toWeb(incoming) as ReadableStream<Uint8Array> : null, {
        status, statusText: incoming.statusMessage, headers,
      }));
    });
    const abort = () => outgoing.destroy(new Error("PRIVATE_API_REQUEST_ABORTED"));
    outgoing.once("error", reject);
    outgoing.once("close", () => init.signal?.removeEventListener("abort", abort));
    if (init.signal?.aborted) {
      abort();
      return;
    }
    init.signal?.addEventListener("abort", abort, { once: true });
    if (init.body !== null && init.body !== undefined) {
      if (!(init.body instanceof ReadableStream)) {
        outgoing.destroy(new Error("INVALID_PRIVATE_API_BODY"));
        return;
      }
      const body = Readable.fromWeb(init.body as Parameters<typeof Readable.fromWeb>[0]);
      body.once("error", (error) => outgoing.destroy(error));
      outgoing.once("close", () => body.destroy());
      body.pipe(outgoing);
    } else {
      outgoing.end();
    }
  });
}
