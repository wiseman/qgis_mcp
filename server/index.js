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
    name: "qgis_ping",
    command: "ping",
    schema: {},
    description: `Ping the QGIS MCP plugin to verify that the server can reach the running
QGIS instance.

Returns a JSON string such as:
    {
      "status": "success",
      "result": {"pong": true}
    }
No side-effects.`,
  },
  {
    name: "qgis_get_info",
    command: "get_qgis_info",
    schema: {},
    description: `Retrieve basic information about the active QGIS application.

Returns a JSON string with keys:
  • qgis_version   – full version string (e.g. "3.36.0-Lima")
  • profile_folder – absolute path to the user profile directory
  • plugins_count  – number of loaded plugins`,
  },
  {
    name: "qgis_load_project",
    command: "load_project",
    schema: {
      path: z.string().describe("Absolute path to the project file that QGIS can open."),
    },
    description: `Load an existing QGIS project (.qgs or .qgz) from disk.

Returns a JSON string {"status":"success","result":{"loaded": <path>,
"layer_count": <int>}}

Side effects: replaces the currently opened project in QGIS and refreshes
the map view.`,
  },
  {
    name: "qgis_create_new_project",
    command: "create_new_project",
    schema: {
      path: z
        .string()
        .describe("Destination path ending in .qgz or .qgs. Parent directories must already exist."),
    },
    description: `Create a brand-new, empty QGIS project and immediately save it to the given
path.

Returns a JSON confirmation message with layer_count (normally 0).

Side effects: clears any project that is currently open in QGIS.`,
  },
  {
    name: "qgis_get_project_info",
    command: "get_project_info",
    schema: {},
    description: `Return a concise summary of the currently open project.

Returns a JSON string containing:
  • filename
  • title
  • layer_count
  • crs (auth id)
  • layers – up to ten layer descriptors {id,name,type,visible}`,
  },
  {
    name: "qgis_add_vector_layer",
    command: "add_vector_layer",
    schema: {
      path: z.string().describe("File path or data-source URI recognised by QGIS."),
      provider: z.string().default("ogr").describe('QGIS data provider key, default "ogr".'),
      name: z
        .string()
        .optional()
        .describe("Custom display name for the layer (defaults to the file base-name)."),
    },
    description: `Add a vector dataset (Shapefile, GeoJSON, GeoPackage, …) to the project.

Returns a JSON description of the new layer {id,name,type,feature_count}.

Side effects: inserts the layer into the current project and triggers a map
refresh.`,
  },
  {
    name: "qgis_add_raster_layer",
    command: "add_raster_layer",
    schema: {
      path: z.string().describe("Absolute file path or URI to the raster."),
      provider: z.string().default("gdal").describe('Data provider key, default "gdal".'),
      name: z.string().optional().describe("Layer name to display in the layer panel."),
    },
    description: `Add a raster dataset (e.g. GeoTIFF, JPEG2000) to the project.

Returns a JSON string with {id,name,type,width,height}.

Side effects: adds the raster layer to the project.`,
  },
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
    name: "qgis_remove_layer",
    command: "remove_layer",
    schema: {
      layer_id: z.string().describe("The internal QGIS layer id obtained from other tools."),
    },
    description: `Remove a layer from the project.

Returns JSON {"status":"success","result":{"removed": layer_id}}

Side effects: deletes the layer from the project and the layer tree.`,
  },
  {
    name: "qgis_zoom_to_layer",
    command: "zoom_to_layer",
    schema: {
      layer_id: z.string().describe("QGIS layer id."),
    },
    description: `Zoom the map canvas to the full extent of a specific layer.

Returns JSON {"status":"success","result":{"zoomed_to": layer_id}}

Side effects: changes the visible map extent in the QGIS UI only.`,
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
    name: "qgis_execute_processing",
    command: "execute_processing",
    schema: {
      algorithm: z.string().describe('Provider-qualified algorithm id (e.g. "native:buffer").'),
      parameters: z
        .record(z.string(), z.any())
        .describe("Dictionary of parameter names and values exactly as expected by the algorithm."),
    },
    description: `Run a QGIS Processing algorithm.

Returns JSON {"status":"success","result":{"algorithm": id, "result": {...}}}
All complex objects (layers, paths) are serialised to strings.

Side effects: depends on the algorithm – may create layers, files or modify
data.`,
  },
  {
    name: "qgis_save_project",
    command: "save_project",
    schema: {
      path: z
        .string()
        .optional()
        .describe(
          "Destination .qgs/.qgz file. If omitted the project is saved over the existing file on disk."
        ),
    },
    description: `Save the current project.

Returns a JSON confirmation {"status":"success","result":{"saved": path}}

Side effects: writes a project file to disk.`,
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

Important: This is a powerful tool that can be used when none of the other
tools are sufficient or would be too inefficient.

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
