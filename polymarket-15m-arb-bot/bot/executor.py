"""Исполнитель ордеров — dry-run и live режимы."""

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .models import ArbitrageOpportunity, TradeResult

log = logging.getLogger(__name__)


class DryRunExecutor:
    """
    Симулирует исполнение сделок без реального размещения ордеров.

    Логирует каждую возможность как если бы она была исполнена.
    Используется для тестирования стратегии.
    """

    def __init__(self, config: Config):
        self.config = config
        self._trades: list[TradeResult] = []
        self._total_simulated_profit = 0.0

    async def execute(self, opp: ArbitrageOpportunity) -> TradeResult:
        """Симулировать исполнение арбитражной сделки."""
        result = TradeResult(
            opportunity=opp,
            success=True,
            dry_run=True,
            yes_fill_price=opp.yes_ask,
            no_fill_price=opp.no_ask,
            actual_cost=opp.estimated_cost,
            actual_profit=opp.estimated_profit,
            executed_at=datetime.now(timezone.utc),
        )

        self._trades.append(result)
        self._total_simulated_profit += opp.estimated_profit

        log.info(
            "[DRY RUN] 🎯 TRADE SIMULATED\n"
            "  Market:  %s\n"
            "  YES ask: $%.4f  NO ask: $%.4f  Combined: $%.4f\n"
            "  Shares:  %.0f  Cost: $%.2f  Net profit: $%.2f (%.2f%%)\n"
            "  Time to expiry: %.0fs\n"
            "  Session profit so far: $%.2f",
            opp.market.question[:60],
            opp.yes_ask,
            opp.no_ask,
            opp.combined_cost,
            opp.trade_size,
            opp.estimated_cost,
            opp.estimated_profit,
            opp.gross_profit_pct * 100,
            opp.market.seconds_to_expiry or 0,
            self._total_simulated_profit,
        )

        return result

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def total_simulated_profit(self) -> float:
        return self._total_simulated_profit

    def get_stats(self) -> dict:
        return {
            "trades": self.trade_count,
            "total_simulated_profit": round(self._total_simulated_profit, 4),
        }


class LiveExecutor:
    """
    Реальное исполнение ордеров через Polymarket CLOB API.

    ВНИМАНИЕ: Требует:
    - py-clob-client (pip install py-clob-client)
    - PRIVATE_KEY, WALLET_ADDRESS, POLY_API_KEY/SECRET/PASSPHRASE в .env

    Пока не реализован — используйте DryRunExecutor для тестирования.
    """

    def __init__(self, config: Config):
        self.config = config
        self._verify_credentials()

    def _verify_credentials(self):
        if not self.config.is_live_configured():
            raise RuntimeError(
                "Live trading requires PRIVATE_KEY, WALLET_ADDRESS, "
                "POLY_API_KEY/SECRET/PASSPHRASE in .env"
            )

    async def execute(self, opp: ArbitrageOpportunity) -> TradeResult:
        raise NotImplementedError(
            "LiveExecutor не реализован. "
            "Используйте DryRunExecutor (DRY_RUN=true) для тестирования. "
            "Для live торговли нужно добавить py-clob-client интеграцию."
        )


def create_executor(config: Config) -> DryRunExecutor | LiveExecutor:
    """Создать нужный тип исполнителя в зависимости от конфига."""
    if config.dry_run:
        log.info("Running in DRY RUN mode — no real orders will be placed")
        return DryRunExecutor(config)
    else:
        log.warning("Running in LIVE mode — real orders will be placed!")
        return LiveExecutor(config)
