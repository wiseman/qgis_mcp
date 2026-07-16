#!/usr/bin/env node
// QGIS MCP server: a thin stdio<->TCP proxy. Each MCP tool call is forwarded
// as {"type": <command>, "params": {...}} to the socket server that the QGIS
// MCP plugin runs inside QGIS, and the JSON reply is returned as text.
import { readFileSync } from "node:fs";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { QgisClient } from "./qgis_client.js";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

const HOST = process.env.QGIS_MCP_HOST || "localhost";
const PORT = parseInt(process.env.QGIS_MCP_PORT || "", 10) || 9876;

// stdout is the MCP transport; anything human-readable goes to stderr.
function dbg(msg) {
  if (process.env.QGIS_MCP_DEBUG) {
    process.stderr.write(`[qgis-mcp ${new Date().toISOString()}] ${msg}\n`);
  }
}

const qgis = new QgisClient({ host: HOST, port: PORT, version: pkg.version, debug: dbg });

function sendCommand(type, params = {}, timeoutMs) {
  dbg(`sending command: ${type}`);
  return qgis.send(type, params, timeoutMs);
}

function unwrapResponse(response) {
  if (!response || response.status !== "success") {
    throw new Error(response?.message || "QGIS returned no response");
  }
  return response.result;
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
    name: "qgis_get_project",
    title: "Inspect the QGIS project",
    command: "get_project",
    schema: {},
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    description: `Return bounded metadata about the current QGIS project and map canvas.

Use this as the first call when orienting to a QGIS session. The result includes
the project filename, title, dirty state, home path, project CRS, layer counts,
active layer, canvas CRS/extent/scale/rotation, and WAI QGIS MCP plugin and
protocol versions. Use qgis_get_layers for the full layer inventory.`,
  },
  {
    name: "qgis_get_layers",
    title: "List QGIS layers",
    command: "get_layers",
    schema: {},
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    description: `List every layer currently loaded in the project.

Returns an array of layer descriptors as structured output. Each
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
    title: "Inspect QGIS layer features",
    command: "get_layer_features",
    schema: {
      layer_id: z.string().describe("Target vector layer id."),
      limit: z.number().int().default(10).describe("Maximum number of features to return (default 10)."),
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    description: `Retrieve attribute and lightweight geometry data for a subset of features
from a vector layer.  Useful for quickly inspecting layer attributes without
transferring full geometries for large features.

Returns structured output with the keys:

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
    title: "Render the QGIS map",
    command: "render_map",
    schema: {
      path: z.string().optional().describe("Optional output image path on the QGIS host."),
      width: z.number().int().min(1).max(2048).default(800).describe("Image width in pixels, default 800."),
      height: z.number().int().min(1).max(2048).default(600).describe("Image height in pixels, default 600."),
    },
    timeoutMs: 60_000,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    description: `Render the current map view and return the PNG image inline.

If path is supplied, also saves the image on the QGIS host. Returns the image
plus structured metadata {rendered, path, width, height, layers_drawn}.

Side effects: does not alter project data; writes a file only when path is supplied.`,
  },
  {
    name: "qgis_execute_code",
    title: "Execute PyQGIS code",
    command: "execute_code",
    schema: {
      code: z.string().describe("Python source code string."),
    },
    timeoutMs: 60_000,
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true },
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

Qt classes are not implicitly imported. Import the exact Qt APIs each snippet
needs, for example:

    from qgis.PyQt.QtCore import QVariant, Qt
    from qgis.PyQt.QtGui import QColor, QFont

Execution context
-----------------
Every call uses a fresh Python namespace. Variables do not persist between
calls, so each snippet must import and reacquire everything it needs. QGIS
project state does persist, and layers can be reacquired through QgsProject.

If the last statement is a bare expression, that expression's value is returned
as "result". Do not use a top-level "return" — it is a syntax error. Anything
you print() is captured and returned as "stdout", so print() is a fine way to
inspect state.

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

Execution is not transactional. If a snippet changes the project and later
raises an exception, changes made before the exception remain in QGIS.

Returns this structured payload directly:

On success:
    {"success": true, "result": <value>, "stdout": …, "stderr": …}
When the value is not JSON-encodable (e.g. a QgsVectorLayer), "result" is
null and "result_repr" holds its repr() instead.

When your code raises, the MCP result is marked as an error and the structured
payload carries the failure and traceback — retry against it:
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
  {
    instructions:
      "WAI QGIS MCP controls the currently running QGIS session. Start with " +
      "qgis_get_project for project context, use dedicated read-only tools for " +
      "inspection, and use self-contained qgis_execute_code snippets for changes.",
  }
);

for (const tool of TOOLS) {
  server.registerTool(
    tool.name,
    {
      title: tool.title,
      description: tool.description,
      inputSchema: tool.schema,
      outputSchema: { result: z.unknown() },
      annotations: tool.annotations,
    },
    async (args) => {
      const result = unwrapResponse(await sendCommand(tool.command, prune(args), tool.timeoutMs));
      if (tool.command === "render_map") {
        const { image_base64: data, mime_type: mimeType, ...metadata } = result;
        return {
          content: [
            { type: "image", data, mimeType },
            { type: "text", text: JSON.stringify(metadata, null, 2) },
          ],
          structuredContent: { result: metadata },
        };
      }
      const isExecutionError = tool.command === "execute_code" && result?.success === false;
      return {
        ...(isExecutionError ? { isError: true } : {}),
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        structuredContent: { result },
      };
    }
  );
}

await server.connect(new StdioServerTransport());
dbg("qgis-mcp server running on stdio");

// Exit when the MCP client disconnects (stdin closes); nothing useful can
// happen after that.
process.stdin.on("close", () => {
  qgis.close();
  process.exit(0);
});
