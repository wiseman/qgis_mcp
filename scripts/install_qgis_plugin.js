#!/usr/bin/env node
// Install the QGIS MCP plugin into a QGIS user profile.
//
// Copies (or symlinks, with --symlink) the qgis_mcp_plugin folder into the
// QGIS profile's python/plugins directory. After running this, restart QGIS
// and enable "QGIS MCP" in Plugins -> Manage and Install Plugins.
//
// Usage: node scripts/install_qgis_plugin.js [--profile NAME] [--symlink]
import { cpSync, existsSync, lstatSync, mkdirSync, rmSync, symlinkSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import os from "node:os";
import path from "node:path";

const PLUGIN_NAME = "qgis_mcp_plugin";

function defaultProfilesRoot() {
  const home = os.homedir();
  switch (process.platform) {
    case "darwin":
      return path.join(home, "Library", "Application Support", "QGIS", "QGIS3", "profiles");
    case "win32":
      return path.join(home, "AppData", "Roaming", "QGIS", "QGIS3", "profiles");
    default:
      return path.join(home, ".local", "share", "QGIS", "QGIS3", "profiles");
  }
}

const { values: args } = parseArgs({
  options: {
    profile: { type: "string", default: "default" },
    symlink: { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  },
});

if (args.help) {
  console.log("Usage: node scripts/install_qgis_plugin.js [--profile NAME] [--symlink]");
  console.log("  --profile NAME  QGIS profile name (default: default)");
  console.log("  --symlink       Symlink instead of copy, so plugin edits in this repo");
  console.log("                  take effect on QGIS restart");
  process.exit(0);
}

const src = fileURLToPath(new URL(`../${PLUGIN_NAME}`, import.meta.url));
if (!existsSync(src)) {
  console.error(`error: ${src} not found`);
  process.exit(1);
}

const profileDir = path.join(defaultProfilesRoot(), args.profile);
if (!existsSync(profileDir)) {
  console.error(`error: QGIS profile not found at ${profileDir}`);
  console.error(
    "Find your profile folder in QGIS: Settings -> User Profiles -> Open Active Profile Folder"
  );
  process.exit(1);
}

const pluginsDir = path.join(profileDir, "python", "plugins");
mkdirSync(pluginsDir, { recursive: true });
const dest = path.join(pluginsDir, PLUGIN_NAME);

let existing = null;
try {
  existing = lstatSync(dest);
} catch {}
if (existing) {
  if (existing.isSymbolicLink() || existing.isFile()) {
    unlinkSync(dest);
  } else {
    rmSync(dest, { recursive: true });
  }
  console.log(`Removed existing ${dest}`);
}

if (args.symlink) {
  // "junction" so Windows doesn't require admin rights; ignored elsewhere.
  symlinkSync(path.resolve(src), dest, process.platform === "win32" ? "junction" : "dir");
  console.log(`Symlinked ${dest} -> ${path.resolve(src)}`);
} else {
  cpSync(src, dest, {
    recursive: true,
    filter: (s) => {
      const base = path.basename(s);
      return base !== "__pycache__" && base !== ".DS_Store";
    },
  });
  console.log(`Copied plugin to ${dest}`);
}

console.log("\nNext steps:");
console.log("  1. Restart QGIS");
console.log('  2. Plugins -> Manage and Install Plugins -> enable "QGIS MCP"');
console.log("  3. Plugins -> QGIS MCP -> QGIS MCP -> Start Server");
