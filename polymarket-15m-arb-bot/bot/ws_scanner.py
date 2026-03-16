"""WebSocket сканер — подписка на цены токенов в реальном времени."""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from .config import Config
from .models import Market, MarketPrices

log = logging.getLogger(__name__)

# Макс. токенов на одно WS-соединение
MAX_ASSETS_PER_CONNECTION = 500

PriceUpdateCallback = Callable[[MarketPrices], Awaitable[None]]


class WebSocketScanner:
    """
    Подписывается на цены YES/NO токенов через Polymarket WebSocket.

    Реальный формат Polymarket WS:
    - book event: {"event_type":"book","asset_id":"...","asks":[{"price":"0.55","size":"200"},...]}
    - price_change event: {
          "event_type":"price_change",
          "market":"0x...",
          "price_changes": [
              {"asset_id":"...","best_ask":"0.55","best_bid":"0.45","price":"0.55","side":"SELL","size":"150"},
              ...
          ]
      }

    Важно: каждый price_changes item содержит актуальные best_ask/best_bid.
    Не нужно поддерживать полный стакан.
    """

    def __init__(self, config: Config, on_price_update: PriceUpdateCallback):
        self.config = config
        self.on_price_update = on_price_update

        self._prices: dict[str, MarketPrices] = {}      # market_id → MarketPrices
        self._token_to_market: dict[str, str] = {}       # token_id → market_id
        self._markets: dict[str, Market] = {}            # market_id → Market

        self._running = False
        self._last_message_time: float = 0.0
        self._message_count: int = 0

    def load_markets(self, markets: list[Market]) -> None:
        """Зарегистрировать рынки для отслеживания."""
        self._markets = {m.id: m for m in markets}
        self._token_to_market = {}
        self._prices = {}

        for market in markets:
            # Пропускаем рынки без валидных token IDs
            if not market.yes_token.token_id or not market.no_token.token_id:
                continue
            self._token_to_market[market.yes_token.token_id] = market.id
            self._token_to_market[market.no_token.token_id] = market.id
            self._prices[market.id] = MarketPrices(market=market)

        token_count = len(self._token_to_market)
        log.info(
            "Scanner loaded %d markets (%d tokens)",
            len(self._markets),
            token_count,
        )

    def get_token_ids(self) -> list[str]:
        return list(self._token_to_market.keys())

    async def run(self) -> None:
        """Запустить сканер с автоматическим переподключением."""
        self._running = True
        session = aiohttp.ClientSession()
        reconnect_delay = 2.0

        try:
            while self._running:
                try:
                    await self._connect_and_listen(session)
                    reconnect_delay = 2.0
                except Exception as e:
                    if not self._running:
                        break
                    log.error(
                        "WebSocket error: %s — reconnecting in %.0fs", e, reconnect_delay
                    )
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
        finally:
            await session.close()

    async def _connect_and_listen(self, session: aiohttp.ClientSession) -> None:
        """Подключиться к WS и слушать сообщения."""
        log.info("Connecting to WebSocket: %s", self.config.ws_url)

        async with session.ws_connect(
            self.config.ws_url,
            heartbeat=30,
            receive_timeout=120,
        ) as ws:
            self._last_message_time = time.monotonic()
            log.info("WebSocket connected")

            # Подписываемся на все токены
            token_ids = self.get_token_ids()
            await self._subscribe(ws, token_ids)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_message_time = time.monotonic()
                    self._message_count += 1
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WebSocket error message: %s", ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    log.info("WebSocket closed")
                    break

    async def _subscribe(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        token_ids: list[str],
    ) -> None:
        """Подписаться на токены батчами."""
        total = len(token_ids)
        for i in range(0, total, MAX_ASSETS_PER_CONNECTION):
            batch = token_ids[i : i + MAX_ASSETS_PER_CONNECTION]
            msg = json.dumps({"assets_ids": batch, "type": "Market"})
            await ws.send_str(msg)
            batch_num = i // MAX_ASSETS_PER_CONNECTION + 1
            total_batches = (total + MAX_ASSETS_PER_CONNECTION - 1) // MAX_ASSETS_PER_CONNECTION
            log.info(
                "Subscribed batch %d/%d (%d tokens)",
                batch_num, total_batches, len(batch),
            )
            if i + MAX_ASSETS_PER_CONNECTION < total:
                await asyncio.sleep(1.0)  # пауза между батчами

    async def _handle_message(self, raw: str) -> None:
        """
        Обработать WS-сообщение.

        Polymarket отправляет:
        - [] (пустой список) = подтверждение подписки
        - [{event_type: "book", ...}] = список событий
        - {event_type: "price_change", ...} = одиночное событие
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            if event_type == "book":
                await self._handle_book(item)
            elif event_type == "price_change":
                await self._handle_price_change(item)

    async def _handle_book(self, data: dict) -> None:
        """
        Обработать снапшот стакана.

        Формат:
        {
          "event_type": "book",
          "asset_id": "...",
          "bids": [{"price": "0.45", "size": "1000"}],
          "asks": [{"price": "0.55", "size": "200"}]
        }
        """
        token_id = data.get("asset_id") or ""
        asks_raw = data.get("asks") or []

        if not token_id:
            return

        best_ask: Optional[float] = None
        best_ask_size: Optional[float] = None

        if asks_raw:
            try:
                # best ask = минимальная цена в asks
                asks = [(float(a["price"]), float(a["size"])) for a in asks_raw if float(a.get("size", 0)) > 0]
                if asks:
                    asks.sort(key=lambda x: x[0])
                    best_ask, best_ask_size = asks[0]
            except (KeyError, ValueError):
                pass

        await self._update_price(token_id, best_ask, best_ask_size)

    async def _handle_price_change(self, data: dict) -> None:
        """
        Обработать изменение цен.

        Формат:
        {
          "event_type": "price_change",
          "market": "0x...",
          "price_changes": [
            {
              "asset_id": "...",
              "best_ask": "0.55",
              "best_bid": "0.45",
              "side": "SELL",
              "price": "0.55",
              "size": "150"
            }
          ]
        }
        """
        price_changes = data.get("price_changes") or []

        for change in price_changes:
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id") or ""
            if not token_id:
                continue

            # Используем best_ask из сообщения (уже посчитано сервером)
            best_ask: Optional[float] = None
            best_ask_size: Optional[float] = None

            try:
                ask_str = change.get("best_ask")
                if ask_str is not None:
                    best_ask = float(ask_str)
                # Для size используем size текущего изменения как приближение
                size_str = change.get("size")
                if size_str is not None:
                    s = float(size_str)
                    if s > 0:
                        best_ask_size = s
            except (ValueError, TypeError):
                continue

            await self._update_price(token_id, best_ask, best_ask_size)

    async def _update_price(
        self,
        token_id: str,
        best_ask: Optional[float],
        best_ask_size: Optional[float],
    ) -> None:
        """Обновить цену для токена и вызвать колбек."""
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        prices = self._prices.get(market_id)
        if not prices:
            return

        is_yes = token_id == prices.market.yes_token.token_id

        if is_yes:
            prices.yes_best_ask = best_ask
            if best_ask_size is not None:
                prices.yes_best_ask_size = best_ask_size
        else:
            prices.no_best_ask = best_ask
            if best_ask_size is not None:
                prices.no_best_ask_size = best_ask_size

        # Вызываем колбек когда есть обе цены
        if prices.yes_best_ask is not None and prices.no_best_ask is not None:
            try:
                await self.on_price_update(prices)
            except Exception as e:
                log.error("Price update callback error: %s", e)

    def stop(self) -> None:
        self._running = False

    @property
    def seconds_since_last_message(self) -> float:
        if self._last_message_time == 0:
            return float("inf")
        return time.monotonic() - self._last_message_time

    @property
    def message_count(self) -> int:
        return self._message_count
