#!/usr/bin/env python3
"""Post-install smoke test for claw-network plugin configuration.

Validates that the installed claw-network plugin configuration matches the
manifest schema and that all required fields are present and well-formed.

Exit code 0 if all checks pass, 1 if any check fails.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_FIELDS = ("endpoint", "runtimeId", "name", "ownerName")


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def report(label: str, passed: bool) -> bool:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}")
    return passed


def check_openclaw_accepts_config(
    openclaw_bin: str,
    openclaw_home: Path,
) -> tuple[bool, str]:
    env = {
        **os.environ,
        "HOME": str(openclaw_home.parent),
        "OPENCLAW_CONFIG_PATH": str(openclaw_home / "openclaw.json"),
        "OPENCLAW_STATE_DIR": str(openclaw_home),
    }
    result = subprocess.run(
        [
            openclaw_bin,
            "config",
            "get",
            "plugins.entries.claw-network",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip()
    return False, detail


def run_checks(openclaw_home: Path, project_dir: Path, openclaw_bin: str) -> bool:
    all_passed = True

    # ---- Load manifest schema ------------------------------------------------
    manifest_path = project_dir / "claw-network-plugin" / "openclaw.plugin.json"
    print(f"Manifest : {manifest_path}")
    manifest = load_json(manifest_path)
    schema = manifest.get("configSchema", {})
    schema_props = set(schema.get("properties", {}).keys())
    onboarding_schema = schema.get("properties", {}).get("onboarding", {})
    onboarding_schema_props = set(onboarding_schema.get("properties", {}).keys())

    # ---- Load installed config -----------------------------------------------
    config_path = openclaw_home / "openclaw.json"
    print(f"Config   : {config_path}")
    installed = load_json(config_path)

    plugins_entries = installed.get("plugins", {}).get("entries", {})

    # (a) Plugin entry exists
    passed = "claw-network" in plugins_entries
    all_passed &= report("Plugin entry exists", passed)
    if not passed:
        print("    -> Cannot continue remaining checks without plugin entry.")
        return False

    plugin_entry = plugins_entries["claw-network"]
    config = plugin_entry.get("config")

    # (b) Config object exists
    passed = isinstance(config, dict)
    all_passed &= report("Config object exists", passed)
    if not passed:
        print("    -> Cannot continue remaining checks without config object.")
        return False

    # (c) No extra keys (additionalProperties: false)
    extra_keys = set(config.keys()) - schema_props
    passed = len(extra_keys) == 0
    all_passed &= report("No extra keys", passed)
    if not passed:
        print(f"    -> Extra keys: {sorted(extra_keys)}")

    # (d) Required fields present and non-empty strings
    for field in REQUIRED_FIELDS:
        value = config.get(field)
        field_ok = isinstance(value, str) and len(value) > 0
        all_passed &= report(f"Required field '{field}' present", field_ok)

    # (e) Endpoint format valid
    endpoint = config.get("endpoint", "")
    passed = isinstance(endpoint, str) and (
        endpoint.startswith("http://") or endpoint.startswith("https://")
    )
    all_passed &= report("Endpoint format valid", passed)
    if not passed:
        print(f"    -> endpoint = {endpoint!r}")

    # (f) Path fields valid (clientPath, dataDir) -- only if present & non-empty
    for field in ("clientPath", "dataDir"):
        value = config.get(field)
        if isinstance(value, str) and len(value) > 0:
            exists = Path(value).exists()
            all_passed &= report(f"Path field '{field}' exists on filesystem", exists)
            if not exists:
                print(f"    -> {field} = {value!r}")

    # (g) configVersion present and non-empty
    cv = config.get("configVersion")
    passed = isinstance(cv, str) and len(cv) > 0
    all_passed &= report("configVersion present", passed)

    # (h) Onboarding valid -- if present, all keys must be in manifest schema
    onboarding = config.get("onboarding")
    if isinstance(onboarding, dict):
        extra_onboarding = set(onboarding.keys()) - onboarding_schema_props
        passed = len(extra_onboarding) == 0
        all_passed &= report("Onboarding valid (no extra keys)", passed)
        if not passed:
            print(f"    -> Extra onboarding keys: {sorted(extra_onboarding)}")

    # (i) Target OpenClaw version accepts the generated config
    passed, detail = check_openclaw_accepts_config(openclaw_bin, openclaw_home)
    all_passed &= report("OpenClaw accepts config", passed)
    if not passed and detail:
        print(f"    -> {detail}")

    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-install smoke test for claw-network plugin configuration."
    )
    parser.add_argument(
        "--openclaw-home",
        type=Path,
        default=Path.home() / ".openclaw",
        help="Path to the openclaw home directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the project root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--openclaw-bin",
        default="openclaw",
        help="OpenClaw CLI binary to use for runtime config validation (default: openclaw)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("claw-network post-install smoke test")
    print("=" * 60)

    try:
        all_passed = run_checks(args.openclaw_home, args.project_dir, args.openclaw_bin)
    except FileNotFoundError as exc:
        print(f"\n  [FAIL] File not found: {exc}")
        all_passed = False
    except json.JSONDecodeError as exc:
        print(f"\n  [FAIL] Invalid JSON: {exc}")
        all_passed = False

    print("=" * 60)
    if all_passed:
        print("Result: ALL CHECKS PASSED")
        sys.exit(0)
    else:
        print("Result: ONE OR MORE CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
