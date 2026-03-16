"""Общее состояние бота — читается дашбордом в реальном времени."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RecentTrade:
    """Запись о последней симулированной / реальной сделке."""
    market: str         # короткое название рынка
    yes_ask: float
    no_ask: float
    combined: float
    profit_pct: float
    profit_usd: float
    trade_size: float
    dry_run: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ActiveMarket:
    """Рынок, по которому сейчас идут обновления цен."""
    question: str
    yes_ask: Optional[float]
    no_ask: Optional[float]
    seconds_left: Optional[float]
    combined: Optional[float]
    profit_pct: Optional[float]   # None если combined >= 1


class BotState:
    """
    Singleton-объект состояния.
    Бот пишет сюда данные, дашборд читает.
    """

    def __init__(self):
        self.started_at: datetime = datetime.now(timezone.utc)
        self.mode: str = "DRY RUN"
        self.running: bool = False

        # Статистика
        self.markets_loaded: int = 0
        self.tokens_subscribed: int = 0
        self.price_updates: int = 0
        self.analyzed: int = 0
        self.opportunities_found: int = 0
        self.trades_executed: int = 0
        self.winning_trades: int = 0
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self.ws_last_msg_sec: float = 0.0
        self.ws_connected: bool = False

        # Последние сделки (макс. 20)
        self.recent_trades: list[RecentTrade] = []

        # Активные рынки с ценами (только те что в окне)
        self.active_market_prices: dict[str, ActiveMarket] = {}  # market_id → ActiveMarket

        # Конфиг (для отображения)
        self.min_profit_pct: float = 0.02
        self.max_position_size: float = 50.0
        self.min_seconds_to_expiry: int = 30
        self.max_seconds_to_expiry: int = 1000

    def add_trade(self, trade: RecentTrade) -> None:
        self.recent_trades.insert(0, trade)
        if len(self.recent_trades) > 20:
            self.recent_trades = self.recent_trades[:20]
        self.trades_executed += 1
        if trade.profit_usd > 0:
            self.winning_trades += 1
        self.daily_pnl += trade.profit_usd
        self.total_pnl += trade.profit_usd

    def update_market_price(
        self,
        market_id: str,
        question: str,
        yes_ask: Optional[float],
        no_ask: Optional[float],
        seconds_left: Optional[float],
    ) -> None:
        combined = (yes_ask + no_ask) if (yes_ask and no_ask) else None
        profit_pct = (1.0 - combined) if combined and combined < 1.0 else None

        self.active_market_prices[market_id] = ActiveMarket(
            question=question,
            yes_ask=yes_ask,
            no_ask=no_ask,
            seconds_left=seconds_left,
            combined=combined,
            profit_pct=profit_pct,
        )
        self.price_updates += 1

    def get_top_opportunities(self, n: int = 8) -> list[ActiveMarket]:
        """Топ рынков по близости к арбитражу (combined наименьший)."""
        markets = [
            m for m in self.active_market_prices.values()
            if m.combined is not None
            and m.seconds_left is not None
            and self.min_seconds_to_expiry < m.seconds_left < self.max_seconds_to_expiry
        ]
        markets.sort(key=lambda m: m.combined or 2.0)
        return markets[:n]

    @property
    def win_rate(self) -> float:
        if self.trades_executed == 0:
            return 0.0
        return self.winning_trades / self.trades_executed * 100

    @property
    def uptime_str(self) -> str:
        delta = datetime.now(timezone.utc) - self.started_at
        total = int(delta.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# Глобальный singleton
bot_state = BotState()
