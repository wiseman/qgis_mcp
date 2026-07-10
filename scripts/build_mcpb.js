#!/usr/bin/env node
// Build a .mcpb bundle (Claude Desktop extension) for the QGIS MCP server.
//
// Stages the manifest, server, and production node_modules into dist/mcpb/
// and packs them with the official mcpb CLI (run via npx). The bundle version
// comes from package.json, which is the canonical version for this project.
// Output: dist/qgis-mcp-<version>.mcpb
//
// Usage: node scripts/build_mcpb.js
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repo = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(readFileSync(path.join(repo, "package.json"), "utf8"));

const pluginJson = JSON.parse(readFileSync(path.join(repo, ".claude-plugin", "plugin.json"), "utf8"));
if (pluginJson.version !== pkg.version) {
  console.error(
    `warning: .claude-plugin/plugin.json version (${pluginJson.version}) != package.json version (${pkg.version})`
  );
}

const staging = path.join(repo, "dist", "mcpb");
rmSync(staging, { recursive: true, force: true });
mkdirSync(staging, { recursive: true });

const manifest = JSON.parse(readFileSync(path.join(repo, "mcpb", "manifest.json"), "utf8"));
manifest.version = pkg.version;
writeFileSync(path.join(staging, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");

cpSync(path.join(repo, "server"), path.join(staging, "server"), { recursive: true });

// Minimal package.json for the bundle: just enough to install prod deps.
writeFileSync(
  path.join(staging, "package.json"),
  JSON.stringify(
    { name: pkg.name, version: pkg.version, type: pkg.type, dependencies: pkg.dependencies },
    null,
    2
  ) + "\n"
);

// npm (not pnpm) on purpose: pnpm's node_modules is a symlink farm into its
// global store, which does not survive zipping into the bundle.
execFileSync("npm", ["install", "--omit=dev", "--no-audit", "--no-fund"], {
  cwd: staging,
  stdio: "inherit",
});

const out = path.join(repo, "dist", `qgis-mcp-${pkg.version}.mcpb`);
execFileSync("npx", ["-y", "@anthropic-ai/mcpb", "validate", path.join(staging, "manifest.json")], {
  stdio: "inherit",
});
execFileSync("npx", ["-y", "@anthropic-ai/mcpb", "pack", staging, out], { stdio: "inherit" });

console.log(`\nBuilt ${out}`);
