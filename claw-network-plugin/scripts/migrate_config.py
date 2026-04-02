"""Migrate claw-network plugin configuration between schema versions.

Currently supported migrations:
  v0 -> v1: Remove config keys not present in the manifest schema whitelist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Re-use helpers already defined in the sibling install script.
from install_local import load_allowed_config_keys, load_json, write_json

SOURCE_DIR = Path(__file__).resolve().parents[1]  # claw-network-plugin root


def _resolve_config_path(openclaw_home: Path) -> Path:
    return openclaw_home / "openclaw.json"


def _get_plugin_config(config: dict) -> dict | None:
    """Return the mutable plugin config dict, or None if it doesn't exist."""
    try:
        return config["plugins"]["entries"]["claw-network"]["config"]
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def migrate_v0_to_v1(plugin_config: dict, allowed_keys: set[str]) -> list[str]:
    """Remove keys not in the manifest schema and bump configVersion to '1'.

    Returns a list of human-readable change descriptions.
    """
    changes: list[str] = []

    # Remove keys that are not in the manifest whitelist.
    keys_to_remove = [k for k in plugin_config if k not in allowed_keys]
    for key in sorted(keys_to_remove):
        changes.append(f"  removed key: {key}")
        del plugin_config[key]

    # Stamp the new version.
    plugin_config["configVersion"] = "1"
    changes.append("  set configVersion = \"1\"")

    return changes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate claw-network plugin config to the latest schema version",
    )
    parser.add_argument(
        "--openclaw-home",
        default=str(Path.home() / ".openclaw"),
        help="Path to the OpenClaw home directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to disk",
    )
    args = parser.parse_args()

    openclaw_home = Path(args.openclaw_home).expanduser().resolve()
    config_path = _resolve_config_path(openclaw_home)

    # --- Load files ---------------------------------------------------------
    config = load_json(config_path)
    if not config:
        print(f"Config file not found or empty: {config_path}")
        sys.exit(1)

    plugin_config = _get_plugin_config(config)
    if plugin_config is None:
        print("No claw-network plugin config found in openclaw.json — nothing to migrate.")
        sys.exit(0)

    current_version = plugin_config.get("configVersion", "0")
    allowed_keys = load_allowed_config_keys(SOURCE_DIR)

    # --- Run migrations in order -------------------------------------------
    all_changes: list[str] = []

    if current_version == "0":
        print(f"Migrating v0 → v1 …")
        changes = migrate_v0_to_v1(plugin_config, allowed_keys)
        all_changes.extend(changes)
        current_version = "1"

    # (future migrations would be chained here)

    # --- Report & persist ---------------------------------------------------
    if not all_changes:
        print("Config is already up-to-date — no changes needed.")
        sys.exit(0)

    print()
    print("Changes:")
    for line in all_changes:
        print(line)
    print()

    if args.dry_run:
        print("[dry-run] No files were written.")
    else:
        write_json(config_path, config)
        print(f"Updated {config_path}")


if __name__ == "__main__":
    main()
