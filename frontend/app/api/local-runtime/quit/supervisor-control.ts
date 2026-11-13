import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createConnection, isIPv4, type Socket } from "node:net";

const MAX_CONTROL_MESSAGE_BYTES = 4096;

type RuntimeEnvironment = Record<string, string | undefined>;

type SupervisorControlConfiguration = {
  host: string;
  port: number;
  challenge: string;
};

export type SupervisorQuitOptions = {
  environment?: RuntimeEnvironment;
  timeoutMilliseconds?: number;
};

export class SupervisorControlUnavailable extends Error {
  constructor() {
    super("The local Supervisor control endpoint is unavailable.");
    this.name = "SupervisorControlUnavailable";
  }
}

function controlConfiguration(
  environment: RuntimeEnvironment,
): SupervisorControlConfiguration | null {
  const host = environment.SUPERVISOR_CONTROL_HOST?.trim();
  const rawPort = environment.SUPERVISOR_CONTROL_PORT?.trim();
  const challenge = environment.SUPERVISOR_CONTROL_CHALLENGE;
  if (
    !host
    || !rawPort
    || !challenge
    || !(host === "::1" || (isIPv4(host) && host.startsWith("127.")))
    || !/^\d{1,5}$/.test(rawPort)
    || Buffer.byteLength(challenge, "utf8") < 32
    || Buffer.byteLength(challenge, "utf8") > 512
  ) {
    return null;
  }
  const port = Number(rawPort);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) return null;
  return { host, port, challenge };
}

function proof(challenge: string, purpose: string): string {
  return createHmac("sha256", challenge).update(purpose, "utf8").digest("hex");
}

function safeProofMatches(supplied: unknown, expected: string): boolean {
  if (typeof supplied !== "string" || !/^[0-9a-f]{64}$/.test(supplied)) return false;
  return timingSafeEqual(Buffer.from(supplied, "ascii"), Buffer.from(expected, "ascii"));
}

function writeMessage(socket: Socket, payload: Record<string, unknown>): Promise<void> {
  const encoded = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
  if (encoded.byteLength > MAX_CONTROL_MESSAGE_BYTES) {
    return Promise.reject(new SupervisorControlUnavailable());
  }
  return new Promise((resolve, reject) => {
    socket.write(encoded, (error) => {
      if (error) reject(error);
      else resolve();
    });
  });
}

function readMessage(socket: Socket): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let buffered = Buffer.alloc(0);

    const cleanup = () => {
      socket.off("data", onData);
      socket.off("error", onError);
      socket.off("timeout", onTimeout);
      socket.off("close", onClose);
    };
    const fail = (error: Error) => {
      cleanup();
      reject(error);
    };
    const onError = (error: Error) => fail(error);
    const onTimeout = () => fail(new SupervisorControlUnavailable());
    const onClose = () => fail(new SupervisorControlUnavailable());
    const onData = (chunk: Buffer) => {
      buffered = Buffer.concat([buffered, chunk]);
      const newline = buffered.indexOf(0x0a);
      if (
        (newline === -1 && buffered.byteLength > MAX_CONTROL_MESSAGE_BYTES)
        || newline >= MAX_CONTROL_MESSAGE_BYTES
      ) {
        fail(new SupervisorControlUnavailable());
        return;
      }
      if (newline === -1) return;
      const raw = buffered.subarray(0, newline);
      cleanup();
      try {
        const decoded = new TextDecoder("utf-8", { fatal: true }).decode(raw);
        const parsed: unknown = JSON.parse(decoded);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new TypeError("control response must be an object");
        }
        resolve(parsed as Record<string, unknown>);
      } catch {
        reject(new SupervisorControlUnavailable());
      }
    };

    socket.on("data", onData);
    socket.once("error", onError);
    socket.once("timeout", onTimeout);
    socket.once("close", onClose);
  });
}

function connect(configuration: SupervisorControlConfiguration, timeout: number): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host: configuration.host, port: configuration.port });
    const failed = () => {
      socket.destroy();
      reject(new SupervisorControlUnavailable());
    };
    socket.setTimeout(timeout);
    socket.once("error", failed);
    socket.once("timeout", failed);
    socket.once("connect", () => {
      socket.off("error", failed);
      socket.off("timeout", failed);
      resolve(socket);
    });
  });
}

export async function sendSupervisorQuit(
  options: SupervisorQuitOptions = {},
): Promise<void> {
  const configuration = controlConfiguration(options.environment ?? process.env);
  const timeout = options.timeoutMilliseconds ?? 2000;
  if (configuration === null || !Number.isFinite(timeout) || timeout <= 0) {
    throw new SupervisorControlUnavailable();
  }

  let socket: Socket | null = null;
  try {
    socket = await connect(configuration, timeout);
    const nonce = randomBytes(32).toString("hex");
    await writeMessage(socket, {
      schema_version: 1,
      operation: "CHALLENGE",
      nonce,
    });
    const challengeResponse = await readMessage(socket);
    if (
      challengeResponse.schema_version !== 1
      || !safeProofMatches(
        challengeResponse.proof,
        proof(configuration.challenge, `endpoint:${nonce}`),
      )
    ) {
      throw new SupervisorControlUnavailable();
    }

    await writeMessage(socket, {
      schema_version: 1,
      operation: "COMMAND",
      command: "QUIT",
      authorization: proof(configuration.challenge, `command:${nonce}:QUIT`),
    });
    const accepted = await readMessage(socket);
    if (accepted.schema_version !== 1 || accepted.status !== "ACCEPTED") {
      throw new SupervisorControlUnavailable();
    }
  } catch {
    throw new SupervisorControlUnavailable();
  } finally {
    socket?.destroy();
  }
}
