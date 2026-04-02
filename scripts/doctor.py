#!/usr/bin/env python3
"""Self-diagnostic command for claw-network.

Runs a series of checks against the local configuration, file system,
and network services, printing PASS / FAIL / WARN for each one.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

_counts = {"PASS": 0, "FAIL": 0, "WARN": 0}


def _report(status: str, label: str, detail: str = "") -> None:
    _counts[status] += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"[{status}] {label}{suffix}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_config_exists(openclaw_home: Path) -> dict | None:
    """1. Config exists: ~/.openclaw/openclaw.json"""
    config_path = openclaw_home / "openclaw.json"
    if config_path.is_file():
        _report("PASS", "Config exists", str(config_path))
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _report("FAIL", "Config readable", str(exc))
            return None
    else:
        _report("FAIL", "Config exists", f"{config_path} not found")
        return None


def check_plugin_config_present(config: dict | None) -> dict | None:
    """2. Plugin config present: plugins.entries.claw-network.config"""
    if config is None:
        _report("FAIL", "Plugin config present", "no config loaded")
        return None

    try:
        plugin_cfg = config["plugins"]["entries"]["claw-network"]["config"]
        _report("PASS", "Plugin config present")
        return plugin_cfg
    except (KeyError, TypeError):
        _report("FAIL", "Plugin config present",
                "plugins.entries.claw-network.config not found")
        return None


def check_config_schema_valid(plugin_cfg: dict | None,
                              project_dir: Path) -> None:
    """3. Config schema valid against the plugin manifest."""
    manifest_path = project_dir / "claw-network-plugin" / "openclaw.plugin.json"

    if not manifest_path.is_file():
        _report("WARN", "Config schema valid",
                f"manifest not found at {manifest_path}")
        return

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _report("FAIL", "Config schema valid",
                f"cannot read manifest: {exc}")
        return

    if plugin_cfg is None:
        _report("FAIL", "Config schema valid", "no plugin config to validate")
        return

    # Determine allowed keys from the manifest's config schema
    allowed_keys: set[str] = set()
    schema = manifest.get("config", manifest.get("configSchema", {}))
    if isinstance(schema, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            allowed_keys = set(properties.keys())

    if allowed_keys:
        extra = set(plugin_cfg.keys()) - allowed_keys
        if extra:
            _report("FAIL", "Config schema valid",
                    f"unexpected config keys: {extra}")
            return

    # Required fields must be non-empty
    required = ["endpoint", "runtimeId", "name", "ownerName"]
    missing = []
    for field in required:
        val = plugin_cfg.get(field)
        if not val:  # None, "", or missing
            missing.append(field)

    if missing:
        _report("FAIL", "Config schema valid",
                f"required fields missing or empty: {missing}")
    else:
        _report("PASS", "Config schema valid")


def check_config_version(plugin_cfg: dict | None) -> None:
    """4. configVersion present."""
    if plugin_cfg is None:
        _report("FAIL", "configVersion present", "no plugin config loaded")
        return

    if "configVersion" in plugin_cfg:
        _report("PASS", "configVersion present",
                f"value={plugin_cfg['configVersion']}")
    else:
        _report("FAIL", "configVersion present",
                "configVersion key missing from plugin config")


def check_paths(plugin_cfg: dict | None) -> None:
    """5. Path checks: clientPath, dataDir, sidecarScript."""
    if plugin_cfg is None:
        _report("FAIL", "Path checks", "no plugin config loaded")
        return

    path_keys = ["clientPath", "dataDir", "sidecarScript"]
    for key in path_keys:
        raw = plugin_cfg.get(key)
        if raw is None:
            _report("WARN", f"Path check ({key})", "key not set in config")
            continue
        p = Path(raw).expanduser()
        if p.exists():
            _report("PASS", f"Path check ({key})", str(p))
        else:
            _report("FAIL", f"Path check ({key})", f"{p} does not exist")


def check_endpoint_format(plugin_cfg: dict | None) -> str | None:
    """6. Endpoint format: must start with http:// or https://."""
    if plugin_cfg is None:
        _report("FAIL", "Endpoint format", "no plugin config loaded")
        return None

    endpoint = plugin_cfg.get("endpoint", "")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        _report("PASS", "Endpoint format", endpoint)
        return endpoint
    else:
        _report("FAIL", "Endpoint format",
                f"endpoint does not start with http(s)://: {endpoint!r}")
        return None


def check_endpoint_reachable(endpoint: str | None) -> None:
    """7. Endpoint reachable: GET <endpoint>/health with 5 s timeout."""
    if endpoint is None:
        _report("FAIL", "Endpoint reachable", "no valid endpoint")
        return

    url = endpoint.rstrip("/") + "/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            status = resp.status
        if 200 <= status < 300:
            _report("PASS", "Endpoint reachable", f"{url} -> {status}")
        else:
            _report("WARN", "Endpoint reachable", f"{url} -> HTTP {status}")
    except Exception as exc:
        _report("FAIL", "Endpoint reachable", f"{url} -> {exc}")


def check_gateway_health() -> None:
    """8. Gateway health: GET http://127.0.0.1:18789/health with 3 s timeout."""
    url = "http://127.0.0.1:18789/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=3) as resp:
            status = resp.status
        if 200 <= status < 300:
            _report("PASS", "Gateway health", f"{url} -> {status}")
        else:
            _report("WARN", "Gateway health", f"{url} -> HTTP {status}")
    except Exception as exc:
        _report("FAIL", "Gateway health", f"{url} -> {exc}")


def check_sidecar_process() -> None:
    """9. Sidecar process: look for a running sidecar_runner process."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "sidecar_runner"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().splitlines()
            _report("PASS", "Sidecar process",
                    f"running (PIDs: {', '.join(pids)})")
        else:
            _report("WARN", "Sidecar process", "no sidecar_runner process found")
    except FileNotFoundError:
        _report("WARN", "Sidecar process", "pgrep not available on this system")
    except Exception as exc:
        _report("FAIL", "Sidecar process", str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    default_project_dir = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description="claw-network self-diagnostic tool",
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
    args = parser.parse_args()

    openclaw_home: Path = args.openclaw_home
    project_dir: Path = args.project_dir

    print("=" * 60)
    print("claw-network doctor")
    print("=" * 60)
    print(f"  openclaw-home : {openclaw_home}")
    print(f"  project-dir   : {project_dir}")
    print("=" * 60)
    print()

    # 1. Config exists
    config = check_config_exists(openclaw_home)

    # 2. Plugin config present
    plugin_cfg = check_plugin_config_present(config)

    # 3. Config schema valid
    check_config_schema_valid(plugin_cfg, project_dir)

    # 4. configVersion present
    check_config_version(plugin_cfg)

    # 5. Path checks
    check_paths(plugin_cfg)

    # 6. Endpoint format
    endpoint = check_endpoint_format(plugin_cfg)

    # 7. Endpoint reachable
    check_endpoint_reachable(endpoint)

    # 8. Gateway health
    check_gateway_health()

    # 9. Sidecar process
    check_sidecar_process()

    # Summary
    print()
    print("=" * 60)
    total = _counts["PASS"] + _counts["FAIL"] + _counts["WARN"]
    print(
        f"Summary: {total} checks — "
        f"{_counts['PASS']} PASS, "
        f"{_counts['FAIL']} FAIL, "
        f"{_counts['WARN']} WARN"
    )
    print("=" * 60)

    if _counts["FAIL"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
