#!/usr/bin/env python3
"""One-time instance repair script for claw-network.

Reads the local openclaw.json configuration, loads the plugin manifest
schema, and applies a series of repairs to bring the config into
compliance.  Writes back only when changes are detected.
"""

import argparse
import copy
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Read and parse a JSON file, raising on any error."""
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Repair steps
# ---------------------------------------------------------------------------

def repair_illegal_keys(
    plugin_cfg: dict,
    allowed_keys: set[str],
    changes: list[str],
) -> None:
    """Remove config keys that are not in the manifest schema properties."""
    illegal = set(plugin_cfg.keys()) - allowed_keys
    for key in sorted(illegal):
        del plugin_cfg[key]
        changes.append(f"Removed illegal config key: {key!r}")


def repair_onboarding(
    plugin_cfg: dict,
    onboarding_allowed: set[str],
    changes: list[str],
) -> None:
    """Remove keys inside onboarding that are not in the manifest schema."""
    onboarding = plugin_cfg.get("onboarding")
    if not isinstance(onboarding, dict):
        return

    illegal = set(onboarding.keys()) - onboarding_allowed
    for key in sorted(illegal):
        del onboarding[key]
        changes.append(f"Removed illegal onboarding key: {key!r}")


def repair_config_version(
    plugin_cfg: dict,
    allowed_keys: set[str],
    changes: list[str],
) -> None:
    """Set configVersion to '1' when the installed manifest supports it."""
    if "configVersion" in allowed_keys and "configVersion" not in plugin_cfg:
        plugin_cfg["configVersion"] = "1"
        changes.append("Set configVersion to \"1\"")


def warn_empty_required_fields(
    plugin_cfg: dict,
    warnings: list[str],
) -> None:
    """Print warnings for required fields that are empty or missing."""
    required = ["endpoint", "runtimeId", "name", "ownerName"]
    for field in required:
        val = plugin_cfg.get(field)
        if not val:  # None, "", or missing
            warnings.append(
                f"Required field {field!r} is empty or missing — "
                f"please set it manually"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    default_project_dir = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description="One-time repair for a claw-network instance config",
    )
    parser.add_argument(
        "--openclaw-home",
        type=Path,
        default=Path("~/.openclaw").expanduser(),
        help="Path to the openclaw home directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=default_project_dir,
        help=(
            "Path to the project root "
            f"(default: {default_project_dir})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing to disk",
    )
    args = parser.parse_args()

    openclaw_home: Path = args.openclaw_home
    project_dir: Path = args.project_dir
    dry_run: bool = args.dry_run

    # ---- Load openclaw.json ------------------------------------------------

    config_path = openclaw_home / "openclaw.json"
    if not config_path.is_file():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config = _load_json(config_path)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Cannot read config: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- Load manifest schema ----------------------------------------------

    manifest_path = (
        project_dir / "claw-network-plugin" / "openclaw.plugin.json"
    )
    if not manifest_path.is_file():
        print(
            f"ERROR: Manifest not found: {manifest_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        manifest = _load_json(manifest_path)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Cannot read manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    config_schema = manifest.get("configSchema", {})
    schema_props = config_schema.get("properties", {})
    allowed_keys: set[str] = set(schema_props.keys())

    onboarding_schema = schema_props.get("onboarding", {})
    onboarding_props = onboarding_schema.get("properties", {})
    onboarding_allowed: set[str] = set(onboarding_props.keys())

    # ---- Navigate to plugin config -----------------------------------------

    try:
        plugin_cfg = config["plugins"]["entries"]["claw-network"]["config"]
    except (KeyError, TypeError):
        print(
            "ERROR: plugins.entries.claw-network.config not found in "
            f"{config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Keep a snapshot so we can detect changes
    original = copy.deepcopy(plugin_cfg)

    # ---- Run repairs -------------------------------------------------------

    changes: list[str] = []
    warnings: list[str] = []

    # 3a. Remove illegal keys
    repair_illegal_keys(plugin_cfg, allowed_keys, changes)

    # 3b. Fix onboarding
    repair_onboarding(plugin_cfg, onboarding_allowed, changes)

    # 3c. Set configVersion
    repair_config_version(plugin_cfg, allowed_keys, changes)

    # 3d. Warn on empty required fields
    warn_empty_required_fields(plugin_cfg, warnings)

    # ---- Report ------------------------------------------------------------

    print("=" * 60)
    print("claw-network instance repair")
    print("=" * 60)
    print(f"  openclaw-home : {openclaw_home}")
    print(f"  project-dir   : {project_dir}")
    print(f"  config file   : {config_path}")
    print(f"  manifest      : {manifest_path}")
    if dry_run:
        print("  mode          : DRY RUN (no changes written)")
    print("=" * 60)
    print()

    if changes:
        print(f"Repairs ({len(changes)}):")
        for change in changes:
            print(f"  - {change}")
    else:
        print("No repairs needed.")

    if warnings:
        print()
        print(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  ! {warning}")

    # ---- Write back (only if changes were actually made) -------------------

    modified = plugin_cfg != original

    if modified and not dry_run:
        try:
            _write_json(config_path, config)
            print()
            print(f"Config written to {config_path}")
        except OSError as exc:
            print(
                f"\nERROR: Failed to write config: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
    elif modified and dry_run:
        print()
        print("Dry run — no changes written.")
    else:
        print()
        print("Config unchanged — nothing to write.")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
