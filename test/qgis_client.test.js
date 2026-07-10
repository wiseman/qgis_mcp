import assert from "node:assert/strict";
import net from "node:net";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { encodeFrame, PROTOCOL_VERSION, QgisClient } from "../server/qgis_client.js";

async function fakeQgis({ pluginVersion = "0.1.0", onCommand } = {}) {
  const sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    let buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4) {
        const length = buffer.readUInt32BE(0);
        if (buffer.length < length + 4) return;
        const command = JSON.parse(buffer.subarray(4, length + 4).toString("utf8"));
        buffer = buffer.subarray(length + 4);
        if (command.type === "ping") {
          const frame = encodeFrame({
            status: "success",
            result: { pong: true, protocol_version: PROTOCOL_VERSION, plugin_version: pluginVersion },
          });
          // Deliberately split header and payload to exercise TCP framing.
          socket.write(frame.subarray(0, 2));
          setImmediate(() => socket.write(frame.subarray(2)));
        } else {
          onCommand?.(socket, command);
        }
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    port: server.address().port,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

test("performs a version handshake and reads a split response frame", async () => {
  const qgis = await fakeQgis({
    onCommand(socket, command) {
      const frame = encodeFrame({ status: "success", result: { echoed: command.params.value } });
      socket.write(frame.subarray(0, 4));
      setImmediate(() => socket.write(frame.subarray(4, 9)));
      setImmediate(() => socket.write(frame.subarray(9)));
    },
  });
  const client = new QgisClient({ host: "127.0.0.1", port: qgis.port, version: "0.1.0" });
  try {
    const response = await client.send("echo", { value: 42 });
    assert.deepEqual(response, { status: "success", result: { echoed: 42 } });
  } finally {
    client.close();
    await qgis.close();
  }
});

test("rejects a mismatched plugin version with an actionable message", async () => {
  const qgis = await fakeQgis({ pluginVersion: "9.9.9" });
  const client = new QgisClient({ host: "127.0.0.1", port: qgis.port, version: "0.1.0" });
  try {
    await assert.rejects(
      client.send("echo"),
      /version mismatch: server is 0\.1\.0, plugin is 9\.9\.9.*Update the QGIS plugin and MCP server together/s
    );
  } finally {
    client.close();
    await qgis.close();
  }
});

test("times out a stalled command and closes its socket", async () => {
  let commandCount = 0;
  const qgis = await fakeQgis({
    onCommand(socket) {
      commandCount += 1;
      if (commandCount > 1) {
        socket.write(encodeFrame({ status: "success", result: { recovered: true } }));
      }
    },
  });
  const client = new QgisClient({ host: "127.0.0.1", port: qgis.port, version: "0.1.0" });
  try {
    await assert.rejects(client.send("stall", {}, 25), /timed out/);
    assert.equal(client.socket, null);
    const response = await client.send("retry", {}, 100);
    assert.deepEqual(response.result, { recovered: true });
  } finally {
    client.close();
    await qgis.close();
  }
});

test("exposes render output as MCP image content and structured metadata", async () => {
  const onePixelPng = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
  const qgis = await fakeQgis({
    onCommand(socket, command) {
      assert.equal(command.type, "render_map");
      socket.write(encodeFrame({
        status: "success",
        result: {
          rendered: true,
          path: null,
          width: 800,
          height: 600,
          layers_drawn: ["roads"],
          mime_type: "image/png",
          image_base64: onePixelPng,
        },
      }));
    },
  });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["server/index.js"],
    cwd: process.cwd(),
    env: {
      ...process.env,
      QGIS_MCP_HOST: "127.0.0.1",
      QGIS_MCP_PORT: String(qgis.port),
    },
    stderr: "pipe",
  });
  const client = new Client({ name: "qgis-mcp-test", version: "1.0.0" });
  try {
    await client.connect(transport);
    const tools = await client.listTools();
    const renderTool = tools.tools.find((tool) => tool.name === "qgis_render_map");
    assert.equal(renderTool.annotations.idempotentHint, true);

    const result = await client.callTool({ name: "qgis_render_map", arguments: {} });
    assert.equal(result.content[0].type, "image");
    assert.equal(result.content[0].mimeType, "image/png");
    assert.equal(result.content[0].data, onePixelPng);
    assert.deepEqual(result.structuredContent.result.layers_drawn, ["roads"]);
    assert.equal("image_base64" in result.structuredContent.result, false);
  } finally {
    await client.close();
    await qgis.close();
  }
});
