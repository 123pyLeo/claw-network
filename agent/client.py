from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets


class ClawNetworkClient:
    def __init__(
        self,
        runtime_id: str,
        name: str,
        owner_name: str,
        server_url: str,
        root_dir: Path,
        *,
        onboarding: dict | None = None,
    ) -> None:
        self.runtime_id = runtime_id
        self.name = name
        self.owner_name = owner_name
        self.server_url = server_url.rstrip("/")
        self.root_dir = root_dir
        self.onboarding = onboarding or {}
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
                    auth_token TEXT,
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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(lobster_profile)").fetchall()}
            if "auth_token" not in columns:
                conn.execute("ALTER TABLE lobster_profile ADD COLUMN auth_token TEXT")

    def _get_auth_token(self) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT auth_token FROM lobster_profile WHERE runtime_id = ?",
                (self.runtime_id,),
            ).fetchone()
        if row is None:
            return None
        token = str(row["auth_token"] or "").strip()
        return token or None

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        url = f"{self.server_url}{path}"
        data = None
        headers = {}
        auth_token = self._get_auth_token()
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=20) as resp:
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

    def _save_profile(self, claw_id: str, auth_token: str | None = None) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO lobster_profile (runtime_id, claw_id, auth_token, name, owner_name, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(runtime_id) DO UPDATE SET
                    claw_id = excluded.claw_id,
                    auth_token = COALESCE(excluded.auth_token, lobster_profile.auth_token),
                    name = excluded.name,
                    owner_name = excluded.owner_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.runtime_id, claw_id, auth_token, self.name, self.owner_name),
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

    @staticmethod
    def _status_label(status: str) -> str:
        labels = {
            "queued": "排队中",
            "delivered": "已送达",
            "consumed": "已接收",
            "read": "已读",
            "failed": "失败",
        }
        return labels.get(status, status)

    def _decorate_event(self, event: dict) -> dict:
        payload = dict(event)
        payload["status_label"] = payload.get("status_label") or self._status_label(str(payload.get("status", "")))
        return payload

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

    def acknowledge_event(self, event_id: str, status: str) -> dict:
        result = self._request(
            "POST",
            f"/events/{event_id}/ack",
            {
                "claw_id": self._get_my_claw_id(),
                "status": status,
            },
        )
        return self._decorate_event(result)

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
        token = self._get_auth_token()
        query_params = {}
        if after:
            query_params["after"] = after
        if token:
            query_params["token"] = token
        if query_params:
            params = "?" + urllib.parse.urlencode(query_params)
        return f"{base}/ws/{claw_id}{params}"

    def register(self) -> dict:
        payload = {
            "runtime_id": self.runtime_id,
            "name": self.name,
            "owner_name": self.owner_name,
        }
        if self.onboarding:
            payload["onboarding"] = self.onboarding
        result = self._request("POST", "/register", payload)
        self._save_profile(result["lobster"]["claw_id"], result.get("auth_token"))
        return result

    def get_my_lobster_id(self) -> str:
        return self._get_my_claw_id()

    def list_lobsters(self, limit: int = 100, with_presence: bool = False) -> list[dict]:
        path = "/lobsters_with_presence" if with_presence else "/lobsters"
        return self._request("GET", f"{path}?limit={limit}")

    @staticmethod
    def _looks_like_claw_id(value: str) -> bool:
        normalized = value.strip().upper()
        return normalized.startswith("CLAW-") and len(normalized) >= 8

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def resolve_lobster(self, query: str, limit: int = 10) -> dict:
        needle = query.strip()
        if not needle:
            return {"status": "not_found", "query": query, "matches": []}

        normalized = self._normalize_text(needle)
        direct_id = needle.strip().upper()

        matches_by_id: dict[str, dict] = {}

        if self._looks_like_claw_id(needle):
            for row in self.list_lobsters(limit=limit, with_presence=True):
                claw_id = str(row["claw_id"]).strip().upper()
                if claw_id == direct_id:
                    matches_by_id[claw_id] = {
                        "claw_id": claw_id,
                        "name": row["name"],
                        "owner_name": row["owner_name"],
                        "online": bool(row.get("online", False)),
                        "source": "direct_id",
                        "score": 0,
                    }

        try:
            for row in self.list_lobster_friends():
                friend_name = str(row["friend_name"]).strip()
                friend_norm = self._normalize_text(friend_name)
                if friend_norm == normalized:
                    score = 0
                elif normalized in friend_norm:
                    score = 10
                else:
                    continue
                claw_id = str(row["friend_claw_id"]).strip().upper()
                matches_by_id.setdefault(
                    claw_id,
                    {
                        "claw_id": claw_id,
                        "name": friend_name,
                        "owner_name": None,
                        "online": None,
                        "source": "friend",
                        "score": score,
                    },
                )
        except RuntimeError:
            pass

        for row in self._request("GET", f"/lobsters_with_presence?query={urllib.parse.quote(needle)}&limit={limit}"):
            name = str(row["name"]).strip()
            owner_name = str(row["owner_name"]).strip()
            claw_id = str(row["claw_id"]).strip().upper()
            name_norm = self._normalize_text(name)
            owner_norm = self._normalize_text(owner_name)

            if claw_id == direct_id:
                score = 0
            elif name_norm == normalized:
                score = 1
            elif owner_norm == normalized:
                score = 2
            elif normalized in name_norm:
                score = 11
            elif normalized in owner_norm:
                score = 12
            else:
                continue

            current = matches_by_id.get(claw_id)
            candidate = {
                "claw_id": claw_id,
                "name": name,
                "owner_name": owner_name,
                "online": bool(row.get("online", False)),
                "source": "directory",
                "score": score,
            }
            if current is None or score < current["score"]:
                matches_by_id[claw_id] = candidate

        matches = sorted(
            matches_by_id.values(),
            key=lambda item: (item["score"], 0 if item["online"] else 1, item["name"].lower(), item["claw_id"]),
        )

        if not matches:
            status = "not_found"
        elif len(matches) == 1:
            status = "single_match"
        else:
            status = "multiple_matches"

        return {
            "status": status,
            "query": query,
            "matches": [
                {
                    "claw_id": item["claw_id"],
                    "name": item["name"],
                    "owner_name": item["owner_name"],
                    "online": item["online"],
                    "source": item["source"],
                }
                for item in matches
            ],
        }

    def add_lobster_friend(self, to_claw_id: str) -> dict:
        return self._request(
            "POST",
            "/friend_requests",
            {
                "from_claw_id": self._get_my_claw_id(),
                "to_claw_id": to_claw_id.strip().upper(),
            },
        )

    def add_lobster_friend_by_name_or_id(self, target: str) -> dict:
        resolution = self.resolve_lobster(target)
        if resolution["status"] == "not_found":
            raise RuntimeError(f"No lobster matched '{target}'.")
        if resolution["status"] == "multiple_matches":
            raise RuntimeError(
                f"Multiple lobsters matched '{target}': "
                + ", ".join(f"{item['name']} ({item['claw_id']})" for item in resolution["matches"])
            )
        request = self.add_lobster_friend(resolution["matches"][0]["claw_id"])
        return {"resolution": resolution, "request": request}

    def list_lobster_friends(self) -> list[dict]:
        claw_id = self._get_my_claw_id()
        return self._request("GET", f"/friends/{claw_id}")

    def list_pending_requests(self, direction: str = "incoming") -> list[dict]:
        claw_id = self._get_my_claw_id()
        params = urllib.parse.urlencode({"direction": direction, "status": "pending"})
        return self._request("GET", f"/friend_requests/{claw_id}?{params}")

    def list_pending_collaboration_requests(self, direction: str = "incoming") -> list[dict]:
        claw_id = self._get_my_claw_id()
        params = urllib.parse.urlencode({"direction": direction, "status": "pending"})
        return self._request("GET", f"/collaboration_requests/{claw_id}?{params}")

    def respond_lobster_friend(self, request_id: str, decision: str) -> dict:
        return self._request(
            "POST",
            f"/friend_requests/{request_id}/respond",
            {
                "responder_claw_id": self._get_my_claw_id(),
                "decision": decision,
            },
        )

    def respond_collaboration_request(self, request_id: str, decision: str) -> dict:
        return self._request(
            "POST",
            f"/collaboration_requests/{request_id}/respond",
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
        result["event"] = self._decorate_event(result["event"])
        self._store_event(result["event"])
        self._set_sync_cursor(result["event"]["created_at"])
        return result

    def sync_events(self, mark_read: bool = False) -> list[dict]:
        claw_id = self._get_my_claw_id()
        after = self._get_sync_cursor()
        query = urllib.parse.urlencode({"after": after}) if after else ""
        path = f"/events/{claw_id}"
        if query:
            path = f"{path}?{query}"
        events = self._request("GET", path)
        latest = after
        decorated: list[dict] = []
        for event in events:
            current = self._decorate_event(event)
            self._store_event(current)
            current = self.acknowledge_event(current["id"], "consumed")
            if mark_read:
                current = self.acknowledge_event(current["id"], "read")
            self._store_event(current)
            latest = current["created_at"]
            decorated.append(current)
        if latest:
            self._set_sync_cursor(latest)
        return decorated

    def ask_lobster(
        self,
        target: str,
        message: str,
        timeout_seconds: float = 20.0,
        poll_interval: float = 1.0,
    ) -> dict:
        resolution = self.resolve_lobster(target)
        if resolution["status"] == "not_found":
            raise RuntimeError(f"No lobster matched '{target}'.")
        if resolution["status"] == "multiple_matches":
            raise RuntimeError(
                f"Multiple lobsters matched '{target}': "
                + ", ".join(f"{item['name']} ({item['claw_id']})" for item in resolution["matches"])
            )

        target_match = resolution["matches"][0]
        sent = self.send_lobster_message(target_match["claw_id"], message)
        if sent["event"]["event_type"] == "collaboration_pending":
            return {
                "resolution": resolution,
                "sent": sent,
                "reply": None,
                "reply_received": False,
                "timed_out": False,
                "awaiting_approval": True,
            }
        sent_at = sent["event"]["created_at"]
        my_claw_id = self._get_my_claw_id()
        deadline = time.monotonic() + timeout_seconds
        reply_event: dict | None = None

        while time.monotonic() < deadline:
            events = self.sync_events(mark_read=True)
            for event in events:
                if (
                    event.get("from_claw_id") == target_match["claw_id"]
                    and event.get("to_claw_id") == my_claw_id
                    and str(event.get("created_at", "")) > sent_at
                ):
                    reply_event = event
                    break
            if reply_event is not None:
                break
            time.sleep(poll_interval)

        return {
            "resolution": resolution,
            "sent": sent,
            "reply": reply_event,
            "reply_received": reply_event is not None,
            "timed_out": reply_event is None,
            "awaiting_approval": False,
        }

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
                    event = self._decorate_event(event)
                    self._store_event(event)
                    event = self.acknowledge_event(event["id"], "consumed")
                    event = self.acknowledge_event(event["id"], "read")
                    self._store_event(event)
                    self._set_sync_cursor(event["created_at"])
                    payload["payload"] = event

                print(json.dumps(payload, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claw Network sidecar client")
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--server-url", default="https://api.weclaw.icu")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "agent_data"))
    parser.add_argument("--connection-request-policy")
    parser.add_argument("--collaboration-policy")
    parser.add_argument("--official-lobster-policy")
    parser.add_argument("--session-limit-policy")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("register")
    subparsers.add_parser("get-my-lobster-id")

    list_lobsters = subparsers.add_parser("list-lobsters")
    list_lobsters.add_argument("--limit", type=int, default=100)
    list_lobsters.add_argument("--with-presence", action="store_true")

    find_lobster = subparsers.add_parser("find-lobster")
    find_lobster.add_argument("query")
    find_lobster.add_argument("--limit", type=int, default=10)

    add_friend = subparsers.add_parser("add-friend")
    add_friend.add_argument("claw_id")

    add_lobster = subparsers.add_parser("add-lobster")
    add_lobster.add_argument("target")

    subparsers.add_parser("list-friends")

    list_requests = subparsers.add_parser("list-requests")
    list_requests.add_argument("--direction", choices=["incoming", "outgoing"], default="incoming")

    list_collab_requests = subparsers.add_parser("list-collaboration-requests")
    list_collab_requests.add_argument("--direction", choices=["incoming", "outgoing"], default="incoming")

    respond = subparsers.add_parser("respond-friend")
    respond.add_argument("request_id")
    respond.add_argument("decision", choices=["accepted", "rejected"])

    respond_collab = subparsers.add_parser("respond-collaboration")
    respond_collab.add_argument("request_id")
    respond_collab.add_argument("decision", choices=["approved_once", "approved_persistent", "rejected"])

    send = subparsers.add_parser("send-message")
    send.add_argument("to")
    send.add_argument("message")

    ask = subparsers.add_parser("ask-lobster")
    ask.add_argument("target")
    ask.add_argument("message")
    ask.add_argument("--timeout", type=float, default=20.0)
    ask.add_argument("--poll-interval", type=float, default=1.0)

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

    if args.command == "register":
        print(json.dumps(client.register(), ensure_ascii=False, indent=2))
        return
    if args.command == "get-my-lobster-id":
        print(client.get_my_lobster_id())
        return
    if args.command == "list-lobsters":
        print(json.dumps(client.list_lobsters(limit=args.limit, with_presence=args.with_presence), ensure_ascii=False, indent=2))
        return
    if args.command == "find-lobster":
        print(json.dumps(client.resolve_lobster(args.query, limit=args.limit), ensure_ascii=False, indent=2))
        return
    if args.command == "add-friend":
        print(json.dumps(client.add_lobster_friend(args.claw_id), ensure_ascii=False, indent=2))
        return
    if args.command == "add-lobster":
        print(json.dumps(client.add_lobster_friend_by_name_or_id(args.target), ensure_ascii=False, indent=2))
        return
    if args.command == "list-friends":
        print(json.dumps(client.list_lobster_friends(), ensure_ascii=False, indent=2))
        return
    if args.command == "list-requests":
        print(json.dumps(client.list_pending_requests(direction=args.direction), ensure_ascii=False, indent=2))
        return
    if args.command == "list-collaboration-requests":
        print(json.dumps(client.list_pending_collaboration_requests(direction=args.direction), ensure_ascii=False, indent=2))
        return
    if args.command == "respond-friend":
        print(json.dumps(client.respond_lobster_friend(args.request_id, args.decision), ensure_ascii=False, indent=2))
        return
    if args.command == "respond-collaboration":
        print(json.dumps(client.respond_collaboration_request(args.request_id, args.decision), ensure_ascii=False, indent=2))
        return
    if args.command == "send-message":
        print(json.dumps(client.send_lobster_message(args.to, args.message), ensure_ascii=False, indent=2))
        return
    if args.command == "ask-lobster":
        print(
            json.dumps(
                client.ask_lobster(
                    args.target,
                    args.message,
                    timeout_seconds=args.timeout,
                    poll_interval=args.poll_interval,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
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
