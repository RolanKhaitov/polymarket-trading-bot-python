"""Модели данных бота."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Token:
    """YES или NO токен рынка."""
    token_id: str
    outcome: str   # "Yes" или "No"
    price: float   # текущая цена из Gamma API


@dataclass
class Market:
    """Предсказательный рынок на Polymarket."""
    id: str
    question: str
    slug: str
    yes_token: Token
    no_token: Token
    end_date: Optional[datetime]
    start_date: Optional[datetime]
    liquidity: float
    active: bool

    @property
    def duration_seconds(self) -> Optional[float]:
        """Длительность рынка в секундах."""
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).total_seconds()
        return None

    @property
    def seconds_to_expiry(self) -> Optional[float]:
        """Секунд до закрытия рынка."""
        if self.end_date:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            end = self.end_date
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return (end - now).total_seconds()
        return None


@dataclass
class MarketPrices:
    """Текущие лучшие цены рынка по WebSocket."""
    market: Market
    yes_best_ask: Optional[float] = None
    no_best_ask: Optional[float] = None
    yes_best_ask_size: Optional[float] = None   # Ликвидность на best ask YES
    no_best_ask_size: Optional[float] = None    # Ликвидность на best ask NO

    # Внутренний стакан (все уровни) для расчёта средней цены при большом объёме
    yes_asks: list = field(default_factory=list)   # [(price, size), ...]
    no_asks: list = field(default_factory=list)    # [(price, size), ...]

    @property
    def combined_ask(self) -> Optional[float]:
        """YES_ask + NO_ask — если < 1.0, есть арбитраж."""
        if self.yes_best_ask is None or self.no_best_ask is None:
            return None
        return self.yes_best_ask + self.no_best_ask

    @property
    def gross_profit_pct(self) -> Optional[float]:
        """Gross прибыль в долях: 1 - combined_ask."""
        c = self.combined_ask
        if c is None:
            return None
        return 1.0 - c

    @property
    def min_liquidity(self) -> Optional[float]:
        """Минимальная доступная ликвидность (меньшая из двух сторон) в shares."""
        if self.yes_best_ask_size is None or self.no_best_ask_size is None:
            return None
        return min(self.yes_best_ask_size, self.no_best_ask_size)


@dataclass
class ArbitrageOpportunity:
    """Обнаруженная арбитражная возможность."""
    market: Market
    yes_ask: float
    no_ask: float
    combined_cost: float       # yes_ask + no_ask
    gross_profit_pct: float    # (1 - combined_cost) до комиссий
    yes_liquidity: float       # shares доступно на YES
    no_liquidity: float        # shares доступно на NO
    trade_size: float          # shares для торговли
    estimated_cost: float      # trade_size * combined_cost
    estimated_profit: float    # trade_size * (1 - combined_cost) - fees
    detected_at: datetime = field(default_factory=datetime.utcnow)

    def summary(self) -> str:
        return (
            f"[ARBI] {self.market.question[:50]}\n"
            f"  YES: ${self.yes_ask:.4f}  NO: ${self.no_ask:.4f}  "
            f"Combined: ${self.combined_cost:.4f}\n"
            f"  Gross profit: {self.gross_profit_pct*100:.2f}%  "
            f"Size: {self.trade_size:.0f} shares  "
            f"Cost: ${self.estimated_cost:.2f}  "
            f"Profit: ${self.estimated_profit:.2f}"
        )


@dataclass
class TradeResult:
    """Результат исполнения сделки."""
    opportunity: ArbitrageOpportunity
    success: bool
    dry_run: bool
    yes_fill_price: Optional[float] = None
    no_fill_price: Optional[float] = None
    actual_cost: Optional[float] = None
    actual_profit: Optional[float] = None
    error: Optional[str] = None
    executed_at: datetime = field(default_factory=datetime.utcnow)
