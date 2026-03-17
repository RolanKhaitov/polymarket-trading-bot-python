"""In-memory state of the monitoring service."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class MonitoringState:
    # Cached market list (raw Polymarket Gamma API format)
    markets_raw: list = field(default_factory=list)
    markets_loaded_at: Optional[datetime] = None

    # Polymarket WS connection status
    ws_connected: bool = False
    ws_reconnect_count: int = 0
    ws_messages_received: int = 0
    ws_messages_relayed: int = 0

    # Connected bot clients
    ws_client_count: int = 0
    ws_clients_total: int = 0

    # Errors
    last_error: str = ""
    last_rest_error: str = ""

    # Startup time
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Global singleton — shared across all modules
state = MonitoringState()
