"""
WebSocket broadcaster — manages connected bot clients and relays messages.

Design:
- Monitoring service connects to Polymarket WS (upstream)
- Bot clients connect to monitoring service WS (downstream)
- Every message received from Polymarket is broadcast to all connected bots
- Per-client filtering is NOT done here — the bot's WebSocketScanner filters by token_id
"""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class Broadcaster:
    """Manages a set of connected WebSocket clients and broadcasts messages to all."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()

    def register(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)
        log.info("[BCAST] Bot client connected. Active clients: %d", len(self._clients))

    def unregister(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)
        log.info("[BCAST] Bot client disconnected. Active clients: %d", len(self._clients))

    async def broadcast(self, message: str) -> int:
        """
        Send message to all connected clients.
        Returns number of clients successfully sent to.
        Removes dead connections silently.
        """
        if not self._clients:
            return 0

        sent = 0
        dead: set[web.WebSocketResponse] = set()

        for ws in self._clients:
            try:
                if not ws.closed:
                    await ws.send_str(message)
                    sent += 1
                else:
                    dead.add(ws)
            except Exception as e:
                log.debug("[BCAST] Send failed to client: %s", e)
                dead.add(ws)

        for ws in dead:
            self._clients.discard(ws)

        return sent

    @property
    def client_count(self) -> int:
        return len(self._clients)


# Global singleton
broadcaster = Broadcaster()
