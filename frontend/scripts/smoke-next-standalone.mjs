import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { dirname, resolve } from "node:path";

const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const TIMEOUT_MS = 30_000;

function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

async function freeLoopbackPort() {
  const listener = createServer();
  await new Promise((resolveListen, rejectListen) => {
    listener.once("error", rejectListen);
    listener.listen(0, "127.0.0.1", resolveListen);
  });
  const address = listener.address();
  if (address === null || typeof address === "string") {
    listener.close();
    throw new Error("Could not allocate a loopback port for the standalone smoke.");
  }
  const port = address.port;
  await new Promise((resolveClose, rejectClose) => {
    listener.close((error) => (error ? rejectClose(error) : resolveClose()));
  });
  return port;
}

async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  child.kill("SIGTERM");
  const exited = new Promise((resolveExit) => child.once("exit", resolveExit));
  if (await Promise.race([exited.then(() => true), delay(10_000).then(() => false)])) {
    return;
  }
  child.kill("SIGKILL");
  await exited;
}

async function main() {
  const rawRoot = process.argv[2];
  if (!rawRoot) {
    throw new Error("Usage: node smoke-next-standalone.mjs STANDALONE_ROOT");
  }
  const root = resolve(rawRoot);
  const port = await freeLoopbackPort();
  const child = spawn(process.execPath, [resolve(root, "server.js")], {
    cwd: root,
    env: {
      API_INTERNAL_URL: "http://127.0.0.1:9",
      HOSTNAME: "127.0.0.1",
      INTERNAL_SERVICE_TOKEN: "compose-smoke-service-token-0000000000001",
      NEXT_TELEMETRY_DISABLED: "1",
      NODE_ENV: "production",
      PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
      PORT: String(port),
      WEB_PUBLIC_ORIGIN: `http://127.0.0.1:${port}`,
    },
    stdio: "ignore",
  });
  try {
    const deadline = Date.now() + TIMEOUT_MS;
    let lastError;
    while (Date.now() < deadline) {
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(
          `Next standalone exited before readiness (status ${child.exitCode ?? child.signalCode}).`,
        );
      }
      try {
        const response = await fetch(`http://127.0.0.1:${port}/setup`, {
          headers: { Connection: "close" },
          signal: AbortSignal.timeout(1_000),
        });
        const body = new Uint8Array(await response.arrayBuffer());
        const mediaType = response.headers.get("content-type")?.toLowerCase() ?? "";
        const text = new TextDecoder().decode(body).toLowerCase();
        if (
          response.status === 200 &&
          body.byteLength <= MAX_RESPONSE_BYTES &&
          mediaType.startsWith("text/html") &&
          text.includes("<html")
        ) {
          console.log("next-standalone-smoke=passed");
          return;
        }
        lastError = new Error("Next standalone returned an invalid setup-page response.");
      } catch (error) {
        lastError = error;
      }
      await delay(100);
    }
    throw new Error("Next standalone did not become ready.", { cause: lastError });
  } finally {
    await stop(child);
  }
}

await main();
