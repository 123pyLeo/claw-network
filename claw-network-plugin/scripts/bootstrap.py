from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Claw Network plugin config")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--python-bin", default="python3")
    parser.add_argument("--client-path", default="/home/openclaw-a2a-mvp/agent/client.py")
    parser.add_argument("--data-dir", default="/home/openclaw-a2a-mvp/agent_data")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = {
        "plugins": {
            "entries": {
                "claw-network": {
                    "enabled": True,
                    "endpoint": args.endpoint,
                    "runtimeId": args.runtime_id,
                    "name": args.name,
                    "ownerName": args.owner_name,
                    "pythonBin": args.python_bin,
                    "clientPath": args.client_path,
                    "dataDir": args.data_dir,
                }
            }
        }
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(output))


if __name__ == "__main__":
    main()
