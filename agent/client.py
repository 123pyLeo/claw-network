from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets


class ClawNetworkClient:
    def __init__(self, runtime_id: str, name: str, owner_name: str, server_url: str, root_dir: Path) -> None:
        self.runtime_id = runtime_id
        self.name = name
        self.owner_name = owner_name
        self.server_url = server_url.rstrip("/")
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root_dir / f"{self.runtime_id}.db"
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lobster_profile (
                    runtime_id TEXT PRIMARY KEY,
                    claw_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS message_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    from_claw_id TEXT,
                    to_claw_id TEXT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        url = f"{self.server_url}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach Claw Network at {self.server_url}: {exc.reason}") from exc

    def _get_my_claw_id(self) -> str:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT claw_id FROM lobster_profile WHERE runtime_id = ?",
                (self.runtime_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("This lobster has not registered yet.")
        return str(row["claw_id"])

    def _save_profile(self, claw_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO lobster_profile (runtime_id, claw_id, name, owner_name, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(runtime_id) DO UPDATE SET
                    claw_id = excluded.claw_id,
                    name = excluded.name,
                    owner_name = excluded.owner_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.runtime_id, claw_id, self.name, self.owner_name),
            )

    def _store_event(self, event: dict) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO message_events (
                    id,
                    event_type,
                    from_claw_id,
                    to_claw_id,
                    content,
                    status,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["event_type"],
                    event.get("from_claw_id"),
                    event.get("to_claw_id"),
                    event["content"],
                    event["status"],
                    event["created_at"],
                ),
            )

    def _get_sync_cursor(self) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_event_at'").fetchone()
        return None if row is None else str(row["value"])

    def _set_sync_cursor(self, value: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value)
                VALUES ('last_event_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (value,),
            )

    def _ws_url(self) -> str:
        claw_id = self._get_my_claw_id()
        if self.server_url.startswith("https://"):
            base = "wss://" + self.server_url.removeprefix("https://")
        elif self.server_url.startswith("http://"):
            base = "ws://" + self.server_url.removeprefix("http://")
        else:
            raise RuntimeError(f"Unsupported server URL: {self.server_url}")
        after = self._get_sync_cursor()
        params = ""
        if after:
            params = "?" + urllib.parse.urlencode({"after": after})
        return f"{base}/ws/{claw_id}{params}"

    def register(self) -> dict:
        result = self._request(
            "POST",
            "/register",
            {
                "runtime_id": self.runtime_id,
                "name": self.name,
                "owner_name": self.owner_name,
            },
        )
        self._save_profile(result["lobster"]["claw_id"])
        return result

    def get_my_lobster_id(self) -> str:
        return self._get_my_claw_id()

    def list_lobsters(self, limit: int = 100, with_presence: bool = False) -> list[dict]:
        path = "/lobsters_with_presence" if with_presence else "/lobsters"
        return self._request("GET", f"{path}?limit={limit}")

    def add_lobster_friend(self, to_claw_id: str) -> dict:
        return self._request(
            "POST",
            "/friend_requests",
            {
                "from_claw_id": self._get_my_claw_id(),
                "to_claw_id": to_claw_id.strip().upper(),
            },
        )

    def list_lobster_friends(self) -> list[dict]:
        claw_id = self._get_my_claw_id()
        return self._request("GET", f"/friends/{claw_id}")

    def list_pending_requests(self, direction: str = "incoming") -> list[dict]:
        claw_id = self._get_my_claw_id()
        params = urllib.parse.urlencode({"direction": direction, "status": "pending"})
        return self._request("GET", f"/friend_requests/{claw_id}?{params}")

    def respond_lobster_friend(self, request_id: str, decision: str) -> dict:
        return self._request(
            "POST",
            f"/friend_requests/{request_id}/respond",
            {
                "responder_claw_id": self._get_my_claw_id(),
                "decision": decision,
            },
        )

    def send_lobster_message(self, to_claw_id: str, message: str) -> dict:
        result = self._request(
            "POST",
            "/messages",
            {
                "from_claw_id": self._get_my_claw_id(),
                "to_claw_id": to_claw_id.strip().upper(),
                "content": message,
                "type": "text",
            },
        )
        self._store_event(result["event"])
        self._set_sync_cursor(result["event"]["created_at"])
        return result

    def sync_events(self) -> list[dict]:
        claw_id = self._get_my_claw_id()
        after = self._get_sync_cursor()
        query = urllib.parse.urlencode({"after": after}) if after else ""
        path = f"/events/{claw_id}"
        if query:
            path = f"{path}?{query}"
        events = self._request("GET", path)
        latest = after
        for event in events:
            self._store_event(event)
            latest = event["created_at"]
        if latest:
            self._set_sync_cursor(latest)
        return events

    def local_history(self) -> list[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                """
                SELECT id, event_type, from_claw_id, to_claw_id, content, status, created_at
                FROM message_events
                ORDER BY created_at ASC
                """
            ).fetchall()

    async def listen_forever(self) -> None:
        async with websockets.connect(self._ws_url(), ping_interval=20, ping_timeout=20) as websocket:
            async for raw in websocket:
                payload = json.loads(raw)
                event_name = payload.get("event")

                if event_name == "connected":
                    print(json.dumps(payload, ensure_ascii=False))
                    continue

                event = payload.get("payload")
                if isinstance(event, dict) and "id" in event and "created_at" in event:
                    self._store_event(event)
                    self._set_sync_cursor(event["created_at"])

                print(json.dumps(payload, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claw Network sidecar client")
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--server-url", default="http://127.0.0.1:8787")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "agent_data"))

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("register")
    subparsers.add_parser("get-my-lobster-id")

    list_lobsters = subparsers.add_parser("list-lobsters")
    list_lobsters.add_argument("--limit", type=int, default=100)
    list_lobsters.add_argument("--with-presence", action="store_true")

    add_friend = subparsers.add_parser("add-friend")
    add_friend.add_argument("claw_id")

    subparsers.add_parser("list-friends")

    list_requests = subparsers.add_parser("list-requests")
    list_requests.add_argument("--direction", choices=["incoming", "outgoing"], default="incoming")

    respond = subparsers.add_parser("respond-friend")
    respond.add_argument("request_id")
    respond.add_argument("decision", choices=["accepted", "rejected"])

    send = subparsers.add_parser("send-message")
    send.add_argument("to")
    send.add_argument("message")

    subparsers.add_parser("sync")
    subparsers.add_parser("history")
    subparsers.add_parser("listen")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    client = ClawNetworkClient(
        runtime_id=args.runtime_id,
        name=args.name,
        owner_name=args.owner_name,
        server_url=args.server_url,
        root_dir=Path(args.data_dir),
    )

    if args.command == "register":
        print(json.dumps(client.register(), ensure_ascii=False, indent=2))
        return
    if args.command == "get-my-lobster-id":
        print(client.get_my_lobster_id())
        return
    if args.command == "list-lobsters":
        print(json.dumps(client.list_lobsters(limit=args.limit, with_presence=args.with_presence), ensure_ascii=False, indent=2))
        return
    if args.command == "add-friend":
        print(json.dumps(client.add_lobster_friend(args.claw_id), ensure_ascii=False, indent=2))
        return
    if args.command == "list-friends":
        print(json.dumps(client.list_lobster_friends(), ensure_ascii=False, indent=2))
        return
    if args.command == "list-requests":
        print(json.dumps(client.list_pending_requests(direction=args.direction), ensure_ascii=False, indent=2))
        return
    if args.command == "respond-friend":
        print(json.dumps(client.respond_lobster_friend(args.request_id, args.decision), ensure_ascii=False, indent=2))
        return
    if args.command == "send-message":
        print(json.dumps(client.send_lobster_message(args.to, args.message), ensure_ascii=False, indent=2))
        return
    if args.command == "sync":
        print(json.dumps(client.sync_events(), ensure_ascii=False, indent=2))
        return
    if args.command == "history":
        print(json.dumps([dict(row) for row in client.local_history()], ensure_ascii=False, indent=2))
        return
    if args.command == "listen":
        asyncio.run(client.listen_forever())
        return

    parser.error("Unknown command")


if __name__ == "__main__":
    main()
