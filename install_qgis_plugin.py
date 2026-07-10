#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Install the QGIS MCP plugin into a QGIS user profile.

Copies (or symlinks, with --symlink) the qgis_mcp_plugin folder into the
QGIS profile's python/plugins directory. After running this, restart QGIS
and enable "QGIS MCP" in Plugins -> Manage and Install Plugins.

Usage:
    uv run install_qgis_plugin.py [--profile NAME] [--symlink]
"""

import argparse
import platform
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "qgis_mcp_plugin"


def default_profiles_root() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library/Application Support/QGIS/QGIS3/profiles"
    if system == "Windows":
        return home / "AppData/Roaming/QGIS/QGIS3/profiles"
    return home / ".local/share/QGIS/QGIS3/profiles"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default", help="QGIS profile name (default: default)")
    parser.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink instead of copy, so plugin edits in this repo take effect on QGIS restart",
    )
    args = parser.parse_args()

    src = Path(__file__).parent / PLUGIN_NAME
    if not src.is_dir():
        print(f"error: {src} not found", file=sys.stderr)
        return 1

    profile_dir = default_profiles_root() / args.profile
    if not profile_dir.is_dir():
        print(f"error: QGIS profile not found at {profile_dir}", file=sys.stderr)
        print("Find your profile folder in QGIS: Settings -> User Profiles -> Open Active Profile Folder", file=sys.stderr)
        return 1

    plugins_dir = profile_dir / "python" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / PLUGIN_NAME

    if dest.is_symlink() or dest.is_file():
        dest.unlink()
        print(f"Removed existing {dest}")
    elif dest.is_dir():
        shutil.rmtree(dest)
        print(f"Removed existing {dest}")

    if args.symlink:
        dest.symlink_to(src.resolve())
        print(f"Symlinked {dest} -> {src.resolve()}")
    else:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"Copied plugin to {dest}")

    print("\nNext steps:")
    print("  1. Restart QGIS")
    print('  2. Plugins -> Manage and Install Plugins -> enable "QGIS MCP"')
    print("  3. Plugins -> QGIS MCP -> QGIS MCP -> Start Server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
