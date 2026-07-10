#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build a .mcpb bundle (Claude Desktop extension) for the QGIS MCP server.

Stages the manifest and server script into dist/mcpb/ and packs them with the
official mcpb CLI (run via npx). The bundle version comes from pyproject.toml,
which is the canonical version for this project. Output: dist/qgis-mcp-<version>.mcpb.

Usage:
    uv run build_mcpb.py
"""

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).parent


def main() -> int:
    staging = REPO / "dist" / "mcpb"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "server").mkdir(parents=True)

    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((REPO / "mcpb" / "manifest.json").read_text())
    manifest["version"] = version
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy(REPO / "src" / "qgis_mcp" / "qgis_mcp_server.py", staging / "server" / "qgis_mcp_server.py")

    out = REPO / "dist" / f"qgis-mcp-{version}.mcpb"

    for cmd in (
        ["npx", "-y", "@anthropic-ai/mcpb", "validate", str(staging / "manifest.json")],
        ["npx", "-y", "@anthropic-ai/mcpb", "pack", str(staging), str(out)],
    ):
        result = subprocess.run(cmd)
        if result.returncode != 0:
            return result.returncode

    print(f"\nBuilt {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
