"""Риск-менеджер — защита от чрезмерных потерь."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from .config import Config

log = logging.getLogger(__name__)


class RiskManager:
    """
    Контролирует риски:
    - Дневной лимит убытка
    - Максимальное количество одновременных позиций
    - Пауза после серии убытков
    """

    def __init__(self, config: Config):
        self.config = config

        # Дневные трекеры
        self._today = date.today()
        self._daily_pnl = 0.0              # PnL за сегодня

        # Текущие позиции
        self._open_positions: set[str] = set()   # market_id открытых позиций

        # Статистика
        self._total_trades = 0
        self._winning_trades = 0
        self._total_profit = 0.0

        # Пауза
        self._paused_until: Optional[datetime] = None
        self._consecutive_losses = 0

    def can_trade(self, market_id: str) -> tuple[bool, str]:
        """
        Проверить можно ли открывать новую позицию.

        Returns:
            (True, "") если можно торговать
            (False, reason) если нельзя
        """
        self._reset_daily_if_needed()

        # Пауза активна?
        if self._paused_until:
            now = datetime.now(timezone.utc)
            if now < self._paused_until:
                remaining = (self._paused_until - now).total_seconds() / 60
                return False, f"Paused for {remaining:.0f} more minutes"
            else:
                self._paused_until = None
                log.info("Risk pause lifted — resuming trading")

        # Дневной лимит убытка
        if self._daily_pnl <= -self.config.max_daily_loss:
            return False, (
                f"Daily loss limit reached: ${self._daily_pnl:.2f} "
                f"(limit: ${self.config.max_daily_loss:.2f})"
            )

        # Слишком много открытых позиций
        if len(self._open_positions) >= self.config.max_concurrent_positions:
            return False, (
                f"Max concurrent positions: {len(self._open_positions)} "
                f"/ {self.config.max_concurrent_positions}"
            )

        # Рынок уже открыт
        if market_id in self._open_positions:
            return False, f"Already have position in {market_id}"

        return True, ""

    def open_position(self, market_id: str) -> None:
        """Зарегистрировать открытую позицию."""
        self._open_positions.add(market_id)
        log.debug("Position opened: %s (%d total)", market_id, len(self._open_positions))

    def close_position(self, market_id: str, pnl: float) -> None:
        """Закрыть позицию и обновить PnL."""
        self._open_positions.discard(market_id)
        self._daily_pnl += pnl
        self._total_profit += pnl
        self._total_trades += 1

        if pnl > 0:
            self._winning_trades += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            # Пауза после 5 подряд убыточных сделок
            if self._consecutive_losses >= 5:
                from datetime import timedelta
                self._paused_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                log.warning(
                    "5 consecutive losses — pausing for 30 minutes. "
                    "Daily PnL: $%.2f",
                    self._daily_pnl,
                )

        log.debug(
            "Position closed: %s | pnl=$%.2f | daily=$%.2f | total=$%.2f",
            market_id, pnl, self._daily_pnl, self._total_profit,
        )

    def _reset_daily_if_needed(self) -> None:
        """Сбросить дневные счётчики если наступил новый день."""
        today = date.today()
        if today != self._today:
            log.info(
                "New day — resetting daily PnL (was $%.2f)", self._daily_pnl
            )
            self._today = today
            self._daily_pnl = 0.0

    def get_stats(self) -> dict:
        win_rate = (
            self._winning_trades / self._total_trades
            if self._total_trades > 0 else 0
        )
        return {
            "daily_pnl": round(self._daily_pnl, 4),
            "total_profit": round(self._total_profit, 4),
            "total_trades": self._total_trades,
            "winning_trades": self._winning_trades,
            "win_rate": round(win_rate * 100, 1),
            "open_positions": len(self._open_positions),
            "consecutive_losses": self._consecutive_losses,
            "paused": self._paused_until is not None,
        }
