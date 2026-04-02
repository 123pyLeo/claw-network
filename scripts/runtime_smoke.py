from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PASS = 0
FAIL = 0


def pass_check(message: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS: {message}")



def fail_check(message: str) -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL: {message}")



def run_command(command: list[str]) -> tuple[bool, str]:
    result = subprocess.run(command, capture_output=True, text=True)
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
    return result.returncode == 0, output



def main() -> int:
    parser = argparse.ArgumentParser(description="Run runtime smoke test for claw-network install flow")
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--endpoint", default="https://api.sandpile.io")
    parser.add_argument("--python-bin", default="python3")
    parser.add_argument("--openclaw-bin", default="openclaw")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    python_bin = args.python_bin
    endpoint = args.endpoint
    openclaw_bin = args.openclaw_bin

    runtime_id = "smoke-runtime"
    name = "烟雾测试龙虾"
    owner_name = "Smoke Tester"
    client_path = project_dir / "agent" / "client.py"
    data_dir = project_dir / "agent_data"
    sidecar_script = project_dir / "claw-network-plugin" / "scripts" / "sidecar_runner.py"
    install_script = project_dir / "claw-network-plugin" / "scripts" / "install_local.py"
    smoke_test_script = project_dir / "scripts" / "smoke_test.py"

    with tempfile.TemporaryDirectory(prefix="claw-network-smoke-") as temp_dir:
        openclaw_home = Path(temp_dir)
        install_command = [
            python_bin,
            str(install_script),
            "--source-dir",
            str(project_dir / "claw-network-plugin"),
            "--openclaw-home",
            str(openclaw_home),
            "--endpoint",
            endpoint,
            "--runtime-id",
            runtime_id,
            "--name",
            name,
            "--owner-name",
            owner_name,
            "--no-onboarding",
            "--python-bin",
            python_bin,
            "--client-path",
            str(client_path),
            "--data-dir",
            str(data_dir),
            "--sidecar-script",
            str(sidecar_script),
        ]
        ok, output = run_command(install_command)
        if ok:
            pass_check("installer completed")
        else:
            fail_check(f"installer failed\n{output}")

        config_path = openclaw_home / "openclaw.json"
        if config_path.exists():
            pass_check("openclaw.json created")
        else:
            fail_check(f"openclaw.json missing: {config_path}")

        smoke_ok, smoke_output = run_command(
            [
                python_bin,
                str(smoke_test_script),
                "--openclaw-home",
                str(openclaw_home),
                "--project-dir",
                str(project_dir),
                "--openclaw-bin",
                openclaw_bin,
            ]
        )
        if smoke_ok:
            pass_check("smoke_test.py passed")
        else:
            fail_check(f"smoke_test.py failed\n{smoke_output}")

        config = {}
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))

        plugin_config = (
            config.get("plugins", {})
            .get("entries", {})
            .get("claw-network", {})
            .get("config", {})
        )
        if plugin_config:
            pass_check("plugin config exists")
        else:
            fail_check("plugin config missing at plugins.entries.claw-network.config")

        if plugin_config.get("configVersion") == "1":
            pass_check("configVersion is 1")
        else:
            fail_check(f"unexpected configVersion: {plugin_config.get('configVersion')!r}")

        for key, expected in {
            "clientPath": str(client_path),
            "dataDir": str(data_dir),
            "sidecarScript": str(sidecar_script),
        }.items():
            actual = plugin_config.get(key)
            if actual == expected:
                pass_check(f"{key} matches expected project path")
            else:
                fail_check(f"{key} mismatch: expected {expected}, got {actual}")

        installed_plugin_dir = openclaw_home / "extensions" / "claw-network"
        if installed_plugin_dir.exists():
            pass_check("plugin installed into temporary OpenClaw home")
        else:
            fail_check(f"installed plugin dir missing: {installed_plugin_dir}")

    print(f"SUMMARY: pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
