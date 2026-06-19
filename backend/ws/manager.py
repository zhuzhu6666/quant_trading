"""Per-connection WebSocket manager with room-based broadcasting."""
import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    def __init__(self) -> None:
        # channel name → set of websockets
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, channel: str, subprotocol: str | None = None) -> None:
        """Accept WS connection and join channel.

        Args:
            ws: WebSocket connection.
            channel: Room name to join.
            subprotocol: Echo back the client's subprotocol (browser WebSocket API
                         requires the server to respond with the same subprotocol
                         from Sec-WebSocket-Protocol, otherwise the connection is
                         rejected as 403).
        """
        await ws.accept(subprotocol=subprotocol)
        async with self._lock:
            self._rooms[channel].add(ws)
        logger.debug(f"ws connected to {channel} (total={len(self._rooms[channel])})")

    async def disconnect(self, ws: WebSocket, channel: str) -> None:
        async with self._lock:
            self._rooms[channel].discard(ws)
            if not self._rooms[channel]:
                del self._rooms[channel]
        logger.debug(f"ws disconnected from {channel}")

    async def broadcast(self, channel: str, payload: dict[str, Any]) -> None:
        """Send payload (JSON-serialized) to all sockets in channel."""
        async with self._lock:
            sockets = list(self._rooms.get(channel, set()))
        if not sockets:
            return
        msg = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    for ch in self._rooms:
                        self._rooms[ch].discard(ws)

    def room_size(self, channel: str) -> int:
        return len(self._rooms.get(channel, set()))


_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
