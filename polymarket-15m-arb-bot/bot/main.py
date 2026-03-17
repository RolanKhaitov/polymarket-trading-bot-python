"""Главная оркестрация бота."""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from .analyzer import ArbitrageAnalyzer
from .config import Config
from .data_logger import DataLogger
from .executor import create_executor
from .gamma import GammaClient
from .monitoring_source import build_direct_components, build_monitoring_components, probe_monitoring
from .models import ArbitrageOpportunity, MarketPrices, OrderInfo
from .risk import RiskManager
from .state import BotState, RecentTrade, bot_state
from .ws_scanner import WebSocketScanner

log = logging.getLogger(__name__)


class ArbitrageBot:
    """
    Polymarket бот — стратегия Gabagool (Favourite-Leg).

    Три независимых состояния:
    ┌─────────────────────────────────────────────────────────────────┐
    │  _traded_markets  │ одна попытка подачи ордера на рынок/эпоху  │
    │                   │ не зависит от результата исполнения         │
    ├─────────────────────────────────────────────────────────────────┤
    │  _submitting      │ ордера сейчас в полёте (place → fill/fail)  │
    │                   │ учитывается в can_trade() через in_flight    │
    ├─────────────────────────────────────────────────────────────────┤
    │  risk._open_      │ ТОЛЬКО подтверждённые filled позиции        │
    │  positions        │ open_position() вызывается после fill       │
    └─────────────────────────────────────────────────────────────────┘

    Жизненный цикл ордера:
    signal → [_traded_markets + _submitting] → await execute()
          → FILLED/PARTIAL: risk.open_position() + _pending_positions
          → CANCELLED/FAILED: очистка, позиция не создаётся
          → market expires → resolve → risk.close_position()
    """

    def __init__(self, config: Config, state: BotState = bot_state):
        self.config = config
        self.state = state
        self.analyzer = ArbitrageAnalyzer(config)
        self.executor = create_executor(config, state)
        self.risk = RiskManager(config)
        self.data_logger = DataLogger()

        # Источник данных — direct / monitoring / auto
        ds = config.data_source.lower()
        if ds == "monitoring":
            self.gamma, self.scanner = build_monitoring_components(config, self._on_price_update)
            self.state.data_source = "MONITORING"
        else:
            # "direct" or "auto" — auto может переключиться в start()
            self.gamma, self.scanner = build_direct_components(config, self._on_price_update)
            self.state.data_source = "DIRECT"

        self._running = False
        self._market_refresh_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._balance_task: asyncio.Task | None = None
        self._started_at = datetime.now(timezone.utc)

        # Одна попытка подачи ордера на рынок за эпоху.
        # Устанавливается на сигнале, сбрасывается когда рынок истекает.
        # НЕ сбрасывается при failed/cancelled — предотвращает повторный спам.
        self._traded_markets: set[str] = set()

        # Ордера сейчас в процессе исполнения (между submit и fill/fail).
        # Используется в can_trade(in_flight=) чтобы не превысить лимит позиций
        # пока реальные fills ещё не подтверждены.
        self._submitting: set[str] = set()

        # Подтверждённые позиции ожидающие разрешения:
        # market_id → {trade, side, order_id, filled_size, avg_fill_price,
        #               estimated_cost, estimated_profit}
        # Создаётся ТОЛЬКО после confirmed fill.
        self._pending_positions: dict[str, dict] = {}

        # market_id-ы для которых уже запущена задача resolution
        self._resolving: set[str] = set()

        # Инициализируем state — режим определяется bot_mode, dry_run/paper_trading производные
        self.state.mode = {"dry": "DRY RUN", "paper": "PAPER", "live": "LIVE"}.get(
            config.bot_mode, "DRY RUN"
        )
        self.state.min_profit_pct = config.min_profit_pct
        self.state.max_position_size = config.max_position_size
        self.state.min_seconds_to_expiry = config.min_seconds_to_expiry
        self.state.max_seconds_to_expiry = config.max_seconds_to_expiry
        self.state.upcoming_window_seconds = config.upcoming_window_seconds
        self.state.limit_discount = config.limit_discount
        self.state.min_favorite_price = config.min_favorite_price
        self.state.candidate_near_delta = config.candidate_near_delta

        # Статические wallet-поля (адреса — без сетевых вызовов)
        self._init_wallet_state()

    async def start(self) -> None:
        """Запустить бота."""
        self._running = True
        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)

        log.info(
            "=" * 60 + "\n"
            "  Polymarket Favourite-Leg Bot (Gabagool strategy)\n"
            "  Mode: %s | Min fav: %.0f%% | Discount: %.0f¢ | Max pos: $%.0f\n"
            + "=" * 60,
            self.state.mode,
            self.config.min_favorite_price * 100,
            self.config.limit_discount * 100,
            self.config.max_position_size,
        )

        # ── Data source selection & observability ────────────────────────────
        ds_cfg = self.config.data_source.lower()

        if ds_cfg == "direct":
            log.info(
                "[DATA SOURCE] DIRECT | gamma=%s  ws=%s",
                self.config.gamma_url,
                self.config.ws_url,
            )
            self.state.add_event("DATA SOURCE: DIRECT")

        elif ds_cfg == "monitoring":
            log.info(
                "[DATA SOURCE] MONITORING | gamma=%s  ws=%s",
                self.config.monitoring_gamma_url,
                self.config.monitoring_ws_url,
            )
            self.state.add_event("DATA SOURCE: MONITORING")

        elif ds_cfg == "auto":
            probe_url = (self.config.monitoring_gamma_url or "").rstrip("/") + "/events"
            log.info("[DATA SOURCE] AUTO — probing %s ...", probe_url)
            self.state.add_event("DATA SOURCE: AUTO — probing monitoring...")

            if await probe_monitoring(self.config):
                self.gamma, self.scanner = build_monitoring_components(
                    self.config, self._on_price_update
                )
                self.state.data_source = "MONITORING"
                log.info(
                    "[DATA SOURCE] AUTO → MONITORING (probe OK) | gamma=%s",
                    self.config.monitoring_gamma_url,
                )
                self.state.add_event("DATA SOURCE → MONITORING (probe OK)")
            else:
                log.warning(
                    "[DATA SOURCE] AUTO → DIRECT (probe FAILED — falling back to Polymarket APIs)"
                )
                self.state.add_event("DATA SOURCE → DIRECT (monitoring unreachable, fallback)")

        else:
            log.warning("[DATA SOURCE] Unknown value %r — defaulting to DIRECT", ds_cfg)
            self.state.add_event(f"DATA SOURCE: unknown '{ds_cfg}' — defaulting to DIRECT")

        self._log_startup_summary()
        await self._refresh_markets()

        if not self.scanner.get_token_ids():
            log.warning("No markets found — will retry in %ds.", self.config.market_refresh_interval)

        self._market_refresh_task = asyncio.create_task(self._market_refresh_loop())
        self._stats_task = asyncio.create_task(self._state_sync_loop())
        self._balance_task = asyncio.create_task(self._balance_refresh_loop())

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

        for task in (self._market_refresh_task, self._stats_task, self._balance_task):
            if task and not task.done():
                task.cancel()

        await self.gamma.close()
        self._log_final_stats()

    async def _on_price_update(self, prices: MarketPrices) -> None:
        """
        Горячий путь: обработка каждого ценового обновления.
        Не делает await — WS цикл не блокируется.
        """
        # Latency: время между получением WS-сообщения и началом обработки
        if prices.recv_time > 0:
            self.state.tick_latency_ms = (time.monotonic() - prices.recv_time) * 1000

        market_id = prices.market.id
        seconds_left = prices.market.seconds_to_expiry

        # Рынок истёк — запускаем resolution в фоне
        if seconds_left is not None and seconds_left <= 0:
            if market_id in self._pending_positions and market_id not in self._resolving:
                self._resolving.add(market_id)
                asyncio.create_task(
                    self._resolve_position(market_id, prices.yes_best_ask, prices.no_best_ask)
                )
            self._traded_markets.discard(market_id)
            if seconds_left < -30:
                self.state.active_market_prices.pop(market_id, None)
            return

        # Обновляем state для дашборда
        self.state.update_market_price(
            market_id=market_id,
            question=prices.market.question,
            yes_ask=prices.yes_best_ask,
            no_ask=prices.no_best_ask,
            seconds_left=seconds_left,
        )

        # Одна попытка на рынок за эпоху — не спамим ордерами
        if market_id in self._traded_markets:
            return

        # Бот на паузе (нажата 'p' в дашборде)
        if self.state.bot_paused:
            return

        # Анализ — ищем фаворита
        opp = self.analyzer.analyze(prices)
        if opp is None:
            return

        self.state.opportunities_found += 1

        # Присваиваем signal_id для сквозной трассировки signal→order→resolution
        opp.signal_id = self.data_logger.new_signal_id()

        # Проверка рисков с учётом ордеров сейчас в полёте (_submitting).
        # Это предотвращает превышение max_concurrent_positions пока fills не подтверждены.
        allowed, reason = self.risk.can_trade(market_id, in_flight=len(self._submitting))
        if not allowed:
            log.debug("Risk block [%s]: %s", prices.market.question[:30], reason)
            self.data_logger.log_signal(opp, mode=self.state.mode,
                                        status="risk_skip", skip_reason=reason)
            return

        # Фиксируем попытку и запускаем исполнение в отдельной задаче
        self._traded_markets.add(market_id)
        self._submitting.add(market_id)
        self.data_logger.log_signal(opp, mode=self.state.mode, status="submitted")
        opp._signal_time = time.monotonic()  # для SIGNAL_TO_ORDER_LATENCY

        # Dashboard last-signal for diagnostics
        self.state.last_signal_info = (
            f"{opp.side} @ {opp.limit_price:.3f} | "
            f"left={int(opp.market.seconds_to_expiry or 0)}s | "
            f"size={int(opp.trade_size)} | cost=${opp.estimated_cost:.2f} | "
            f"{opp.market.question[:30]}"
        )
        log.info(
            "[SIGNAL] %s | %s @ limit=%.3f | left=%.0fs | size=%.0f | cost=$%.2f",
            opp.market.question[:40], opp.side, opp.limit_price,
            opp.market.seconds_to_expiry or 0, opp.trade_size, opp.estimated_cost,
        )
        asyncio.create_task(self._execute_and_track(opp))

    async def _execute_and_track(self, opp: ArbitrageOpportunity) -> None:
        """
        Разместить ордер и — только при confirmed fill — зарегистрировать позицию.

        Flows:
        ┌──────────────┬─────────────────────────────────────────────────────────┐
        │ FILLED       │ risk.open_position() + _pending_positions               │
        ├──────────────┼─────────────────────────────────────────────────────────┤
        │ PARTIAL      │ risk.open_position() + _pending_positions (filled часть)│
        ├──────────────┼─────────────────────────────────────────────────────────┤
        │ NOT_FILLED   │ очистить _submitting. позиция не создаётся              │
        ├──────────────┼─────────────────────────────────────────────────────────┤
        │ CANCELLED    │ очистить _submitting. позиция не создаётся              │
        ├──────────────┼─────────────────────────────────────────────────────────┤
        │ FAILED/error │ очистить _submitting. позиция не создаётся              │
        └──────────────┴─────────────────────────────────────────────────────────┘

        _traded_markets НЕ сбрасывается ни в каком случае:
        одна попытка подачи на рынок за эпоху — без повторного спама.
        """
        market_id = opp.market.id

        # ── Размещаем ордер ───────────────────────────────────────────────────
        executor_type = self.state.mode.replace(" ", "_")   # "DRY RUN"→"DRY_RUN" etc.
        order_info: OrderInfo | None = None
        _t_execute = time.monotonic()
        try:
            order_info = await self.executor.execute(opp)
        except Exception as e:
            log.error("Execution error for %s: %s", opp.market.question[:35], e)
            order_info = OrderInfo(
                order_id="", status="FAILED",
                filled_size=0.0, unfilled_size=opp.trade_size,
                avg_fill_price=0.0,
                placed_at=datetime.now(timezone.utc),
                dry_run=self.config.dry_run,
            )
        finally:
            # _submitting всегда очищается после execute() — независимо от результата
            self._submitting.discard(market_id)

        # SIGNAL_TO_ORDER_LATENCY — от сигнала до завершения execute()
        signal_to_order_ms = (time.monotonic() - _t_execute) * 1000
        self.state.signal_to_order_ms = signal_to_order_ms
        signal_time = getattr(opp, "_signal_time", 0.0)
        total_ms = (time.monotonic() - signal_time) * 1000 if signal_time else 0.0
        log.debug(
            "LATENCY: execute=%.1fms  tick→order=%.1fms  market=%s",
            signal_to_order_ms, total_ms, opp.market.question[:30],
        )

        # Логируем результат исполнения (FILLED / PARTIAL / CANCELLED / FAILED)
        self.data_logger.log_order(opp, order_info,
                                   mode=self.state.mode, executor_type=executor_type)

        # ── Ордер не исполнился: CANCELLED / NOT_FILLED / FAILED ─────────────
        # risk.open_position() НЕ вызывается — позиция не создаётся
        if order_info.filled_size <= 0:
            if order_info.status != "FAILED":   # FAILED уже залогирован выше как error
                log.info(
                    "Order not filled (status=%s) for %s",
                    order_info.status, opp.market.question[:35],
                )
            return

        # ── Ордер исполнён: FILLED или PARTIAL ───────────────────────────────
        # Только здесь risk.open_position() — только при реальном fill
        filled = order_info.filled_size
        fill_price = order_info.avg_fill_price
        actual_cost = filled * fill_price

        if order_info.unfilled_size > 0:
            log.info(
                "Partial fill: %.0f/%.0f shares @ $%.4f for %s",
                filled, filled + order_info.unfilled_size,
                fill_price, opp.market.question[:35],
            )

        # Открываем позицию ПОСЛЕ подтверждённого fill
        self.risk.open_position(market_id)

        trade = RecentTrade(
            market=opp.market.question[:45],
            yes_ask=opp.yes_ask,
            no_ask=opp.no_ask,
            combined=fill_price,           # реальная цена входа
            profit_pct=1.0 - fill_price,   # ожидаемый профит если выиграем
            profit_usd=0.0,                # неизвестно до resolution
            trade_size=filled,             # реальный объём, не запрошенный
            dry_run=self.config.dry_run,
            seconds_left=opp.market.seconds_to_expiry or 0.0,
            side=opp.side,
            outcome="PENDING",
        )
        self.state.add_trade(trade)

        self._pending_positions[market_id] = {
            "trade": trade,
            "side": opp.side,
            "order_id": order_info.order_id,
            "filled_size": filled,
            "limit_price": opp.limit_price or opp.combined_cost,
            "avg_fill_price": fill_price,
            "market_liquidity": opp.market.liquidity,
            "estimated_cost": actual_cost,
            "estimated_profit": filled * (1.0 - fill_price),
            "signal_id": opp.signal_id,   # для связи с resolution
        }

    async def _resolve_position(
        self,
        market_id: str,
        yes_ask: float | None,
        no_ask: float | None,
    ) -> None:
        """Разрешить pending-позицию по реальному исходу рынка."""
        info = self._pending_positions.get(market_id)
        if info is None:
            return

        trade: RecentTrade = info["trade"]
        side: str = info["side"]
        filled_size: float = info["filled_size"]
        avg_fill_price: float = info["avg_fill_price"]
        estimated_cost: float = info["estimated_cost"]

        # Шаг 1: победитель по финальным ценам WS
        winner: str | None = None
        if yes_ask is not None and no_ask is not None:
            if yes_ask < 0.05:
                winner = "NO"
            elif no_ask < 0.05:
                winner = "YES"

        # Шаг 2: Gamma API если WS не дал ответа
        if winner is None:
            winner = await self.gamma.fetch_market_resolution(market_id)
            if winner:
                log.debug("Resolution via Gamma API: %s → %s", market_id[:20], winner)

        # Шаг 3: неизвестно — ждём следующей попытки
        if winner is None:
            log.debug("Resolution unknown for %s — keeping PENDING", market_id[:20])
            self._resolving.discard(market_id)
            return

        # ── Результат известен ────────────────────────────────────────────────
        self._pending_positions.pop(market_id, None)
        self._resolving.discard(market_id)

        won = (winner == side)
        fee = estimated_cost * 0.01   # ~1% комиссия Polymarket

        if won:
            actual_profit = filled_size * (1.0 - avg_fill_price) - fee
            trade.outcome = "WIN"
            trade.profit_usd = actual_profit
            self.state.winning_trades += 1
        else:
            actual_profit = -estimated_cost
            trade.outcome = "LOSS"
            trade.profit_usd = actual_profit

        self.risk.close_position(market_id, actual_profit)
        self.data_logger.log_resolution(
            signal_id=info.get("signal_id", ""),
            market_id=market_id,
            mode=self.state.mode,
            side=side,
            filled_size=filled_size,
            limit_price=info.get("limit_price", avg_fill_price),
            avg_fill_price=avg_fill_price,
            market_liquidity=info.get("market_liquidity", 0.0),
            outcome=trade.outcome,
            winner_side=winner,
            profit_usd=actual_profit,
            entered_at=trade.timestamp,
        )
        log.info(
            "Resolved: %s | %s → %s | filled=%.0f @ $%.4f | profit=$%.2f",
            trade.market[:40], side, trade.outcome,
            filled_size, avg_fill_price, actual_profit,
        )

    def _init_wallet_state(self) -> None:
        """Записать статические wallet-поля в state (без сетевых вызовов)."""
        addr = self.config.polymarket_wallet_address or ""
        self.state.wallet_address_short = (
            f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
        )
        proxy = self.config.polymarket_proxy_address or ""
        self.state.wallet_proxy_short = (
            f"{proxy[:6]}...{proxy[-4:]}" if len(proxy) > 10 else ""
        )
        self.state.wallet_api_configured = bool(
            self.config.poly_api_key
            and self.config.poly_api_secret
            and self.config.poly_api_passphrase
        )

    async def _balance_refresh_loop(self) -> None:
        """Обновлять USDC и POL балансы кошелька каждые 30 секунд."""
        from .balance import BALANCE_REFRESH_INTERVAL, fetch_balances

        address = self.config.polymarket_wallet_address
        if not address:
            log.debug("Balance refresh skipped — POLYMARKET_WALLET_ADDRESS not set")
            return

        while self._running:
            try:
                balances = await fetch_balances(address)
                self.state.wallet_usdc = balances["usdc"]
                self.state.wallet_pol  = balances["pol"]
                log.debug(
                    "Wallet balances: USDC=%.2f  POL=%.4f",
                    self.state.wallet_usdc, self.state.wallet_pol,
                )
                # Warning if balances are low
                if self.state.wallet_usdc < self.config.max_position_size:
                    log.warning(
                        "[WALLET] Low USDC balance: $%.2f < $%.2f (max_position_size)",
                        self.state.wallet_usdc, self.config.max_position_size,
                    )
                if self.state.wallet_pol < 1.0:
                    log.warning(
                        "[WALLET] Low POL balance: %.4f — may not cover gas fees",
                        self.state.wallet_pol,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("Balance fetch failed (will retry in %ds): %s",
                          BALANCE_REFRESH_INTERVAL, exc)

            await asyncio.sleep(BALANCE_REFRESH_INTERVAL)

    def _log_startup_summary(self) -> None:
        """Структурированный startup summary в bot.log и dashboard event log."""
        from .wallet import Wallet

        c = self.config
        ds = self.state.data_source   # финальное значение (после probe для auto режима)

        mode_desc = {
            "DRY RUN": "DRY RUN — симуляция, реальных ордеров нет",
            "PAPER":   "PAPER — виртуальные ордера по реальным ценам",
            "LIVE":    "LIVE — РЕАЛЬНЫЕ ДЕНЬГИ",
        }.get(self.state.mode, self.state.mode)

        ks = "АКТИВЕН" if self.state.kill_switch_active else "выкл"
        le = "включена" if c.live_trading_enabled else "выключена"

        # ── Credential verification (no network, no orders) ───────────────────
        creds = Wallet.verify_credentials(c)

        def _field_status(name: str) -> str:
            return "OK" if name in creds["configured"] else "MISSING"

        wallet_addr = creds["wallet_address"]
        addr_display = (
            f"{wallet_addr[:6]}...{wallet_addr[-4:]}" if wallet_addr else "not set"
        )
        proxy_line = (
            f"  Credentials:  proxy={creds['proxy_address']}" if creds["proxy_address"] else ""
        )

        cred_lines = [
            (
                f"  Credentials:  PRIVATE_KEY={_field_status('POLYMARKET_PRIVATE_KEY')}"
                f"  key_valid={creds['key_valid']}"
                f"  clob_init={'OK' if creds['clob_init_ok'] else 'FAIL'}"
            ),
            (
                f"  Credentials:  WALLET_ADDR={_field_status('POLYMARKET_WALLET_ADDRESS')}"
                f"  ({addr_display})"
            ),
            (
                f"  Credentials:  API_KEY={_field_status('POLY_API_KEY')}"
                f"  API_SECRET={_field_status('POLY_API_SECRET')}"
                f"  API_PASS={_field_status('POLY_API_PASSPHRASE')}"
            ),
        ]
        if proxy_line:
            cred_lines.append(proxy_line)
        if creds["missing"]:
            cred_lines.append(f"  Credentials:  MISSING FIELDS: {', '.join(creds['missing'])}")
        if creds["clob_init_error"]:
            cred_lines.append(f"  Credentials:  CLOB ERROR: {creds['clob_init_error']}")
        cred_ready = "READY" if creds["ready"] else "INCOMPLETE"
        cred_lines.append(f"  Credentials:  trading_client_status={cred_ready}")

        summary_lines = [
            "=" * 62,
            "  STARTUP SUMMARY",
            f"  BOT MODE:     {mode_desc}",
            f"  DATA SOURCE:  {ds}",
            f"  LIVE:         live_enabled={le}   kill_switch={ks}",
            (
                f"  Strategy:     Favourite-Leg  fav>={c.min_favorite_price:.0%}  "
                f"disc={c.limit_discount * 100:.0f}c  max=${c.max_position_size:.0f}"
            ),
            f"  Window:       {c.min_seconds_to_expiry}s – {c.max_seconds_to_expiry}s before expiry",
            (
                f"  Risk:         max ${c.max_daily_loss:.0f}/day  "
                f"max {c.max_concurrent_positions} concurrent positions"
            ),
            *cred_lines,
            "  Data log:     data/signals.csv  orders.csv  resolutions.csv",
            "=" * 62,
        ]
        log.info("\n".join(summary_lines))

        # Log credential warning separately so it stands out
        if creds["missing"]:
            log.warning(
                "[CREDENTIALS] Missing fields: %s — live trading will be unavailable",
                ", ".join(creds["missing"]),
            )
        elif not creds["clob_init_ok"]:
            log.warning("[CREDENTIALS] CLOB client init failed: %s", creds["clob_init_error"])
        else:
            log.info("[CREDENTIALS] All credentials present — trading client ready")

        self.state.add_event(f"STARTED | mode={self.state.mode} | ds={ds} | creds={cred_ready}")

    async def _refresh_markets(self) -> None:
        try:
            markets = await self.gamma.fetch_updown_markets()
            self.scanner.load_markets(markets)
            self.state.markets_loaded = len(markets)
            self.state.tokens_subscribed = len(self.scanner.get_token_ids())
            log.info("Markets refreshed: %d markets / %d tokens",
                     len(markets), self.state.tokens_subscribed)
            self.state.add_event(
                f"REST OK: {len(markets)} markets, {self.state.tokens_subscribed} tokens"
            )
        except Exception as e:
            log.error("Failed to refresh markets: %s", e)
            self.state.add_event(f"REST FAILED: {str(e)[:60]}")

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
            self.state.rejected_no_favorite   = analyzer.get("rejected_no_favorite", 0)
            self.state.rejected_out_of_window = analyzer.get("rejected_out_of_window", 0)
            self.state.rejected_low_liquidity = analyzer.get("rejected_low_liquidity", 0)

            # Считаем рынки в торговом окне и с фаворитом
            in_window = [
                m for m in self.state.active_market_prices.values()
                if m.seconds_left is not None
                and self.config.min_seconds_to_expiry < m.seconds_left < self.config.max_seconds_to_expiry
            ]
            self.state.markets_in_window = len(in_window)
            self.state.markets_with_favorite = sum(
                1 for m in in_window
                if m.favorite_price is not None and m.favorite_price >= self.config.min_favorite_price
            )

            # Favourite price distribution + time-to-expiry breakdown
            # across ALL active markets
            d65, d70, d75, d80 = 0, 0, 0, 0
            tgt5, t25, t12, tlt1 = 0, 0, 0, 0
            for m in self.state.active_market_prices.values():
                fp = m.favorite_price
                if fp is not None:
                    if fp >= 0.80:
                        d80 += 1
                    elif fp >= 0.75:
                        d75 += 1
                    elif fp >= 0.70:
                        d70 += 1
                    elif fp >= 0.65:
                        d65 += 1

                sl = m.seconds_left
                entry_sec = self.config.max_seconds_to_expiry
                if sl is not None and sl > 0:
                    if sl > 300:
                        tgt5 += 1
                    elif sl > entry_sec:
                        t25 += 1
                    elif sl > 60:
                        t12 += 1
                    else:
                        tlt1 += 1

            self.state.fav_dist_65_70 = d65
            self.state.fav_dist_70_75 = d70
            self.state.fav_dist_75_80 = d75
            self.state.fav_dist_80plus = d80
            self.state.time_gt_5m  = tgt5
            self.state.time_2_5m   = t25
            self.state.time_1_2m   = t12
            self.state.time_lt_1m  = tlt1

            self.state.daily_pnl = risk["daily_pnl"]
            self.state.total_pnl = risk["total_profit"]
            self.state.ws_last_msg_sec = self.scanner.seconds_since_last_message
            self.state.ws_connected = self.state.ws_last_msg_sec < 30
            self.state.risk_paused = risk.get("paused", False)
            self.state.traded_this_epoch = len(self._traded_markets)
            self.state.ws_messages_total = self.scanner.message_count
            self.state.ws_unknown_tokens = self.scanner.unknown_token_count
            # Kill switch — перечитываем из env каждую секунду (динамический)
            self.state.kill_switch_active = os.getenv("KILL_SWITCH", "false").lower() == "true"

            # Trade lifecycle
            self.state.active_positions_count = len(self._pending_positions)
            self.state.closed_positions_count = risk.get("total_trades", 0)

            # Last in-window reject from analyzer
            self.state.last_window_reject = analyzer.get("last_window_reject", "")

            # Window market samples — top 3 in-window markets with full debug info.
            # Active_market_prices is keyed by market_id, so we iterate items directly.
            samples = []
            thresh = self.config.min_favorite_price
            in_window_by_id = {
                mid: m
                for mid, m in self.state.active_market_prices.items()
                if m.seconds_left is not None
                and self.config.min_seconds_to_expiry < m.seconds_left < self.config.max_seconds_to_expiry
            }
            for mid_, m in sorted(
                in_window_by_id.items(),
                key=lambda kv: -(kv[1].favorite_price or 0.0),
            )[:3]:
                fp   = m.favorite_price or 0.0
                yes  = m.yes_ask or 0.0
                no   = m.no_ask or 0.0
                left = m.seconds_left or 0.0
                traded = mid_ in self._traded_markets
                if fp >= thresh:
                    reject = "already_traded" if traded else "OK — signal expected"
                else:
                    gap = thresh - fp
                    reject = f"no_fav ({fp:.3f} < {thresh:.2f}, need +{gap:.3f})"
                samples.append({
                    "q": m.question[:34],
                    "yes": yes, "no": no,
                    "fav": m.favorite_side or "—",
                    "fav_p": fp,
                    "left": left,
                    "reject": reject,
                    "traded": traded,
                })
            self.state.window_market_samples = samples

            # Проверяем зависшие позиции
            now = datetime.now(timezone.utc)
            for mid, info in list(self._pending_positions.items()):
                age = (now - info["trade"].timestamp).total_seconds()

                # Принудительно закрываем если зависло > 5 минут
                if age > 300 and mid not in self._resolving:
                    log.warning(
                        "Force-closing stuck position %s (age=%.0fs)", mid[:20], age
                    )
                    self._pending_positions.pop(mid, None)
                    self.risk.close_position(mid, -info["estimated_cost"])

                # Retry resolution через 60 секунд
                elif age > 60 and mid not in self._resolving:
                    self._resolving.add(mid)
                    asyncio.create_task(self._resolve_position(mid, None, None))

    def _log_final_stats(self) -> None:
        risk = self.risk.get_stats()
        log.info(
            "=" * 60 + "\n"
            "  ИТОГ: Сделок=%d  Выигрыш=%.0f%%  PnL=$%.2f%s\n"
            + "=" * 60,
            risk["total_trades"],
            risk["win_rate"],
            risk["total_profit"],
            " (симуляция)" if self.config.dry_run else "",
        )
