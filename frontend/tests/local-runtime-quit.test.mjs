import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:net";
import { createInterface } from "node:readline";
import test from "node:test";

import { requestLocalRuntimeQuit } from "../app/api/local-runtime/quit/local-runtime-quit.ts";
import { sendSupervisorQuit } from "../app/api/local-runtime/quit/supervisor-control.ts";
import {
  INTERNAL_SERVICE_TOKEN_HEADER,
  LOCAL_SESSION_COOKIE_NAME,
} from "../app/api/v1/runtime-bff.ts";

const SERVICE_TOKEN = "test-only-next-service-token-000000000001";
const SESSION_TOKEN = "test-only-browser-session-token-000000000001";
const CONTROL_CHALLENGE = "test-only-supervisor-control-challenge-000000000001";

function proof(challenge, purpose) {
  return createHmac("sha256", challenge).update(purpose, "utf8").digest("hex");
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.notEqual(address, null);
  assert.equal(typeof address, "object");
  return address.port;
}

function environment(port = 43103) {
  return {
    API_INTERNAL_URL: "http://api:8000",
    INTERNAL_SERVICE_TOKEN: SERVICE_TOKEN,
    WEB_PUBLIC_ORIGIN: "http://localhost:3000",
    SUPERVISOR_CONTROL_HOST: "127.0.0.1",
    SUPERVISOR_CONTROL_PORT: String(port),
    SUPERVISOR_CONTROL_CHALLENGE: CONTROL_CHALLENGE,
  };
}

function quitRequest({ origin = "http://localhost:3000", cookie = SESSION_TOKEN } = {}) {
  const headers = new Headers();
  if (origin !== null) headers.set("Origin", origin);
  if (cookie !== null) {
    headers.set("Cookie", `${LOCAL_SESSION_COOKIE_NAME}=${cookie}`);
  }
  return new Request("http://localhost:3000/api/local-runtime/quit", {
    method: "POST",
    headers,
  });
}

test("server-only control client authenticates and sends exact QUIT command", async () => {
  let receivedCommand;
  const server = createServer(async (socket) => {
    const lines = createInterface({ input: socket, crlfDelay: Infinity });
    const iterator = lines[Symbol.asyncIterator]();
    const challengeLine = await iterator.next();
    assert.equal(challengeLine.done, false);
    const challengeRequest = JSON.parse(challengeLine.value);
    assert.equal(challengeRequest.operation, "CHALLENGE");
    socket.write(`${JSON.stringify({
      schema_version: 1,
      proof: proof(CONTROL_CHALLENGE, `endpoint:${challengeRequest.nonce}`),
    })}\n`);

    const commandLine = await iterator.next();
    assert.equal(commandLine.done, false);
    receivedCommand = JSON.parse(commandLine.value);
    assert.equal(receivedCommand.command, "QUIT");
    assert.equal(
      receivedCommand.authorization,
      proof(CONTROL_CHALLENGE, `command:${challengeRequest.nonce}:QUIT`),
    );
    assert.equal(commandLine.value.includes(CONTROL_CHALLENGE), false);
    socket.end(`${JSON.stringify({ schema_version: 1, status: "ACCEPTED" })}\n`);
  });
  const port = await listen(server);
  try {
    await sendSupervisorQuit({ environment: environment(port), timeoutMilliseconds: 2000 });
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
  assert.equal(receivedCommand.command, "QUIT");
});

test("quit route verifies the browser session through protected API before control", async () => {
  const calls = [];
  const request = quitRequest();
  const response = await requestLocalRuntimeQuit(request, {
    environment: environment(),
    fetchImplementation: async (url, init) => {
      calls.push("session");
      assert.equal(String(url), "http://api:8000/api/v1/local-session/status");
      assert.equal(init.method, "GET");
      assert.equal(
        init.headers.get("cookie"),
        `${LOCAL_SESSION_COOKIE_NAME}=${SESSION_TOKEN}`,
      );
      assert.equal(init.headers.get(INTERNAL_SERVICE_TOKEN_HEADER), SERVICE_TOKEN);
      return Response.json({ authenticated: true, setup_state: "READY" }, { status: 200 });
    },
    sendQuit: async ({ environment: controlEnvironment }) => {
      calls.push("control");
      assert.equal(controlEnvironment.SUPERVISOR_CONTROL_CHALLENGE, CONTROL_CHALLENGE);
    },
  });

  assert.equal(response.status, 202);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(calls, ["session", "control"]);
  const body = await response.text();
  assert.doesNotMatch(body, /test-only|api\.internal|43103|challenge/i);
});

test("quit route rejects CSRF and missing or rejected sessions before control", async () => {
  for (const request of [
    quitRequest({ origin: null }),
    quitRequest({ origin: "https://attacker.example" }),
  ]) {
    let touched = false;
    const response = await requestLocalRuntimeQuit(request, {
      environment: environment(),
      fetchImplementation: async () => {
        touched = true;
        return Response.json({ authenticated: true, setup_state: "READY" });
      },
      sendQuit: async () => {
        touched = true;
      },
    });
    assert.equal(response.status, 403);
    assert.equal(touched, false);
  }

  let fetchCalls = 0;
  let quitCalls = 0;
  const missing = await requestLocalRuntimeQuit(quitRequest({ cookie: null }), {
    environment: environment(),
    fetchImplementation: async () => {
      fetchCalls += 1;
      return new Response(null, { status: 200 });
    },
    sendQuit: async () => {
      quitCalls += 1;
    },
  });
  assert.equal(missing.status, 401);
  assert.equal(fetchCalls, 0);

  const rejected = await requestLocalRuntimeQuit(quitRequest(), {
    environment: environment(),
    fetchImplementation: async () => {
      fetchCalls += 1;
      return Response.json({ detail: "rejected" }, { status: 401 });
    },
    sendQuit: async () => {
      quitCalls += 1;
    },
  });
  assert.equal(rejected.status, 401);
  assert.equal(fetchCalls, 1);
  assert.equal(quitCalls, 0);
});

test("quit route fails closed on non-READY status, control error, and unsupported method", async () => {
  let quitCalls = 0;
  const notReady = await requestLocalRuntimeQuit(quitRequest(), {
    environment: environment(),
    fetchImplementation: async () => Response.json(
      { authenticated: true, setup_state: "CONFIGURING" },
      { status: 200 },
    ),
    sendQuit: async () => {
      quitCalls += 1;
    },
  });
  assert.equal(notReady.status, 503);
  assert.equal(quitCalls, 0);

  const unavailable = await requestLocalRuntimeQuit(quitRequest(), {
    environment: environment(),
    fetchImplementation: async () => Response.json(
      { authenticated: true, setup_state: "READY" },
      { status: 200 },
    ),
    sendQuit: async () => {
      throw new Error(`must not expose ${CONTROL_CHALLENGE}`);
    },
  });
  assert.equal(unavailable.status, 503);
  assert.equal((await unavailable.text()).includes(CONTROL_CHALLENGE), false);

  const unsupported = await requestLocalRuntimeQuit(
    new Request("http://localhost:3000/api/local-runtime/quit", { method: "GET" }),
    { environment: environment() },
  );
  assert.equal(unsupported.status, 405);
  assert.equal(unsupported.headers.get("allow"), "POST");
});

test("quit route and UI keep the control challenge server-only", async () => {
  const control = await readFile(
    new URL("../app/api/local-runtime/quit/supervisor-control.ts", import.meta.url),
    "utf8",
  );
  const route = await readFile(
    new URL("../app/api/local-runtime/quit/route.ts", import.meta.url),
    "utf8",
  );
  const page = await readFile(new URL("../app/settings/page.tsx", import.meta.url), "utf8");

  assert.match(route, /runtime = "nodejs"/);
  assert.match(route, /export (?:const|async function) POST/);
  assert.doesNotMatch(route, /export const (?:GET|HEAD|PUT|PATCH|DELETE)/);
  assert.doesNotMatch(`${control}\n${route}\n${page}`, /NEXT_PUBLIC_/);
  assert.doesNotMatch(page, /SUPERVISOR_CONTROL_|INTERNAL_SERVICE_TOKEN/);
  assert.match(page, /window\.confirm/);
  assert.match(page, /Quit AcadeMate Paper/);
  assert.match(page, /response\.status === 202/);
  assert.match(page, /credentials: "same-origin"/);
  assert.match(page, /isDesktopSupervisorAvailable\(runtimeCapabilities\)/);
});
