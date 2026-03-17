"""Исполнитель ордеров — dry-run и live режимы."""

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from .config import Config
from .models import ArbitrageOpportunity, OrderInfo


def _beep() -> None:
    """Звуковой сигнал в фоновом потоке — НЕ блокирует event loop."""
    def _play():
        try:
            import winsound
            winsound.Beep(1000, 300)
            winsound.Beep(1200, 200)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()


log = logging.getLogger(__name__)


class DryRunExecutor:
    """
    Симулирует исполнение сделок без реального размещения ордеров.

    Возвращает OrderInfo с status="FILLED" и полным заполнением —
    симуляция предполагает что лимитный ордер исполняется по limit_price.
    """

    def __init__(self, config: Config):
        self.config = config
        self._trade_count = 0

    async def execute(self, opp: ArbitrageOpportunity) -> OrderInfo:
        self._trade_count += 1
        _beep()

        limit_price = opp.limit_price or opp.combined_cost
        side_str = opp.side if opp.side != "BOTH" else "YES+NO"
        fav_ask = opp.yes_ask if opp.side == "YES" else opp.no_ask

        log.info(
            "[DRY RUN] BID PLACED\n"
            "  Market:  %s\n"
            "  Side: %s | Fav ask: $%.4f | Our limit: $%.4f | Discount: $%.4f\n"
            "  YES: $%.3f  NO: $%.3f\n"
            "  Shares: %.0f | Cost: $%.2f | Expected profit: $%.2f (+%.1f%%) if win\n"
            "  Time to expiry: %.0fs | Win probability: ~%.0f%%",
            opp.market.question[:60],
            side_str, fav_ask, limit_price, fav_ask - limit_price,
            opp.yes_ask, opp.no_ask,
            opp.trade_size,
            opp.estimated_cost,
            opp.estimated_profit,
            opp.gross_profit_pct * 100,
            opp.market.seconds_to_expiry or 0,
            fav_ask * 100,
        )

        return OrderInfo(
            order_id="",
            status="FILLED",
            filled_size=opp.trade_size,
            unfilled_size=0.0,
            avg_fill_price=limit_price,
            placed_at=datetime.now(timezone.utc),
            dry_run=True,
        )

    @property
    def trade_count(self) -> int:
        return self._trade_count


class LiveExecutor:
    """
    Реальное исполнение ордеров через Polymarket CLOB API (via ClobClient).

    Требует:
    - py-clob-client (pip install py-clob-client)
    - PRIVATE_KEY, WALLET_ADDRESS, POLY_API_KEY/SECRET/PASSPHRASE в .env
    """

    def __init__(self, config: Config):
        self.config = config
        if not config.is_live_configured():
            raise RuntimeError(
                "Live trading requires PRIVATE_KEY, WALLET_ADDRESS, "
                "POLY_API_KEY/SECRET/PASSPHRASE in .env"
            )
        from .clob_client import ClobClient
        self._clob = ClobClient(config)

    async def execute(self, opp: ArbitrageOpportunity) -> OrderInfo:
        from .clob_client import ClobApiError, OrderStatus

        limit_price = opp.limit_price or opp.combined_cost
        side = opp.side   # "YES" | "NO"
        token_id = (
            opp.market.yes_token.token_id if side == "YES"
            else opp.market.no_token.token_id
        )

        _beep()
        log.info(
            "[LIVE] Placing limit order: %s | side=%s price=%.4f size=%.0f",
            opp.market.question[:50], side, limit_price, opp.trade_size,
        )

        # ── 1. Размещаем ордер ─────────────────────────────────────────────────
        try:
            placed = await self._clob.place_limit_order(
                token_id=token_id,
                side="BUY",
                limit_price=limit_price,
                size=opp.trade_size,
            )
        except ClobApiError as e:
            log.error("Failed to place order: %s", e)
            return self._make_failed(opp, placed_at=datetime.now(timezone.utc))

        if placed.status == OrderStatus.FAILED or not placed.order_id:
            return self._make_failed(opp, placed_at=datetime.now(timezone.utc))

        placed_at = datetime.now(timezone.utc)

        # ── 2. Поллим статус до terminal state или таймаута ────────────────────
        timeout   = self.config.order_fill_timeout
        poll_int  = self.config.order_poll_interval
        elapsed   = 0.0
        state     = None

        while elapsed < timeout:
            await asyncio.sleep(poll_int)
            elapsed += poll_int

            try:
                state = await self._clob.get_order_status(placed.order_id)
            except ClobApiError as e:
                log.warning("get_order_status error (will retry): %s", e)
                continue

            # FILLED или CANCELLED — терминальное состояние, выходим
            if state.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                break
            # PARTIAL продолжаем поллить — ждём ещё заполнения до дедлайна

        # ── 3. Таймаут (OPEN или PARTIAL): отменяем незаполненный остаток ──────
        if state is None or state.status in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            log.warning(
                "Order %s not fully filled in %.0fs (status=%s) — cancelling remainder",
                placed.order_id[:12], timeout, state.status if state else "NO_RESPONSE",
            )
            try:
                await self._clob.cancel_order(placed.order_id)
                # Финальный poll чтобы зафиксировать актуальный filled_size
                try:
                    state = await self._clob.get_order_status(placed.order_id)
                except ClobApiError:
                    pass  # используем state который был до cancel
            except ClobApiError as e:
                log.error("cancel_order failed: %s", e)

        # ── 4. Формируем итоговый OrderInfo ───────────────────────────────────
        filled    = state.filled_size    if state else 0.0
        remaining = state.remaining_size if state else opp.trade_size

        # avg_fill_price fallback: если API не вернул реальную цену,
        # но шэры были куплены — используем limit_price как лучшее приближение
        avg = state.avg_fill_price if state else 0.0
        if filled > 0 and avg == 0.0:
            avg = limit_price

        # CANCELLED + filled_size > 0 = частичное исполнение перед отменой
        result_status = (state.status if state else OrderStatus.CANCELLED)
        if result_status == OrderStatus.CANCELLED and filled > 0:
            result_status = OrderStatus.PARTIAL

        log.info(
            "[LIVE] Order settled: id=%s status=%s filled=%.2f avg_price=%.4f",
            placed.order_id[:12], result_status, filled, avg,
        )

        return OrderInfo(
            order_id=placed.order_id,
            status=result_status,
            filled_size=filled,
            unfilled_size=remaining,
            avg_fill_price=avg,
            placed_at=placed_at,
            dry_run=False,
        )

    @staticmethod
    def _make_failed(opp: ArbitrageOpportunity, placed_at: datetime) -> OrderInfo:
        return OrderInfo(
            order_id="", status="FAILED",
            filled_size=0.0, unfilled_size=opp.trade_size,
            avg_fill_price=0.0,
            placed_at=placed_at, dry_run=False,
        )


class PaperExecutor:
    """
    Реалистичная бумажная торговля: виртуальные ордера против реальных цен.

    Отличия от DryRunExecutor:
    - Не возвращает FILLED мгновенно.
    - Ждёт N последовательных тиков где ask ≤ limit_price (confirmation window).
    - Одиночный тик на уровне — не fill; цена должна задержаться.
    - Отменяет виртуальный ордер по таймауту.
    - fill_price = ask в момент последнего confirmation тика.

    Модель симуляции:
    - FILLED:    ask ≤ limit_price на N тиков подряд (paper_fill_confirm_ticks)
    - CANCELLED (timeout):  timeout истёк до накопления N тиков
    - CANCELLED (no market): рынок исчез из active_market_prices
    - PARTIAL:   не моделируется — active_market_prices не содержит ask_size

    Осознанные ограничения:
    - Нет queue position: предполагаем, что впереди очереди при fill.
    - Нет market impact: виртуальный ордер не сдвигает цены.
    - fill_price = ask (не limit_price) — консервативная, честная оценка.

    Источник данных: bot_state.active_market_prices (WS, реальное время).
    """

    def __init__(self, config: Config, state: "BotState"):
        self.config = config
        self._state = state

    async def execute(self, opp: ArbitrageOpportunity) -> OrderInfo:
        limit_price    = opp.limit_price or opp.combined_cost
        side           = opp.side   # "YES" | "NO"
        market_id      = opp.market.id
        placed_at      = datetime.now(timezone.utc)
        timeout        = self.config.paper_fill_timeout
        poll_int       = self.config.paper_poll_interval
        confirm_needed = self.config.paper_fill_confirm_ticks

        _beep()
        log.info(
            "[PAPER] Virtual order: %s | side=%s limit=%.4f size=%.0f confirm=%d ticks",
            opp.market.question[:50], side, limit_price, opp.trade_size, confirm_needed,
        )

        elapsed       = 0.0
        confirm_count = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_int)
            elapsed += poll_int

            market = self._state.active_market_prices.get(market_id)
            if market is None:
                # Рынок истёк или вышел из окна tracking
                log.info(
                    "[PAPER] Market no longer tracked after %.1fs — virtual order cancelled",
                    elapsed,
                )
                return OrderInfo(
                    order_id="", status="CANCELLED",
                    filled_size=0.0, unfilled_size=opp.trade_size,
                    avg_fill_price=0.0, placed_at=placed_at, dry_run=False,
                )

            ask = market.yes_ask if side == "YES" else market.no_ask
            if ask is None:
                # Нет данных цены — пропускаем тик, счётчик не сбрасываем
                continue

            if ask <= limit_price:
                confirm_count += 1
                log.debug(
                    "[PAPER] Price at limit %d/%d: ask=%.4f ≤ limit=%.4f",
                    confirm_count, confirm_needed, ask, limit_price,
                )
                if confirm_count >= confirm_needed:
                    log.info(
                        "[PAPER] FILLED (%d ticks confirmed): %s | ask=%.4f size=%.0f elapsed=%.1fs",
                        confirm_count, opp.market.question[:40], ask, opp.trade_size, elapsed,
                    )
                    return OrderInfo(
                        order_id="",
                        status="FILLED",
                        filled_size=opp.trade_size,
                        unfilled_size=0.0,
                        avg_fill_price=ask,   # цена последнего тика подтверждения
                        placed_at=placed_at,
                        dry_run=False,
                    )
            else:
                if confirm_count > 0:
                    log.debug(
                        "[PAPER] Price moved above limit (ask=%.4f) — resetting confirm count",
                        ask,
                    )
                confirm_count = 0

        # Таймаут — цена так и не задержалась на уровне
        log.info(
            "[PAPER] NOT FILLED in %.0fs (ask never confirmed ≤ %.4f) — cancelled",
            timeout, limit_price,
        )
        return OrderInfo(
            order_id="", status="CANCELLED",
            filled_size=0.0, unfilled_size=opp.trade_size,
            avg_fill_price=0.0, placed_at=placed_at, dry_run=False,
        )


def _is_kill_switch() -> bool:
    """Динамически читает KILL_SWITCH из environment — без рестарта бота."""
    return os.getenv("KILL_SWITCH", "false").lower() == "true"


def _guard_blocked(opp: "ArbitrageOpportunity", placed_at: datetime) -> "OrderInfo":
    """Заглушка-OrderInfo для заблокированных guard'ом сделок."""
    return OrderInfo(
        order_id="", status="CANCELLED",
        filled_size=0.0, unfilled_size=opp.trade_size,
        avg_fill_price=0.0, placed_at=placed_at, dry_run=False,
    )


class LiveSafetyGuard:
    """
    Оборачивает LiveExecutor live-специфичными safety-проверками.

    Проверки (fail-fast, до любых сетевых вызовов):
    1. Kill switch  — динамический, берётся из env на каждой попытке (без рестарта)
    2. Session limit — лимит исполненных live ордеров за сессию
    3. Position cap  — жёсткий USD-лимит на одну позицию

    Дневной убыток не дублируется — он уже обрабатывается RiskManager.
    При блокировке → OrderInfo(CANCELLED, filled_size=0), позиция не открывается.
    """

    def __init__(self, config: Config, state: "BotState"):
        self._inner  = LiveExecutor(config)
        self._config = config
        self._state  = state

    async def execute(self, opp: "ArbitrageOpportunity") -> "OrderInfo":
        placed_at = datetime.now(timezone.utc)

        # ── 1. Kill switch (динамически) ───────────────────────────────────
        if _is_kill_switch():
            log.warning(
                "[LIVE GUARD] Kill switch active — order blocked: %s",
                opp.market.question[:40],
            )
            self._state.kill_switch_active = True
            self._state.live_halted = True
            return _guard_blocked(opp, placed_at)

        # ── 2. Session order limit ─────────────────────────────────────────
        if self._state.live_orders_session >= self._config.max_live_orders_per_session:
            log.warning(
                "[LIVE GUARD] Session limit %d reached — no more live orders this session",
                self._config.max_live_orders_per_session,
            )
            self._state.live_halted = True
            return _guard_blocked(opp, placed_at)

        # ── 3. Position size hard cap ──────────────────────────────────────
        if opp.estimated_cost > self._config.max_live_position_usd:
            log.warning(
                "[LIVE GUARD] Position $%.2f exceeds live cap $%.2f — order blocked",
                opp.estimated_cost, self._config.max_live_position_usd,
            )
            return _guard_blocked(opp, placed_at)

        # ── All guards passed — delegate to LiveExecutor ───────────────────
        result = await self._inner.execute(opp)

        if result.filled_size > 0:
            self._state.live_orders_session += 1

        return result


# TYPE_CHECKING импорт чтобы избежать circular dependency
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .state import BotState


def create_executor(
    config: Config,
    state: "BotState | None" = None,
) -> "DryRunExecutor | PaperExecutor | LiveSafetyGuard":
    """Создать исполнитель в зависимости от конфига."""
    if config.paper_trading:
        if state is None:
            raise RuntimeError("PaperExecutor requires state — pass bot_state to create_executor()")
        log.info("Running in PAPER TRADING mode — virtual orders against real market prices")
        return PaperExecutor(config, state)
    elif config.dry_run:
        log.info("Running in DRY RUN mode — no real orders will be placed")
        return DryRunExecutor(config)
    else:
        # Live mode — обязательный opt-in feature flag
        if not config.live_trading_enabled:
            raise RuntimeError(
                "Live trading is disabled.\n"
                "Set LIVE_TRADING_ENABLED=true in .env AND DRY_RUN=false to enable."
            )
        if state is None:
            raise RuntimeError("LiveSafetyGuard requires state — pass bot_state to create_executor()")
        log.warning("Running in LIVE mode — real money will be used!")
        return LiveSafetyGuard(config, state)
