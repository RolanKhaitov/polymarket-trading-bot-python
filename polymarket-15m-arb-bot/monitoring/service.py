"""
Monitoring Service — aiohttp HTTP + WebSocket server.

Endpoints:
    GET  /health   → service health + counters
    GET  /markets  → cached market list (Gamma API compatible format)
    GET  /stats    → detailed metrics
    WS   /ws       → WebSocket relay endpoint for bot clients

Data flow:
    Polymarket Gamma REST → /markets (cached, refreshed every 60s)
    Polymarket CLOB WS   → /ws      (relayed verbatim to connected bots)

The bot's GammaClient + WebSocketScanner connect here instead of Polymarket directly.
Format is IDENTICAL to Polymarket native API — no translation needed.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .broadcaster import broadcaster
from .polymarket_client import fetch_all_markets, market_refresh_loop, ws_relay_loop
from .state import state

log = logging.getLogger(__name__)


# ── HTTP handlers ─────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    uptime = int((datetime.now(timezone.utc) - state.started_at).total_seconds())
    data = {
        "status":           "ok",
        "markets_cached":   len(state.markets_raw),
        "ws_to_polymarket": state.ws_connected,
        "ws_clients":       broadcaster.client_count,
        "messages_relayed": state.ws_messages_relayed,
        "uptime_seconds":   uptime,
    }
    return web.json_response(data)


async def handle_markets(request: web.Request) -> web.Response:
    """
    Return cached market list in Gamma API compatible format.

    Supports query params used by bot's GammaClient:
        active=true / closed=false  → filter by field
        limit=N / offset=N          → pagination
    Other params (order, ascending) are accepted but ignored
    (data is already sorted newest-first from initial fetch).
    """
    markets = list(state.markets_raw)

    # Apply simple filters
    active_filter = request.rel_url.query.get("active", "").lower()
    closed_filter = request.rel_url.query.get("closed", "").lower()

    if active_filter == "true":
        markets = [m for m in markets if m.get("active", True)]
    elif active_filter == "false":
        markets = [m for m in markets if not m.get("active", True)]

    if closed_filter == "false":
        markets = [m for m in markets if not m.get("closed", False)]
    elif closed_filter == "true":
        markets = [m for m in markets if m.get("closed", False)]

    # Pagination
    try:
        total  = len(markets)
        offset = int(request.rel_url.query.get("offset", 0))
        limit  = int(request.rel_url.query.get("limit",  total))
        markets = markets[offset : offset + limit]
    except (ValueError, TypeError):
        pass

    log.debug("[REST] GET /markets -> %d items (cache=%d)", len(markets), len(state.markets_raw))
    return web.json_response(markets)


async def handle_stats(request: web.Request) -> web.Response:
    uptime = int((datetime.now(timezone.utc) - state.started_at).total_seconds())
    data = {
        "markets_cached":            len(state.markets_raw),
        "markets_loaded_at":         state.markets_loaded_at.isoformat() if state.markets_loaded_at else None,
        "ws_connected_to_polymarket": state.ws_connected,
        "ws_reconnect_count":        state.ws_reconnect_count,
        "ws_messages_received":      state.ws_messages_received,
        "ws_messages_relayed":       state.ws_messages_relayed,
        "ws_clients_connected":      broadcaster.client_count,
        "ws_clients_total_sessions": state.ws_clients_total,
        "last_error":                state.last_error,
        "last_rest_error":           state.last_rest_error,
        "uptime_seconds":            uptime,
    }
    return web.json_response(data)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """
    WebSocket endpoint for bot clients.

    Protocol:
    - Client connects
    - Client sends subscription: {"assets_ids": [...], "type": "Market"}
      (we accept it but broadcast everything regardless — bot filters locally)
    - We relay every Polymarket WS event to client
    - Client disconnects cleanly or we detect dead connection
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    broadcaster.register(ws)
    state.ws_clients_total += 1
    client_ip = request.remote or "unknown"
    log.info("[WS SERVER] Bot connected from %s (total sessions: %d)",
             client_ip, state.ws_clients_total)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                log.debug("[WS SERVER] Subscription from bot: %.150s", msg.data[:150])
                # Accept subscription silently — we broadcast all tokens anyway
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("[WS SERVER] Client error: %s", ws.exception())
                break
    finally:
        broadcaster.unregister(ws)
        log.info("[WS SERVER] Bot disconnected from %s", client_ip)

    return ws


# ── App lifecycle ─────────────────────────────────────────────────────────────

async def _initial_load(session: aiohttp.ClientSession) -> None:
    """Background task: load initial markets then hand off to refresh loop."""
    log.info("[STARTUP] Loading initial markets from Polymarket (background)...")
    try:
        markets = await fetch_all_markets(session)
        if markets:
            state.markets_raw       = markets
            state.markets_loaded_at = datetime.now(timezone.utc)
            log.info("[STARTUP] Initial load complete: %d markets cached", len(markets))
        else:
            log.warning("[STARTUP] Initial load returned 0 markets — "
                        "service will retry in %ds via refresh loop",
                        int(os.getenv("MARKET_REFRESH_INTERVAL", "60")))
    except Exception as exc:
        log.error("[STARTUP] Initial market load failed: %s", exc)
        state.last_rest_error = str(exc)


async def _on_startup(app: web.Application) -> None:
    """Start background tasks — HTTP server binds to port immediately."""
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=20),
    )
    app["http_session"] = session

    # All I/O is non-blocking — server accepts connections right away
    app["task_initial"] = asyncio.create_task(_initial_load(session))
    app["task_refresh"] = asyncio.create_task(market_refresh_loop(session))
    app["task_ws_relay"] = asyncio.create_task(ws_relay_loop(session))
    log.info("[STARTUP] Background tasks started (initial load + refresh + WS relay)")


async def _on_cleanup(app: web.Application) -> None:
    """Cancel background tasks and close HTTP session."""
    log.info("[SHUTDOWN] Stopping background tasks...")
    for key in ("task_initial", "task_refresh", "task_ws_relay"):
        task = app.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    session = app.get("http_session")
    if session and not session.closed:
        await session.close()

    log.info("[SHUTDOWN] Monitoring service stopped.")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/health",  handle_health)
    app.router.add_get("/markets", handle_markets)
    app.router.add_get("/stats",   handle_stats)
    app.router.add_get("/ws",      handle_ws)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    return app


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the monitoring service (blocking, runs until Ctrl-C)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    log.info("=" * 62)
    log.info("  Polymarket Monitoring Service")
    log.info("  REST:    http://%s:%d", host, port)
    log.info("  WS:      ws://%s:%d/ws", host, port)
    log.info("=" * 62)

    asyncio.run(_run_forever(host, port))


async def _run_forever(host: str, port: int) -> None:
    """
    AppRunner/TCPSite variant of web.run_app.
    Prints [READY] exactly after the TCP socket is bound.
    """
    app = create_app()
    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError:
        # Let run_monitoring.py catch and report this
        await runner.cleanup()
        raise

    # ── Port is bound — service is reachable NOW ───────────────────────────
    print(f"\n[READY] Monitoring service listening at http://localhost:{port}", flush=True)
    print(f"[READY]   GET  http://localhost:{port}/health   -> status", flush=True)
    print(f"[READY]   GET  http://localhost:{port}/markets  -> cached markets", flush=True)
    print(f"[READY]   GET  http://localhost:{port}/stats    -> metrics", flush=True)
    print(f"[READY]   WS   ws://localhost:{port}/ws         -> price relay", flush=True)
    print(f"[READY] Markets load in background. Press Ctrl+C to stop.\n", flush=True)

    # Block until cancelled (Ctrl-C or SIGTERM)
    try:
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        log.info("[SHUTDOWN] Cleaning up...")
        await runner.cleanup()
