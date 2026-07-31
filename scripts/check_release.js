#!/usr/bin/env node
// Check the files that make up a release without requiring QGIS.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repo = fileURLToPath(new URL("..", import.meta.url));
const readJson = (name) => JSON.parse(readFileSync(path.join(repo, name), "utf8"));
const pkg = readJson("package.json");
const versions = new Map([
  ["package.json", pkg.version],
  [".claude-plugin/plugin.json", readJson(".claude-plugin/plugin.json").version],
  ["mcpb/manifest.json", readJson("mcpb/manifest.json").version],
]);

const metadataText = readFileSync(path.join(repo, "qgis_mcp_plugin", "metadata.txt"), "utf8");
const metadata = Object.fromEntries(
  metadataText
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith("[") && line.includes("="))
    .map((line) => {
      const separator = line.indexOf("=");
      return [line.slice(0, separator), line.slice(separator + 1)];
    })
);
versions.set("qgis_mcp_plugin/metadata.txt", metadata.version);

const errors = [];
for (const [file, version] of versions) {
  if (version !== pkg.version) {
    errors.push(`${file} version ${version ?? "(missing)"} != package.json version ${pkg.version}`);
  }
}

for (const field of [
  "name", "description", "about", "version", "qgisMinimumVersion",
  "author", "email", "repository",
]) {
  if (!metadata[field]) errors.push(`qgis_mcp_plugin/metadata.txt is missing ${field}`);
}

for (const file of ["LICENSE", "qgis_mcp_plugin/__init__.py", "qgis_mcp_plugin/qgis_mcp_plugin.py"]) {
  if (!existsSync(path.join(repo, file))) errors.push(`${file} is missing`);
}

for (const file of ["server/index.js", "server/qgis_client.js", "scripts/build_mcpb.js", "scripts/build_qgis_plugin.js", "scripts/install_qgis_plugin.js"]) {
  try {
    execFileSync(process.execPath, ["--check", path.join(repo, file)], { stdio: "pipe" });
  } catch (error) {
    errors.push(`${file} has invalid JavaScript: ${error.stderr?.toString().trim() || error.message}`);
  }
}

if (errors.length) {
  console.error(errors.map((error) => `error: ${error}`).join("\n"));
  process.exit(1);
}

console.log(`Release files are consistent at version ${pkg.version}.`);
