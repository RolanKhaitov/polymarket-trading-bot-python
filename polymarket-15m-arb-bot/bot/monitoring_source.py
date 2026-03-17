"""
Data source factory — выбор и проверка источника рыночных данных.

Поддерживаемые режимы (DATA_SOURCE в .env):
    direct      — напрямую из Polymarket (Gamma API + CLOB WebSocket). По умолчанию.
    monitoring  — из внешнего monitoring-сервиса (Gamma-совместимый REST + WS).
    auto        — пробуем monitoring при старте; если недоступен — падаем на direct.

Monitoring-сервис должен:
    - Предоставлять Gamma-совместимый REST-endpoint (MONITORING_API_URL)
      Пример: GET /events?active=true → тот же формат что gamma-api.polymarket.com
    - Предоставлять WebSocket в формате Polymarket price_change/book событий
      Пример: wss://monitoring.internal/ws/market

Дизайн: оба источника используют те же GammaClient + WebSocketScanner,
только с разными URL через dataclasses.replace(). Никакой дополнительной логики.
"""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import aiohttp

from .gamma import GammaClient
from .ws_scanner import PriceUpdateCallback, WebSocketScanner

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

# Таймаут пробы в секундах (дополнительно к config.monitoring_probe_timeout)
_PROBE_CONNECT_TIMEOUT = 2.0


async def probe_monitoring(config: "Config") -> bool:
    """
    Проверить, доступен ли monitoring-сервис.

    Делает GET {monitoring_gamma_url}/events?limit=1 с коротким таймаутом.
    Возвращает True если сервис ответил (любой HTTP статус < 500).
    Возвращает False если URL не задан, сервис не отвечает или ошибка.
    """
    url = (config.monitoring_gamma_url or "").strip()
    if not url:
        log.debug("Monitoring probe: MONITORING_API_URL not set")
        return False

    probe_url = url.rstrip("/") + "/markets"
    timeout = aiohttp.ClientTimeout(
        total=config.monitoring_probe_timeout,
        connect=_PROBE_CONNECT_TIMEOUT,
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(probe_url, timeout=timeout, params={"limit": "1"}) as resp:
                ok = resp.status < 500
                log.info(
                    "Monitoring probe %s → HTTP %d (%s)",
                    probe_url, resp.status, "OK" if ok else "FAIL",
                )
                return ok
    except Exception as exc:
        log.info("Monitoring probe failed (%s): %s", probe_url, exc)
        return False


def build_direct_components(
    config: "Config",
    on_price_update: PriceUpdateCallback,
) -> tuple[GammaClient, WebSocketScanner]:
    """
    Создать компоненты данных, подключённые напрямую к Polymarket.

    Использует:
        gamma_url  = https://gamma-api.polymarket.com
        ws_url     = wss://ws-subscriptions-clob.polymarket.com/ws/market
    """
    return GammaClient(config), WebSocketScanner(config, on_price_update)


def build_monitoring_components(
    config: "Config",
    on_price_update: PriceUpdateCallback,
) -> tuple[GammaClient, WebSocketScanner]:
    """
    Создать компоненты данных, подключённые к monitoring-сервису.

    Использует dataclasses.replace() для подмены URL в конфиге —
    GammaClient и WebSocketScanner не знают, что работают не с Polymarket.

    Raises:
        ValueError если monitoring URL-ы не заданы.
    """
    gamma_url = (config.monitoring_gamma_url or "").strip()
    ws_url    = (config.monitoring_ws_url    or "").strip()

    if not gamma_url:
        raise ValueError(
            "MONITORING_API_URL is required for DATA_SOURCE=monitoring.\n"
            "Example: MONITORING_API_URL=http://localhost:8000"
        )
    if not ws_url:
        raise ValueError(
            "MONITORING_WS_URL is required for DATA_SOURCE=monitoring.\n"
            "Example: MONITORING_WS_URL=ws://localhost:8000/ws"
        )

    # Копия конфига с подменёнными URL — оригинал не мутируется
    mon_cfg = replace(config, gamma_url=gamma_url, ws_url=ws_url)
    log.debug(
        "Building monitoring components: gamma=%s  ws=%s",
        gamma_url, ws_url,
    )
    return GammaClient(mon_cfg), WebSocketScanner(mon_cfg, on_price_update)
