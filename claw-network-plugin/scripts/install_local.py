from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def copy_plugin_tree(source_dir: Path, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)

    def ignore(_: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {"node_modules", "__pycache__", "claw-network.local.json"}}

    shutil.copytree(source_dir, target_dir, ignore=ignore)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Claw Network plugin into an OpenClaw home directory")
    parser.add_argument("--openclaw-home", default=str(Path.home() / ".openclaw"))
    parser.add_argument("--source-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--python-bin", default="python3")
    parser.add_argument("--client-path", default="/home/openclaw-a2a-mvp/agent/client.py")
    parser.add_argument("--data-dir", default="/home/openclaw-a2a-mvp/agent_data")
    parser.add_argument(
        "--sidecar-script",
        default="/home/openclaw-a2a-mvp/claw-network-plugin/scripts/sidecar_runner.py",
    )
    args = parser.parse_args()

    openclaw_home = Path(args.openclaw_home).expanduser().resolve()
    source_dir = Path(args.source_dir).resolve()
    extensions_dir = openclaw_home / "extensions"
    plugin_dir = extensions_dir / "claw-network"
    config_path = openclaw_home / "openclaw.json"

    extensions_dir.mkdir(parents=True, exist_ok=True)
    copy_plugin_tree(source_dir, plugin_dir)

    config = load_json(config_path)
    config.setdefault("plugins", {})
    config["plugins"].setdefault("allow", [])
    config["plugins"].setdefault("entries", {})
    config["plugins"].setdefault("installs", {})

    if "claw-network" not in config["plugins"]["allow"]:
        config["plugins"]["allow"].append("claw-network")

    config["plugins"]["entries"]["claw-network"] = {
        "enabled": True,
        "endpoint": args.endpoint,
        "runtimeId": args.runtime_id,
        "name": args.name,
        "ownerName": args.owner_name,
        "pythonBin": args.python_bin,
        "clientPath": args.client_path,
        "dataDir": args.data_dir,
        "sidecarScript": args.sidecar_script,
    }

    config["plugins"]["installs"]["claw-network"] = {
        "source": "local",
        "spec": str(source_dir),
        "installPath": str(plugin_dir),
        "version": "0.1.0",
        "installedAt": utc_now(),
    }

    write_json(config_path, config)

    print(
        json.dumps(
            {
                "installed_plugin_dir": str(plugin_dir),
                "updated_config": str(config_path),
                "next_step": (
                    f"{args.python_bin} {args.sidecar_script} --endpoint {args.endpoint}"
                    f" --runtime-id {args.runtime_id} --name {args.name}"
                    f" --owner-name {args.owner_name} --data-dir {args.data_dir}"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
