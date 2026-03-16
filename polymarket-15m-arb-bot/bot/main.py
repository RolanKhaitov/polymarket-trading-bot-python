"""Главная оркестрация бота."""

import asyncio
import logging
from datetime import datetime, timezone

from .analyzer import ArbitrageAnalyzer
from .config import Config
from .executor import create_executor
from .gamma import GammaClient
from .models import MarketPrices
from .risk import RiskManager
from .state import BotState, RecentTrade, bot_state
from .ws_scanner import WebSocketScanner

log = logging.getLogger(__name__)


class ArbitrageBot:
    """
    15-минутный арбитражный бот для Polymarket.

    Алгоритм:
    1. Загрузить активные "Up or Down" рынки через Gamma API
    2. Подписаться на WebSocket для получения цен в реальном времени
    3. При каждом обновлении цены → проверить арбитраж (YES + NO < $1)
    4. Если найден → проверить риски → исполнить (dry-run или live)
    5. Каждые market_refresh_interval секунд — обновлять список рынков
    """

    def __init__(self, config: Config, state: BotState = bot_state):
        self.config = config
        self.state = state
        self.gamma = GammaClient(config)
        self.analyzer = ArbitrageAnalyzer(config)
        self.executor = create_executor(config)
        self.risk = RiskManager(config)
        self.scanner = WebSocketScanner(config, on_price_update=self._on_price_update)

        self._running = False
        self._market_refresh_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._started_at = datetime.now(timezone.utc)

        # Инициализируем state
        self.state.mode = "DRY RUN" if config.dry_run else "LIVE"
        self.state.min_profit_pct = config.min_profit_pct
        self.state.max_position_size = config.max_position_size
        self.state.min_seconds_to_expiry = config.min_seconds_to_expiry
        self.state.max_seconds_to_expiry = config.max_seconds_to_expiry

    async def start(self) -> None:
        """Запустить бота."""
        self._running = True
        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)

        mode = self.state.mode
        log.info(
            "=" * 60 + "\n"
            "  Polymarket 15-min Arbitrage Bot\n"
            "  Mode: %s | Min profit: %.1f%% | Max pos: $%.0f\n"
            + "=" * 60,
            mode,
            self.config.min_profit_pct * 100,
            self.config.max_position_size,
        )

        await self._refresh_markets()

        if not self.scanner.get_token_ids():
            log.warning("No markets found — will retry in %ds.", self.config.market_refresh_interval)

        self._market_refresh_task = asyncio.create_task(self._market_refresh_loop())
        self._stats_task = asyncio.create_task(self._state_sync_loop())

        try:
            await self.scanner.run()
        except asyncio.CancelledError:
            log.info("Bot cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Остановить бота."""
        self._running = False
        self.state.running = False
        self.scanner.stop()

        for task in (self._market_refresh_task, self._stats_task):
            if task and not task.done():
                task.cancel()

        await self.gamma.close()
        self._log_final_stats()

    async def _on_price_update(self, prices: MarketPrices) -> None:
        """Горячий путь: обработка каждого ценового обновления."""
        # Обновляем state для дашборда
        self.state.update_market_price(
            market_id=prices.market.id,
            question=prices.market.question,
            yes_ask=prices.yes_best_ask,
            no_ask=prices.no_best_ask,
            seconds_left=prices.market.seconds_to_expiry,
        )

        # Анализ на арбитраж
        opp = self.analyzer.analyze(prices)
        if opp is None:
            return

        self.state.opportunities_found += 1

        # Проверка рисков
        allowed, reason = self.risk.can_trade(prices.market.id)
        if not allowed:
            log.debug("Risk block [%s]: %s", prices.market.question[:30], reason)
            return

        # Исполнить
        self.risk.open_position(prices.market.id)
        try:
            result = await self.executor.execute(opp)

            profit = result.actual_profit or opp.estimated_profit
            self.risk.close_position(prices.market.id, profit)

            # Записываем в state
            self.state.add_trade(RecentTrade(
                market=opp.market.question[:45],
                yes_ask=opp.yes_ask,
                no_ask=opp.no_ask,
                combined=opp.combined_cost,
                profit_pct=opp.gross_profit_pct,
                profit_usd=profit,
                trade_size=opp.trade_size,
                dry_run=self.config.dry_run,
            ))

        except Exception as e:
            log.error("Execution error: %s", e)
            self.risk.close_position(prices.market.id, 0.0)

    async def _refresh_markets(self) -> None:
        try:
            markets = await self.gamma.fetch_updown_markets()
            self.scanner.load_markets(markets)
            self.state.markets_loaded = len(markets)
            self.state.tokens_subscribed = len(self.scanner.get_token_ids())
            log.info("Markets refreshed: %d markets / %d tokens",
                     len(markets), self.state.tokens_subscribed)
        except Exception as e:
            log.error("Failed to refresh markets: %s", e)

    async def _market_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.config.market_refresh_interval)
            if self._running:
                await self._refresh_markets()

    async def _state_sync_loop(self) -> None:
        """Синхронизировать вторичные поля state каждую секунду."""
        while self._running:
            await asyncio.sleep(1)
            risk = self.risk.get_stats()
            analyzer = self.analyzer.get_stats()
            self.state.analyzed = analyzer["analyzed"]
            self.state.daily_pnl = risk["daily_pnl"]
            self.state.total_pnl = risk["total_profit"]
            self.state.ws_last_msg_sec = self.scanner.seconds_since_last_message
            self.state.ws_connected = self.state.ws_last_msg_sec < 30

    def _log_final_stats(self) -> None:
        risk = self.risk.get_stats()
        log.info(
            "=" * 60 + "\n"
            "  FINAL: Trades=%d  Win=%.0f%%  PnL=$%.2f%s\n"
            + "=" * 60,
            risk["total_trades"],
            risk["win_rate"],
            risk["total_profit"],
            " (simulated)" if self.config.dry_run else "",
        )
