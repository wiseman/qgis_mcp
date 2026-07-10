#!/usr/bin/env node
// Build a QGIS Plugin Manager-compatible ZIP without external dependencies.
// The archive has a unique root directory, as QGIS requires. It deliberately
// differs from qgis_mcp_plugin, which is used by an unrelated catalog plugin.
import { deflateRawSync } from "node:zlib";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repo = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(readFileSync(path.join(repo, "package.json"), "utf8"));
const source = path.join(repo, "qgis_mcp_plugin");
const pluginId = "qgis_mcp_wiseman";
const outputDir = path.join(repo, "dist");
const output = path.join(outputDir, `qgis-mcp-plugin-${pkg.version}.zip`);

function filesBelow(directory, prefix = pluginId) {
  return readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.name !== "__pycache__" && entry.name !== ".DS_Store")
    .flatMap((entry) => {
      const diskPath = path.join(directory, entry.name);
      const archivePath = `${prefix}/${entry.name}`;
      return entry.isDirectory() ? filesBelow(diskPath, archivePath) : [{ diskPath, archivePath }];
    });
}

const crcTable = Array.from({ length: 256 }, (_, value) => {
  let crc = value;
  for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  return crc >>> 0;
});

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 0xff];
  return (crc ^ 0xffffffff) >>> 0;
}

function dosTimestamp(date = new Date()) {
  const year = Math.max(date.getFullYear(), 1980) - 1980;
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1),
    date: (year << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  };
}

const localParts = [];
const centralParts = [];
let offset = 0;
const timestamp = dosTimestamp();

for (const file of filesBelow(source)) {
  let data = readFileSync(file.diskPath);
  if (file.archivePath.endsWith("/metadata.txt")) {
    const text = data.toString("utf8").replace(/^version=.*$/m, `version=${pkg.version}`);
    data = Buffer.from(text);
  }
  const compressed = deflateRawSync(data);
  const name = Buffer.from(file.archivePath.replaceAll(path.sep, "/"));
  const crc = crc32(data);

  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt16LE(8, 6); // UTF-8 names
  local.writeUInt16LE(8, 8); // deflate
  local.writeUInt16LE(timestamp.time, 10);
  local.writeUInt16LE(timestamp.date, 12);
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(compressed.length, 18);
  local.writeUInt32LE(data.length, 22);
  local.writeUInt16LE(name.length, 26);
  localParts.push(local, name, compressed);

  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0);
  central.writeUInt16LE(20, 4);
  central.writeUInt16LE(20, 6);
  central.writeUInt16LE(8, 8);
  central.writeUInt16LE(8, 10);
  central.writeUInt16LE(timestamp.time, 12);
  central.writeUInt16LE(timestamp.date, 14);
  central.writeUInt32LE(crc, 16);
  central.writeUInt32LE(compressed.length, 20);
  central.writeUInt32LE(data.length, 24);
  central.writeUInt16LE(name.length, 28);
  central.writeUInt32LE(offset, 42);
  centralParts.push(central, name);
  offset += local.length + name.length + compressed.length;
}

const centralDirectory = Buffer.concat(centralParts);
const end = Buffer.alloc(22);
end.writeUInt32LE(0x06054b50, 0);
const fileCount = centralParts.length / 2;
end.writeUInt16LE(fileCount, 8);
end.writeUInt16LE(fileCount, 10);
end.writeUInt32LE(centralDirectory.length, 12);
end.writeUInt32LE(offset, 16);

mkdirSync(outputDir, { recursive: true });
rmSync(output, { force: true });
writeFileSync(output, Buffer.concat([...localParts, centralDirectory, end]));
console.log(`Built ${output}`);
