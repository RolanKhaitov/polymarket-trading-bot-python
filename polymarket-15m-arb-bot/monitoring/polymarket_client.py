"""
Polymarket upstream client for the monitoring service.

Responsibilities:
1. Fetch market list from Polymarket Gamma API → cache in state
2. Connect to Polymarket CLOB WebSocket → subscribe to all token IDs
3. Relay every WS message verbatim to all connected bot clients via broadcaster

The message format is NOT modified — bot's GammaClient + WebSocketScanner
can parse Polymarket's native format directly.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .broadcaster import broadcaster
from .state import state

log = logging.getLogger(__name__)

# ── Upstream Polymarket endpoints ─────────────────────────────────────────────
GAMMA_URL   = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_WS_URL = os.getenv("POLYMARKET_WS_URL",   "wss://ws-subscriptions-clob.polymarket.com/ws/market")

MARKET_REFRESH_INTERVAL = int(os.getenv("MARKET_REFRESH_INTERVAL", "60"))
MAX_ASSETS_PER_BATCH    = 500

# ── REST: market discovery ─────────────────────────────────────────────────────

async def fetch_all_markets(
    session: aiohttp.ClientSession,
    max_pages: int = 3,
) -> list:
    """
    Fetch active markets from Polymarket Gamma API (newest-first).

    max_pages limits how many 500-item pages are fetched (default 3 = 1500 markets).
    Sorted by startDate desc so most recent / active markets are always included.
    The bot's own GammaClient applies additional filters after receiving the list.
    """
    all_markets: list = []
    offset       = 0
    limit        = 500
    url          = f"{GAMMA_URL}/markets"
    page_num     = 0

    log.info("[REST] Fetching markets from %s (max_pages=%d)", url, max_pages)

    while True:
        if page_num >= max_pages:
            log.info("[REST] Reached max_pages=%d limit — stopping fetch (%d markets)",
                     max_pages, len(all_markets))
            break

        params = {
            "active":    "true",
            "closed":    "false",
            "limit":     limit,
            "offset":    offset,
            "order":     "startDate",
            "ascending": "false",
        }
        try:
            async with session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.error("[REST] /markets HTTP %d — aborting fetch", resp.status)
                    state.last_rest_error = f"HTTP {resp.status}"
                    break

                raw  = await resp.json(content_type=None)
                page: list = raw if isinstance(raw, list) else (raw or {}).get("markets", [])

                if not page:
                    break

                all_markets.extend(page)
                page_num += 1
                log.info("[REST] Page %d offset=%d: got %d markets (total so far: %d)",
                         page_num, offset, len(page), len(all_markets))

                if len(page) < limit:
                    break

                offset += limit

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[REST] Market fetch error: %s", exc)
            state.last_rest_error = str(exc)
            break

    return all_markets


def _extract_token_ids(markets: list) -> list[str]:
    """Extract all clobTokenIds from a market list."""
    token_ids: list[str] = []
    for m in markets:
        raw = m.get("clobTokenIds") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                raw = []
        if isinstance(raw, list):
            token_ids.extend(str(t) for t in raw if t)
    return token_ids


# ── Background task: periodic market refresh ──────────────────────────────────

async def market_refresh_loop(session: aiohttp.ClientSession) -> None:
    """
    Periodically refresh market list.
    First iteration is delayed by MARKET_REFRESH_INTERVAL (initial load done at startup).
    """
    while True:
        await asyncio.sleep(MARKET_REFRESH_INTERVAL)
        log.info("[REST] Refreshing markets (interval=%ds)...", MARKET_REFRESH_INTERVAL)
        try:
            markets = await fetch_all_markets(session)
            if markets:
                state.markets_raw       = markets
                state.markets_loaded_at = datetime.now(timezone.utc)
                log.info("[REST] Refreshed: %d markets cached", len(markets))
            else:
                log.warning("[REST] Refresh returned 0 markets — keeping old cache (%d)", len(state.markets_raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[REST] Refresh error: %s", exc)
            state.last_rest_error = str(exc)


# ── Background task: Polymarket WS relay ──────────────────────────────────────

async def ws_relay_loop(session: aiohttp.ClientSession) -> None:
    """
    Connect to Polymarket CLOB WebSocket, subscribe to all active token IDs,
    and relay every price_change / book event to connected bot clients.

    Reconnects automatically on failure with exponential backoff.
    """
    reconnect_delay = 2.0

    while True:
        # Wait until we have markets to subscribe to
        if not state.markets_raw:
            log.info("[WS POLY] Waiting for initial market load...")
            await asyncio.sleep(2)
            continue

        token_ids = _extract_token_ids(state.markets_raw)
        if not token_ids:
            log.warning("[WS POLY] No token IDs in market cache — retrying in 5s")
            await asyncio.sleep(5)
            continue

        log.info("[WS POLY] Connecting to %s (%d tokens)...", CLOB_WS_URL, len(token_ids))

        try:
            async with session.ws_connect(
                CLOB_WS_URL,
                heartbeat=30,
                receive_timeout=120,
            ) as ws:
                state.ws_connected = True
                log.info("[WS POLY] Connected. Subscribing to %d tokens in batches of %d...",
                         len(token_ids), MAX_ASSETS_PER_BATCH)

                # Subscribe in batches (Polymarket limit)
                for i in range(0, len(token_ids), MAX_ASSETS_PER_BATCH):
                    batch = token_ids[i : i + MAX_ASSETS_PER_BATCH]
                    sub   = json.dumps({"assets_ids": batch, "type": "Market"})
                    await ws.send_str(sub)
                    log.info("[WS POLY] Subscribed batch %d–%d (%d tokens)",
                             i + 1, i + len(batch), len(batch))
                    if i + MAX_ASSETS_PER_BATCH < len(token_ids):
                        await asyncio.sleep(1.0)

                reconnect_delay = 2.0   # reset backoff after successful connect
                msg_count       = 0

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        state.ws_messages_received += 1
                        msg_count += 1

                        # Log first 3 messages so we can verify format
                        if msg_count <= 3:
                            log.info("[WS POLY] Message #%d: %.200s", msg_count, msg.data[:200])

                        # Relay verbatim to all connected bot clients
                        sent = await broadcaster.broadcast(msg.data)
                        if sent > 0:
                            state.ws_messages_relayed += 1

                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        log.info("[WS POLY] Connection closed by server")
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning("[WS POLY] Error: %s", ws.exception())
                        break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[WS POLY] Connection failed: %s — retry in %.0fs", exc, reconnect_delay)
            state.last_error = str(exc)
        finally:
            state.ws_connected = False
            state.ws_reconnect_count += 1

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)
