"""Per-connection WebSocket manager with room-based broadcasting."""
import asyncio
import json
import threading
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    def __init__(self) -> None:
        # channel name → set of websockets
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # State writers run in the serial live-loop thread while WebSocket
        # handlers run on the ASGI event loop.  A generation counter plus an
        # event per event loop bridges those two domains without making the
        # WebSocket endpoint poll the live state.
        self._generation_lock = threading.Lock()
        self._generations: dict[str, int] = defaultdict(int)
        self._events: dict[str, dict[asyncio.AbstractEventLoop, asyncio.Event]] = defaultdict(dict)
        self._socket_loops: dict[int, tuple[str, asyncio.AbstractEventLoop]] = {}

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
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._rooms[channel].add(ws)
            self._socket_loops[id(ws)] = (channel, loop)
            self._events[channel].setdefault(loop, asyncio.Event())
        logger.debug(f"ws connected to {channel} (total={len(self._rooms[channel])})")

    async def disconnect(self, ws: WebSocket, channel: str) -> None:
        async with self._lock:
            self._rooms[channel].discard(ws)
            connection = self._socket_loops.pop(id(ws), None)
            if not self._rooms[channel]:
                del self._rooms[channel]
                self._events.pop(channel, None)
            elif connection is not None:
                _, loop = connection
                if not any(
                    item_channel == channel and item_loop is loop
                    for item_channel, item_loop in self._socket_loops.values()
                ):
                    self._events[channel].pop(loop, None)
        logger.debug(f"ws disconnected from {channel}")

    def current_generation(self, channel: str) -> int:
        with self._generation_lock:
            return int(self._generations.get(channel, 0))

    def notify(self, channel: str) -> int:
        """Notify connected clients that a canonical channel fact changed.

        This method is safe to call from the live-loop thread.  It only wakes
        waiting WebSocket handlers; the handler reads one complete snapshot
        after the notification, so API serialization remains read-only.
        """
        with self._generation_lock:
            self._generations[channel] = int(self._generations.get(channel, 0)) + 1
            generation = self._generations[channel]
            events = list(self._events.get(channel, {}).items())
        for loop, event in events:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # The client loop may have been torn down between the check
                # and scheduling.  The next connection starts from the
                # current generation and does not need this stale wake-up.
                continue
        return generation

    async def wait_for_change(self, channel: str, after_generation: int) -> int:
        """Wait until ``channel`` has a generation newer than the snapshot."""
        loop = asyncio.get_running_loop()
        while True:
            current = self.current_generation(channel)
            if current > after_generation:
                return current
            async with self._lock:
                event = self._events[channel].get(loop)
                if event is None:
                    event = asyncio.Event()
                    self._events[channel][loop] = event
                event.clear()
            # Close the race where a writer notified between the first check
            # and event.clear().
            current = self.current_generation(channel)
            if current > after_generation:
                return current
            await event.wait()

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
