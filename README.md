# QGIS-MCP - QGIS Model Context Protocol Integration

QGIS-MCP connects [QGIS](https://qgis.org/) to [Claude](https://claude.ai/)
through the Model Context Protocol (MCP), allowing Claude to directly interact
with and control QGIS. This integration enables prompt-assisted project
creation, layer loading, code execution and more.

This is a fork of [jjsantos01/qgis_mcp](https://github.com/jjsantos01/qgis_mcp)
(which was in turn strongly based on the
[BlenderMCP](https://github.com/ahujasid/blender-mcp/tree/main) project by
[Siddharth Ahuja](https://x.com/sidahuj)). It has diverged from upstream: the
MCP server is a Node.js package distributed via npm, `execute_code` has notebook
semantics and captured output, feature geometry is lightweight, multiple clients
can connect simultaneously, and the tool surface is trimmed to four tools
(project and layer management happen through `execute_code`).

## Features

- **Two-way communication**: Connect Claude to QGIS through a socket-based
  server.
- **Project manipulation**: Create, load and save projects in QGIS.
- **Layer manipulation**: Add and remove vector or raster layers, list layers
  and inspect their features.
- **Execute processing**: Run algorithms from the [Processing
  Toolbox](https://docs.qgis.org/latest/en/docs/user_manual/processing/toolbox.html).
- **Map rendering**: Render the current map view to an image.
- **Code execution**: Run arbitrary PyQGIS code in QGIS.

## Components

The system consists of two main components:

1. **[QGIS plugin](/qgis_mcp_plugin/)**: A QGIS plugin that runs a socket server
   inside QGIS to receive and execute commands.
2. **[MCP server](/server/index.js)**: A Node.js server (published to npm as
   [`qgis-mcp`](https://www.npmjs.com/package/qgis-mcp)) that implements the
   Model Context Protocol and connects to the QGIS plugin.

Both pieces must be running: the QGIS plugin listens on `localhost:9876`, and
the MCP server (started automatically by Claude) connects to it. The connection
uses bounded, length-prefixed messages and verifies that both components have
matching protocol and release versions before executing commands.

## Installation

### Prerequisites

- QGIS 3.x
- Claude Code or Claude Desktop

You do not need Python, Node.js, Git, or a terminal for the standard QGIS and
Claude Desktop installation.

### 1. Install the QGIS plugin

1. Download `qgis-mcp-plugin-<version>.zip` from the [latest
   release](https://github.com/wiseman/qgis_mcp/releases/latest). Do not unzip
   it.
2. In QGIS, open `Plugins` -> `Manage and Install Plugins` -> `Install from
   ZIP`, select the downloaded file, and click **Install Plugin**.
3. Open `Plugins` -> `QGIS MCP` -> `QGIS MCP`, click **Start Server**, and tick
   **Start server automatically when QGIS opens**.

The server listens only on your computer. It allows the connected AI assistant
to execute PyQGIS code, so only connect MCP clients you trust.

This project is distinct from the similarly named plugin in the official QGIS
catalog. Its internal plugin ID is `qgis_mcp_wiseman`, so installing this ZIP
will not overwrite the catalog plugin. Disable other QGIS MCP plugins before
starting this one because only one server can use the default port.

<details>
<summary>Install from source (developers)</summary>

This method requires Git and Node.js 18 or newer.

```bash
git clone https://github.com/wiseman/qgis_mcp.git
cd qgis_mcp
node scripts/install_qgis_plugin.js
```

This copies the plugin into your default QGIS profile. Use `--profile NAME` for
a non-default profile, or `--symlink` while developing. Restart QGIS after it
finishes.
</details>

### 2. Connect Claude

#### Claude Code (plugin — recommended)

This repo is a Claude Code plugin marketplace. In Claude Code:

```
/plugin marketplace add wiseman/qgis_mcp
/plugin install qgis-mcp@qgis-mcp
```

That's it — the plugin runs the MCP server from npm (`npx -y qgis-mcp`); it
downloads automatically on first use.

#### Claude Code (manual MCP config)

```bash
claude mcp add qgis -- npx -y qgis-mcp
```

#### Claude Desktop (extension bundle)

Download `qgis-mcp-<version>.mcpb` from the [latest
release](https://github.com/wiseman/qgis_mcp/releases/latest). In Claude
Desktop, go to `Settings` -> `Extensions` -> `Advanced settings` -> `Install
Extension…` and select the downloaded file (or just double-click it). The bundle
is fully self-contained. The QGIS host and port are configurable in the
extension's settings.

#### Claude Desktop (manual MCP config)

Go to `Claude` > `Settings` > `Developer` > `Edit Config` > `claude_desktop_config.json` and add:

```json
{
    "mcpServers": {
        "qgis": {
            "command": "npx",
            "args": ["-y", "qgis-mcp"]
        }
    }
}
```

> If you can't find the Developer tab or `claude_desktop_config.json`, see the
> [MCP quickstart
> documentation](https://modelcontextprotocol.io/quickstart/user#2-add-the-filesystem-mcp-server).

#### Non-default host or port

If you change the port in the QGIS MCP panel (default 9876), tell the MCP server
via the `QGIS_MCP_HOST` / `QGIS_MCP_PORT` environment variables. The `.mcpb`
extension exposes these as settings in Claude Desktop; for the other install
methods, set them in the environment Claude runs in, or add an `"env"` block to
the manual MCP config.

### Publishing (maintainers)

The Claude Code plugin and manual configs resolve the server from npm. To
release, set the same version in `package.json`, `.claude-plugin/plugin.json`,
`mcpb/manifest.json`, and `qgis_mcp_plugin/metadata.txt`, then run `npm run
check`. Push a matching tag such as `v0.1.0`. The release workflow publishes
the npm package and attaches both installable bundles to a GitHub release. It
requires an `NPM_TOKEN` repository secret with publish access.

For local builds, use `npm run build:qgis-plugin` and `npm run build:mcpb`.

## Usage

### Starting the connection

1. In QGIS, go to `Plugins` -> `QGIS MCP` -> `QGIS MCP`
    ![plugins menu](/assets/imgs/qgis-plugins-menu.png)
2. Click "Start Server"
    ![start server](/assets/imgs/qgis-mcp-start-server.png)

Tick "Start server automatically when QGIS opens" and you never have to do this
again — the server starts on QGIS launch, on the last port you used.

### Using with Claude

Once the QGIS server is running and the MCP server is configured, Claude will
have access to the QGIS tools.

![Claude tools](assets/imgs/claude-available-tools.png)

#### Tools

The server exposes a deliberately small set of four tools:

- `qgis_execute_code` - Execute arbitrary PyQGIS code with notebook semantics:
  the last bare expression becomes the result, `print()` output is captured, and
  exceptions are returned in-band with tracebacks. This is the workhorse —
  loading/saving projects, adding/removing layers, and running Processing
  algorithms all happen here, and the tool description includes recipes for the
  common operations
- `qgis_get_layers` - List all layers in the current project, including their
  fields
- `qgis_get_layer_features` - Retrieve features from a vector layer. Returns
  full geometry for points but only centroid + bounding box for lines and
  polygons, to keep payloads small
- `qgis_render_map` - Render the current map view and return the PNG directly
  to the MCP client; optionally save it to a path on the QGIS host

### Example commands

This is the example prompt used for the original [demo](https://x.com/jjsantoso/status/1900293848271667395):

```plain
You have access to the tools to work with QGIS. You will do the following:
    1. Ping to check the connection. If it works, continue with the following steps.
    2. Create a new project and save it at: "C:/Users/USER/GitHub/qgis_mcp/data/cdmx.qgz"
    3. Load the vector layer: "C:/Users/USER/GitHub/qgis_mcp/data/cdmx/mgpc_2019.shp" and name it "Colonias".
    4. Load the raster layer: "C:/Users/USER/GitHub/qgis_mcp/data/09014.tif" and name it "BJ"
    5. Zoom to the "BJ" layer.
    6. Execute the centroid algorithm on the "Colonias" layer. Skip the geometry check. Save the output to "colonias_centroids.geojson".
    7. Execute code to create a choropleth map using the "POB2010" field in the "Colonias" layer. Use the quantile classification method with 5 classes and the Spectral color ramp.
    8. Render the map to "C:/Users/USER/GitHub/qgis_mcp/data/cdmx.png"
    9. Save the project.
```
