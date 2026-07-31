# WAI QGIS MCP

WAI QGIS MCP lets a trusted AI assistant work directly in QGIS. Use it to
inspect projects and layers, load data, run Processing algorithms, style maps,
render images, and automate PyQGIS tasks by describing what you want.

It connects an MCP-compatible assistant such as Codex or Claude to the QGIS
desktop application. The assistant can execute PyQGIS code, so only connect
assistants you trust.

## Install

You need to install the QGIS plugin and connect your AI assistant.

### 1. Install the QGIS plugin

1. In QGIS, choose **Plugins → Manage and Install Plugins → Settings** and
   enable **Show also Experimental Plugins**.
2. Open **All**, search for **WAI QGIS MCP**, and click **Install Plugin**.
3. Choose **Plugins → WAI QGIS MCP → WAI QGIS MCP**.
4. Click **Start Server** and enable **Start server automatically when QGIS
   opens**.

The plugin supports QGIS 3.28 and later.

### 2. Connect your AI assistant

#### Claude Desktop

Download `qgis-mcp-<version>.mcpb` from the [latest
release](https://github.com/wiseman/qgis_mcp/releases/latest), then double-click
it. You can also install it from **Settings → Extensions → Advanced settings →
Install Extension…**.

#### Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add wiseman/qgis_mcp
/plugin install qgis-mcp@qgis-mcp
```

#### Codex

Run this command in a terminal:

```bash
codex mcp add qgis -- npx -y qgis-mcp
```

Then restart Codex.

## Use it

Open QGIS and your AI assistant, then ask the assistant to inspect the current
QGIS project. For example:

> Inspect my current QGIS project and summarize its layers, coordinate reference
> systems, and visible extent.

You can then ask it to load data, style a layer, run a Processing algorithm,
save the project, or render the map. Be specific about layer names, fields,
output paths, and whether changes should be saved.

## If it does not connect

- Confirm that the WAI QGIS MCP panel says the server is running.
- Confirm that QGIS and the AI assistant are running on the same computer.
- Restart the AI assistant after installing or configuring the connection.
- The default port is `9876`. If you changed it in QGIS, set the same port in
  the Claude Desktop extension settings or with the `QGIS_MCP_PORT` environment
  variable.
- Enable **Log diagnostics to the QGIS Message Log** in the plugin panel for
  connection and error details.

WAI QGIS MCP is a fork of
[jjsantos01/qgis_mcp](https://github.com/jjsantos01/qgis_mcp).
