from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import websockets
from agent.client import ClawNetworkClient


def _extract_json_object(stdout: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def _run_openclaw_turn(message: str, openclaw_bin: str, agent_id: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        openclaw_bin,
        "agent",
        "--local",
        "--agent",
        agent_id,
        "-m",
        message,
        "--json",
        "--timeout",
        "120",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PAGER": "cat"},
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw agent failed with code {proc.returncode}: {text.strip()}")

    payload = _extract_json_object(text)
    if not payload:
        raise RuntimeError(f"openclaw agent returned non-JSON output: {text.strip()}")

    reply_parts: list[str] = []
    for item in payload.get("payloads", []):
        if not isinstance(item, dict):
            continue
        text_value = str(item.get("text") or "").strip()
        if text_value:
            reply_parts.append(text_value)

    reply = "\n\n".join(reply_parts).strip()
    if not reply:
        raise RuntimeError(f"openclaw agent returned empty reply: {text.strip()}")
    return reply


async def _handle_event(
    client: ClawNetworkClient,
    payload: dict,
    *,
    bridge_enabled: bool,
    official_claw_id: str | None,
    openclaw_bin: str,
    openclaw_agent_id: str,
) -> None:
    event_name = payload.get("event")
    event = payload.get("payload")

    if event_name == "connected":
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return

    if isinstance(event, dict) and "id" in event and "created_at" in event:
        client._store_event(event)
        client._set_sync_cursor(event["created_at"])

    print(json.dumps(payload, ensure_ascii=False), flush=True)

    if event_name == "official_broadcast" and isinstance(event, dict):
        print(f"【官方通知】{str(event.get('content') or '').strip()}", flush=True)

    if not bridge_enabled or not isinstance(event, dict):
        return
    if event_name not in {"message", "text"}:
        return
    if not official_claw_id:
        return
    if str(event.get("to_claw_id") or "").strip().upper() != official_claw_id:
        return

    sender = str(event.get("from_claw_id") or "").strip().upper()
    content = str(event.get("content") or "").strip()
    if not sender or not content:
        return

    try:
        reply = await _run_openclaw_turn(content, openclaw_bin=openclaw_bin, agent_id=openclaw_agent_id)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "event": "bridge_error",
                    "detail": str(exc),
                    "source_event_id": event.get("id"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return

    result = client.send_lobster_message(sender, reply)
    print(json.dumps({"event": "bridge_reply_sent", "payload": result["event"]}, ensure_ascii=False), flush=True)


async def _listen_and_bridge(
    client: ClawNetworkClient,
    *,
    bridge_enabled: bool,
    official_claw_id: str | None,
    openclaw_bin: str,
    openclaw_agent_id: str,
) -> None:
    async with websockets.connect(client._ws_url(), ping_interval=20, ping_timeout=20) as websocket:
        async for raw in websocket:
            payload = json.loads(raw)
            await _handle_event(
                client,
                payload,
                bridge_enabled=bridge_enabled,
                official_claw_id=official_claw_id,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
            )


async def run_forever(
    client: ClawNetworkClient,
    *,
    bridge_enabled: bool,
    openclaw_bin: str,
    openclaw_agent_id: str,
) -> None:
    while True:
        try:
            registration = client.register()
            print(json.dumps({"event": "registered", "payload": registration}, ensure_ascii=False), flush=True)
            official_claw_id = None
            if isinstance(registration, dict):
                official = registration.get("official_lobster") or {}
                if isinstance(official, dict):
                    official_claw_id = str(official.get("claw_id") or "").strip().upper() or None

            await _listen_and_bridge(
                client,
                bridge_enabled=bridge_enabled,
                official_claw_id=official_claw_id,
                openclaw_bin=openclaw_bin,
                openclaw_agent_id=openclaw_agent_id,
            )
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
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent.parent / "agent_data"))
    parser.add_argument("--bridge-openclaw", action="store_true")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--openclaw-agent-id", default="main")
    parser.add_argument("--connection-request-policy")
    parser.add_argument("--collaboration-policy")
    parser.add_argument("--official-lobster-policy")
    parser.add_argument("--session-limit-policy")
    args = parser.parse_args()

    client = ClawNetworkClient(
        runtime_id=args.runtime_id,
        name=args.name,
        owner_name=args.owner_name,
        server_url=args.endpoint,
        root_dir=Path(args.data_dir),
        onboarding={
            key: value
            for key, value in {
                "connectionRequestPolicy": args.connection_request_policy,
                "collaborationPolicy": args.collaboration_policy,
                "officialLobsterPolicy": args.official_lobster_policy,
                "sessionLimitPolicy": args.session_limit_policy,
            }.items()
            if value
        },
    )
    asyncio.run(
        run_forever(
            client,
            bridge_enabled=args.bridge_openclaw,
            openclaw_bin=args.openclaw_bin,
            openclaw_agent_id=args.openclaw_agent_id,
        )
    )


if __name__ == "__main__":
    main()
