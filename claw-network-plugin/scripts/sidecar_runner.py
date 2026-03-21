from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent.client import ClawNetworkClient


async def run_forever(client: ClawNetworkClient) -> None:
    while True:
        try:
            registration = client.register()
            print(json.dumps({"event": "registered", "payload": registration}, ensure_ascii=False), flush=True)
            await client.listen_forever()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "event": "sidecar_error",
                        "detail": str(exc),
                        "retry_in_seconds": 3,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Claw Network sidecar with auto-register and auto-reconnect")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--data-dir", default="/home/openclaw-a2a-mvp/agent_data")
    args = parser.parse_args()

    client = ClawNetworkClient(
        runtime_id=args.runtime_id,
        name=args.name,
        owner_name=args.owner_name,
        server_url=args.endpoint,
        root_dir=Path(args.data_dir),
    )
    asyncio.run(run_forever(client))


if __name__ == "__main__":
    main()
