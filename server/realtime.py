from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, agent_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            existing = self._connections.get(agent_id)
            if existing is not None:
                await existing.close(code=1012, reason="Replaced by a new connection.")
            self._connections[agent_id] = websocket

    async def disconnect(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            current = self._connections.get(agent_id)
            if current is websocket:
                self._connections.pop(agent_id, None)

    async def send_to_agent(self, agent_id: str, payload: dict[str, Any]) -> bool:
        async with self._lock:
            websocket = self._connections.get(agent_id)
        if websocket is None:
            return False
        await websocket.send_json(payload)
        return True

    async def list_online(self) -> list[str]:
        async with self._lock:
            return sorted(self._connections.keys())


manager = ConnectionManager()
