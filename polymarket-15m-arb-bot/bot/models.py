"""Модели данных бота."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    yes_best_ask_size: Optional[float] = None
    no_best_ask_size: Optional[float] = None
    recv_time: float = 0.0  # time.monotonic() когда WS-сообщение получено

    # Внутренний стакан (все уровни) — зарезервировано для будущего использования
    yes_asks: list = field(default_factory=list)
    no_asks: list = field(default_factory=list)

    @property
    def combined_ask(self) -> Optional[float]:
        """YES_ask + NO_ask — если < 1.0, есть арбитраж."""
        if self.yes_best_ask is None or self.no_best_ask is None:
            return None
        return self.yes_best_ask + self.no_best_ask

    @property
    def gross_profit_pct(self) -> Optional[float]:
        c = self.combined_ask
        if c is None:
            return None
        return 1.0 - c

    @property
    def min_liquidity(self) -> Optional[float]:
        if self.yes_best_ask_size is None or self.no_best_ask_size is None:
            return None
        return min(self.yes_best_ask_size, self.no_best_ask_size)


@dataclass
class ArbitrageOpportunity:
    """Обнаруженная торговая возможность (favourite-leg)."""
    market: Market
    yes_ask: float
    no_ask: float
    combined_cost: float       # Для favourite: limit_price (что планируем платить)
    gross_profit_pct: float    # Ожидаемая прибыль если выиграем (1 - limit_price)
    yes_liquidity: float       # shares доступно на YES
    no_liquidity: float        # shares доступно на NO
    trade_size: float          # запрошенный объём (shares)
    estimated_cost: float      # trade_size * limit_price
    estimated_profit: float    # trade_size * (1 - limit_price) - fees (если выиграем)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    side: str = "BOTH"         # "YES", "NO" (favourite), или "BOTH" (arb)
    limit_price: float = 0.0   # наша лимитная цена покупки (favourite_ask - discount)
    signal_id: str = ""        # id для связи signal→order→resolution в data/

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
class OrderInfo:
    """
    Результат размещения ордера — возвращается из executor.execute().

    Единая структура для dry-run и live режимов:
    - dry-run: status="FILLED", filled_size=requested, order_id=""
    - live: содержит реальные данные от CLOB API

    Partial fill: filled_size < (filled_size + unfilled_size)
    """
    order_id: str           # "" в dry-run, реальный ID в live
    status: str             # "FILLED" | "PARTIAL" | "CANCELLED" | "OPEN"
    filled_size: float      # реально исполнено (shares)
    unfilled_size: float    # не исполнено (shares)
    avg_fill_price: float   # средняя цена исполнения (0.0 если не исполнен)
    placed_at: datetime     # когда выставлен ордер
    dry_run: bool
