import net from "node:net";

export const PROTOCOL_VERSION = 1;
export const MAX_MESSAGE_BYTES = 32 * 1024 * 1024;

const DEFAULT_TIMEOUT_MS = 30_000;
const HANDSHAKE_TIMEOUT_MS = 5_000;

class CompatibilityError extends Error {}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function encodeFrame(value) {
  const payload = Buffer.from(JSON.stringify(value), "utf8");
  if (payload.length > MAX_MESSAGE_BYTES) {
    throw new Error(`WAI QGIS MCP message is too large (${payload.length} bytes)`);
  }
  const header = Buffer.allocUnsafe(4);
  header.writeUInt32BE(payload.length);
  return Buffer.concat([header, payload]);
}

export class QgisClient {
  constructor({ host = "localhost", port = 9876, version, debug = () => {} } = {}) {
    this.host = host;
    this.port = port;
    this.version = version;
    this.debug = debug;
    this.socket = null;
    this.everConnected = false;
    this.queue = Promise.resolve();
  }

  send(type, params = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const run = this.queue.then(async () => {
      const socket = await this.#getConnection();
      try {
        return await this.#request(socket, type, params, timeoutMs);
      } catch (error) {
        socket.destroy();
        if (this.socket === socket) this.socket = null;
        throw error;
      }
    });
    this.queue = run.then(() => {}, () => {});
    return run;
  }

  close() {
    this.socket?.destroy();
    this.socket = null;
  }

  async #getConnection() {
    if (this.socket && !this.socket.destroyed) return this.socket;

    const retryDelays = this.everConnected ? [0, 300] : [0, 300, 700, 1_500];
    let lastError;
    for (const retryDelay of retryDelays) {
      if (retryDelay) await delay(retryDelay);
      try {
        return await this.#connectOnce();
      } catch (error) {
        if (error instanceof CompatibilityError) throw error;
        lastError = error;
        this.debug(`connection attempt failed: ${error.message}`);
      }
    }

    const detail = lastError?.code ? `${lastError.code}: ${lastError.message}` : lastError?.message;
    throw new Error(
      `Could not connect to QGIS at ${this.host}:${this.port}${detail ? ` (${detail})` : ""}. ` +
      "Open Plugins -> WAI QGIS MCP -> WAI QGIS MCP, then start the server."
    );
  }

  async #connectOnce() {
    this.debug(`connecting to QGIS at ${this.host}:${this.port}`);
    const socket = await new Promise((resolve, reject) => {
      const candidate = net.createConnection({ host: this.host, port: this.port });
      const timer = setTimeout(() => {
        const error = new Error(`Connection timed out after ${HANDSHAKE_TIMEOUT_MS / 1000}s`);
        error.code = "ETIMEDOUT";
        candidate.destroy();
        reject(error);
      }, HANDSHAKE_TIMEOUT_MS);
      const cleanup = () => {
        clearTimeout(timer);
        candidate.removeListener("connect", onConnect);
        candidate.removeListener("error", onError);
      };
      const onConnect = () => {
        cleanup();
        resolve(candidate);
      };
      const onError = (error) => {
        cleanup();
        reject(error);
      };
      candidate.once("connect", onConnect);
      candidate.once("error", onError);
    });

    socket.unref();
    try {
      const response = await this.#request(
        socket,
        "ping",
        { protocol_version: PROTOCOL_VERSION, server_version: this.version },
        HANDSHAKE_TIMEOUT_MS
      );
      const info = response?.result;
      if (response?.status !== "success" || !info?.pong) {
        throw new CompatibilityError("QGIS plugin did not accept the compatibility handshake");
      }
      if (info.protocol_version !== PROTOCOL_VERSION) {
        throw new CompatibilityError(
          `WAI QGIS MCP protocol mismatch: server uses ${PROTOCOL_VERSION}, plugin uses ${info.protocol_version ?? "unknown"}. ` +
          "Update the QGIS plugin and MCP server together."
        );
      }
      if (info.plugin_version !== this.version) {
        throw new CompatibilityError(
          `WAI QGIS MCP version mismatch: server is ${this.version}, plugin is ${info.plugin_version ?? "unknown"}. ` +
          "Update the QGIS plugin and MCP server together."
        );
      }
    } catch (error) {
      socket.destroy();
      throw error;
    }

    socket.on("close", () => {
      if (this.socket === socket) this.socket = null;
    });
    socket.on("error", (error) => this.debug(`socket error: ${error.message}`));
    this.socket = socket;
    this.everConnected = true;
    this.debug("TCP connection and compatibility handshake established");
    return socket;
  }

  #request(socket, type, params, timeoutMs) {
    return new Promise((resolve, reject) => {
      let buffer = Buffer.alloc(0);
      let expectedLength = null;
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error(`QGIS command '${type}' timed out after ${timeoutMs / 1000}s`));
      }, timeoutMs);
      const cleanup = () => {
        clearTimeout(timer);
        socket.removeListener("data", onData);
        socket.removeListener("close", onClose);
        socket.removeListener("error", onError);
      };
      const onData = (chunk) => {
        buffer = Buffer.concat([buffer, chunk]);
        if (expectedLength === null && buffer.length >= 4) {
          expectedLength = buffer.readUInt32BE(0);
          if (expectedLength > MAX_MESSAGE_BYTES) {
            cleanup();
            reject(new Error(`QGIS response is too large (${expectedLength} bytes)`));
            return;
          }
        }
        if (expectedLength === null || buffer.length < expectedLength + 4) return;
        if (buffer.length !== expectedLength + 4) {
          cleanup();
          reject(new Error("QGIS protocol error: received bytes beyond one response frame"));
          return;
        }
        try {
          const response = JSON.parse(buffer.subarray(4).toString("utf8"));
          cleanup();
          resolve(response);
        } catch (error) {
          cleanup();
          reject(new Error(`QGIS returned invalid JSON: ${error.message}`));
        }
      };
      const onClose = () => {
        cleanup();
        reject(new Error("Connection to QGIS closed unexpectedly"));
      };
      const onError = (error) => {
        cleanup();
        reject(error);
      };
      socket.on("data", onData);
      socket.once("close", onClose);
      socket.once("error", onError);
      try {
        socket.write(encodeFrame({ type, params }));
      } catch (error) {
        cleanup();
        reject(error);
      }
    });
  }
}
