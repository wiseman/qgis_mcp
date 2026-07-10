#!/usr/bin/env node
// QGIS MCP server: a thin stdio<->TCP proxy. Each MCP tool call is forwarded
// as {"type": <command>, "params": {...}} to the socket server that the QGIS
// MCP plugin runs inside QGIS, and the JSON reply is returned as text.
import net from "node:net";
import { readFileSync } from "node:fs";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

const HOST = process.env.QGIS_MCP_HOST || "localhost";
const PORT = parseInt(process.env.QGIS_MCP_PORT || "", 10) || 9876;

// stdout is the MCP transport; anything human-readable goes to stderr.
function dbg(msg) {
  if (process.env.QGIS_MCP_DEBUG) {
    process.stderr.write(`[qgis-mcp ${new Date().toISOString()}] ${msg}\n`);
  }
}

let socket = null;
// The wire protocol has no message ids, so commands must not interleave:
// each command waits for the previous one's response.
let queue = Promise.resolve();

function getConnection() {
  if (socket && !socket.destroyed) {
    return Promise.resolve(socket);
  }
  return new Promise((resolve, reject) => {
    dbg(`connecting to QGIS at ${HOST}:${PORT}`);
    const sock = net.createConnection({ host: HOST, port: PORT });
    // Don't let the QGIS connection keep the process alive once the MCP
    // client closes stdin.
    sock.unref();
    sock.once("connect", () => {
      dbg("TCP connection established");
      sock.on("close", () => {
        if (socket === sock) socket = null;
      });
      socket = sock;
      resolve(sock);
    });
    sock.once("error", (err) => {
      dbg(`connection error: ${err.message}`);
      reject(new Error("Could not connect to QGIS. Make sure the QGIS plugin is running."));
    });
  });
}

function sendCommand(type, params = {}) {
  const run = queue.then(() => doSendCommand(type, params));
  queue = run.then(
    () => {},
    () => {}
  );
  return run;
}

function doSendCommand(type, params) {
  return getConnection().then(
    (sock) =>
      new Promise((resolve, reject) => {
        const chunks = [];
        const cleanup = () => {
          sock.removeListener("data", onData);
          sock.removeListener("close", onClose);
          sock.removeListener("error", onError);
        };
        const onData = (chunk) => {
          chunks.push(chunk);
          // The plugin sends one JSON object with no framing; accumulate
          // until the buffer parses.
          let response;
          try {
            response = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          } catch {
            return;
          }
          cleanup();
          resolve(response);
        };
        const onClose = () => {
          cleanup();
          reject(new Error("Connection to QGIS closed unexpectedly. Make sure the QGIS plugin is running."));
        };
        const onError = (err) => {
          cleanup();
          reject(err);
        };
        sock.on("data", onData);
        sock.once("close", onClose);
        sock.once("error", onError);
        dbg(`sending command: ${type}`);
        sock.write(JSON.stringify({ type, params }));
      })
  );
}

// Drop undefined values so omitted optional args stay out of params: the QGIS
// plugin dispatches handler(**params) and relies on Python kwarg defaults.
function prune(args) {
  const params = {};
  for (const [key, value] of Object.entries(args ?? {})) {
    if (value !== undefined) params[key] = value;
  }
  return params;
}

const TOOLS = [
  {
    name: "qgis_get_layers",
    command: "get_layers",
    schema: {},
    description: `List every layer currently loaded in the project.

Returns a JSON string containing an array of layer descriptors. Each
descriptor includes:

• id – layer id
• name – display name
• type – "vector_<geom>" or "raster"
• crs – Coordinate Reference System auth id (e.g. "EPSG:3857")
• fields – attribute names (vector layers only)
• visible – layer tree visibility flag
• type-specific metadata such as feature_count/geometry_type for vectors or
  width/height for rasters.`,
  },
  {
    name: "qgis_get_layer_features",
    command: "get_layer_features",
    schema: {
      layer_id: z.string().describe("Target vector layer id."),
      limit: z.number().int().default(10).describe("Maximum number of features to return (default 10)."),
    },
    description: `Retrieve attribute and lightweight geometry data for a subset of features
from a vector layer.  Useful for quickly inspecting layer attributes without
transferring full geometries for large features.

Returns a JSON string with the keys:

• layer_id – id of the queried layer
• feature_count – total number of features in the layer
• fields – list of attribute names
• features – array of feature descriptors

For every feature the entry is:

    {
      "id": <int>,
      "attributes": {<field>: <value>, …},
      "geometry": {
         "type": "point",
         "wkt": "POINT (…)"
      }
    }

when the geometry is a point (including multipoint with one point).

For all other geometry types the geometry object is:

    {
      "type": "centroid_bbox",
      "centroid_wkt": "POINT (…)" | null,
      "bbox": {"xmin": <float>, "ymin": <float>,
               "xmax": <float>, "ymax": <float>}
    }

This approach keeps the payload small while still providing a meaningful
spatial summary for lines and polygons.`,
  },
  {
    name: "qgis_render_map",
    command: "render_map",
    schema: {
      path: z.string().describe("Output image path (format inferred from extension)."),
      width: z.number().int().default(800).describe("Image width in pixels, default 800."),
      height: z.number().int().default(600).describe("Image height in pixels, default 600."),
    },
    description: `Export the current map view to an image on disk.

Returns JSON {rendered: true, path, width, height}

Side effects: renders the map and writes an image file; does not alter
project data.`,
  },
  {
    name: "qgis_execute_code",
    command: "execute_code",
    schema: {
      code: z.string().describe("Python source code string."),
    },
    description: `Execute arbitrary Python code inside the QGIS Python interpreter.

This is the primary tool for working with QGIS: everything except layer
inspection and map rendering (which have dedicated tools) is done here.
Common operations:

    QgsProject.instance().read("/path/project.qgz")            # load project
    QgsProject.instance().write("/path/project.qgz")           # save project
    iface.addVectorLayer("/path/roads.shp", "roads", "ogr")    # add vector layer
    iface.addRasterLayer("/path/dem.tif", "dem")               # add raster layer
    QgsProject.instance().removeMapLayer(layer_id)             # remove layer
    iface.setActiveLayer(layer); iface.zoomToActiveLayer()     # zoom to layer
    processing.run("native:centroids", {"INPUT": …, "OUTPUT": …})  # Processing algorithm

Execution context
-----------------
The code runs like a Jupyter cell.  If its last statement is a bare
expression, that expression's value is returned as "result".  Do not use a
top-level "return" — it is a syntax error.  Anything you print() is captured
and returned as "stdout", so print() is a fine way to inspect state.

    layer = QgsProject.instance().mapLayersByName("roads")[0]
    layer.featureCount()          # ← this becomes "result"

The execution namespace already contains everything exported by qgis.core
and qgis.gui (QgsProject, QgsVectorLayer, QgsCoordinateReferenceSystem, …),
plus:
    • iface (the QgisInterface)
    • qgis (the package: qgis.core, qgis.utils, …)
    • processing (when the Processing plugin is loaded)
    • json, math, os

The code runs on the QGIS main thread and blocks the UI while it does, so
keep snippets bounded; do not start long loops or wait on input.

Returns JSON {"status":"success","result": <payload>}, where <payload> is:

On success:
    {"success": true, "result": <value>, "stdout": …, "stderr": …}
When the value is not JSON-encodable (e.g. a QgsVectorLayer), "result" is
null and "result_repr" holds its repr() instead.

When your code raises, the call still succeeds at the transport level and
the payload carries the failure — retry against it:
    {"success": false, "error": …, "traceback": …,
     "stdout": …, "stderr": …}
"stdout" holds whatever was printed before the exception.

Long strings are clipped and marked "... [truncated, N chars total]".

Side effects: whatever the supplied code performs – it can create, modify or
delete layers and data.`,
  },
];

const server = new McpServer(
  { name: "qgis-mcp", version: pkg.version },
  { instructions: "QGIS integration through the Model Context Protocol" }
);

for (const tool of TOOLS) {
  server.registerTool(
    tool.name,
    { description: tool.description, inputSchema: tool.schema },
    async (args) => {
      const result = await sendCommand(tool.command, prune(args));
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    }
  );
}

await server.connect(new StdioServerTransport());
dbg("qgis-mcp server running on stdio");

// Exit when the MCP client disconnects (stdin closes); nothing useful can
// happen after that.
process.stdin.on("close", () => process.exit(0));
