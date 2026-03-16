"""Анализатор арбитражных возможностей."""

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .models import ArbitrageOpportunity, MarketPrices

log = logging.getLogger(__name__)

# Комиссия Polymarket на каждую сторону (~1% от суммы)
# Точная формула: fee = price * size * 0.01
POLYMARKET_FEE_RATE = 0.01


class ArbitrageAnalyzer:
    """
    Анализирует MarketPrices и определяет есть ли арбитражная возможность.

    Условие арбитража:
        yes_ask + no_ask < 1.0

    С учётом комиссий (~1% на каждую сторону):
        net_profit = 1.0 - yes_ask - no_ask - yes_ask*fee - no_ask*fee
                   = 1.0 - combined * (1 + fee)

    Для наличия прибыли нужно:
        combined < 1.0 / (1 + fee) ≈ 0.9901  (при fee=1%)

    Мы используем порог min_profit_pct чтобы дополнительно отфильтровать
    слишком маленькие возможности (шум).
    """

    def __init__(self, config: Config):
        self.config = config
        self._total_analyzed = 0
        self._total_found = 0

    def analyze(self, prices: MarketPrices) -> Optional[ArbitrageOpportunity]:
        """
        Проверить MarketPrices на наличие арбитража.

        Returns:
            ArbitrageOpportunity если есть прибыльная возможность, иначе None.
        """
        self._total_analyzed += 1

        # Нужны обе цены
        if prices.yes_best_ask is None or prices.no_best_ask is None:
            return None

        yes_ask = prices.yes_best_ask
        no_ask = prices.no_best_ask
        combined = yes_ask + no_ask

        # Быстрая проверка: combined должен быть < 1.0
        if combined >= 1.0:
            return None

        # Gross profit до комиссий
        gross_profit_pct = 1.0 - combined

        # Net profit с учётом комиссий
        # Fee = (yes_ask + no_ask) * fee_rate (приблизительно)
        total_fee = combined * POLYMARKET_FEE_RATE
        net_profit_pct = gross_profit_pct - total_fee

        # Проверяем что net profit >= min_profit_pct
        if net_profit_pct < self.config.min_profit_pct:
            if gross_profit_pct > 0:
                log.debug(
                    "Near-miss: %s | combined=%.4f gross=%.2f%% net=%.2f%% (threshold=%.1f%%)",
                    prices.market.question[:40],
                    combined,
                    gross_profit_pct * 100,
                    net_profit_pct * 100,
                    self.config.min_profit_pct * 100,
                )
            return None

        # Проверяем время до закрытия
        seconds_left = prices.market.seconds_to_expiry
        if seconds_left is None:
            return None

        # Слишком мало времени — не входим
        if seconds_left < self.config.min_seconds_to_expiry:
            log.debug(
                "Skipping %s — too close to expiry (%.0fs left)",
                prices.market.question[:40],
                seconds_left,
            )
            return None

        # Слишком много времени — окно ещё не открылось, тихо пропускаем
        if seconds_left > self.config.max_seconds_to_expiry:
            return None

        # Проверяем ликвидность
        yes_liq = prices.yes_best_ask_size or 0.0
        no_liq = prices.no_best_ask_size or 0.0

        # Мин. ликвидность в USD = shares * price
        yes_liq_usd = yes_liq * yes_ask
        no_liq_usd = no_liq * no_ask

        if yes_liq_usd < self.config.min_liquidity_usd:
            log.debug(
                "Skipping %s — low YES liquidity $%.2f < $%.2f",
                prices.market.question[:40],
                yes_liq_usd,
                self.config.min_liquidity_usd,
            )
            return None

        if no_liq_usd < self.config.min_liquidity_usd:
            log.debug(
                "Skipping %s — low NO liquidity $%.2f < $%.2f",
                prices.market.question[:40],
                no_liq_usd,
                self.config.min_liquidity_usd,
            )
            return None

        # Рассчитываем размер позиции
        trade_size = self._calc_trade_size(yes_ask, no_ask, yes_liq, no_liq)
        if trade_size <= 0:
            return None

        estimated_cost = trade_size * combined
        estimated_gross = trade_size * gross_profit_pct
        estimated_fee = trade_size * combined * POLYMARKET_FEE_RATE
        estimated_net = estimated_gross - estimated_fee

        self._total_found += 1

        return ArbitrageOpportunity(
            market=prices.market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined,
            gross_profit_pct=gross_profit_pct,
            yes_liquidity=yes_liq,
            no_liquidity=no_liq,
            trade_size=trade_size,
            estimated_cost=estimated_cost,
            estimated_profit=estimated_net,
            detected_at=datetime.now(timezone.utc),
        )

    def _calc_trade_size(
        self,
        yes_ask: float,
        no_ask: float,
        yes_liq: float,
        no_liq: float,
    ) -> float:
        """
        Рассчитать оптимальный размер позиции в shares.

        Ограничения:
        1. max_position_size (USD)
        2. Доступная ликвидность (50% safety margin)
        3. Обе стороны должны иметь одинаковое кол-во shares
        """
        combined = yes_ask + no_ask

        # Макс. по размеру позиции
        max_by_position = self.config.max_position_size / combined

        # Макс. по ликвидности (берём меньшую сторону с safety margin 50%)
        available_liq = min(yes_liq, no_liq) * 0.5

        # Итоговый размер
        trade_size = min(max_by_position, available_liq)

        # Округляем вниз до целых shares
        trade_size = int(trade_size)

        # Минимум 1 share
        return float(trade_size) if trade_size >= 1 else 0.0

    def get_stats(self) -> dict:
        return {
            "analyzed": self._total_analyzed,
            "found": self._total_found,
        }
