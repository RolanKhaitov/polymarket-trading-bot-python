"""
Data collection layer — сохранение сигналов, ордеров и резолвов в CSV.

Цель: постфактум анализ качества стратегии.
Не содержит стратегической логики. Не влияет на торговый flow.

Файлы:
    data/signals.csv     — каждый обнаруженный сигнал (включая пропущенные)
    data/orders.csv      — каждая попытка исполнения с итогом
    data/resolutions.csv — финальный исход каждой позиции
"""

import csv
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .models import ArbitrageOpportunity, OrderInfo

# ── Директория данных (абсолютный путь от корня проекта) ───────────────────────
_DEFAULT_DATA_DIR = Path(__file__).parent.parent / "data"

# ── Словарь для извлечения тикера из названия рынка ───────────────────────────
_ASSET_KEYWORDS: dict[str, str] = {
    "Bitcoin":    "BTC",
    "Ethereum":   "ETH",
    "Solana":     "SOL",
    "XRP":        "XRP",
    "Dogecoin":   "DOGE",
    "BNB":        "BNB",
    "Hyperliquid": "HYPE",
}

# ── Заголовки CSV ──────────────────────────────────────────────────────────────
_SIGNALS_HEADERS = [
    "signal_id", "timestamp", "market_id", "market_slug",
    "asset", "side", "fav_price", "other_side_price",
    "entry_limit_price", "seconds_left", "market_liquidity",
    "mode", "status", "skip_reason",
]

_ORDERS_HEADERS = [
    "order_event_id", "signal_id", "timestamp", "market_id",
    "mode", "executor_type", "limit_price", "requested_size",
    "filled_size", "unfilled_size", "avg_fill_price", "slippage",
    "order_status", "cancel_reason", "placed_at",
]

_RESOLUTIONS_HEADERS = [
    "resolution_id", "signal_id", "timestamp", "market_id",
    "mode", "side", "filled_size", "limit_price", "avg_fill_price",
    "slippage", "market_liquidity", "outcome", "winner_side",
    "profit_usd", "seconds_held",
]


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _extract_asset(question: str) -> str:
    for keyword, ticker in _ASSET_KEYWORDS.items():
        if keyword in question:
            return ticker
    return "OTHER"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


# ── DataLogger ────────────────────────────────────────────────────────────────

class DataLogger:
    """
    Записывает три типа событий в CSV-файлы.

    Потокобезопасен для asyncio: все вызовы синхронны (нет await),
    context switch не происходит внутри write — гонок нет.
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._signals     = self._dir / "signals.csv"
        self._orders      = self._dir / "orders.csv"
        self._resolutions = self._dir / "resolutions.csv"
        self._init_files()

    def _init_files(self) -> None:
        """Создать файлы с header если не существуют."""
        for path, headers in (
            (self._signals,     _SIGNALS_HEADERS),
            (self._orders,      _ORDERS_HEADERS),
            (self._resolutions, _RESOLUTIONS_HEADERS),
        ):
            if not path.exists():
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(headers)

    def _append(self, path: Path, row: dict, headers: list[str]) -> None:
        """Append one row. Silently logs on I/O error — never raises to caller."""
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=headers).writerow(row)
        except Exception as exc:
            log.warning("DataLogger write failed (%s): %s", path.name, exc)

    @staticmethod
    def new_signal_id() -> str:
        """Генерировать уникальный signal_id для связи signal→order→resolution."""
        return _short_id()

    # ── Публичный API ─────────────────────────────────────────────────────────

    def log_signal(
        self,
        opp: "ArbitrageOpportunity",
        mode: str,
        status: str,          # "submitted" | "risk_skip"
        skip_reason: str = "",
    ) -> None:
        """
        Записать обнаруженный сигнал.

        Вызывается при КАЖДОМ сигнале — до того, как известно, исполнится ли ордер.
        status="risk_skip" позволяет анализировать, сколько сигналов теряется из-за риска.
        """
        m = opp.market
        if opp.side == "YES":
            fav_price, other_price = opp.yes_ask, opp.no_ask
        elif opp.side == "NO":
            fav_price, other_price = opp.no_ask, opp.yes_ask
        else:  # "BOTH" или неизвестный side
            fav_price, other_price = opp.yes_ask, opp.no_ask
        self._append(self._signals, {
            "signal_id":         opp.signal_id,
            "timestamp":         _now_iso(),
            "market_id":         m.id,
            "market_slug":       m.slug,
            "asset":             _extract_asset(m.question),
            "side":              opp.side,
            "fav_price":         round(fav_price, 4),
            "other_side_price":  round(other_price, 4),
            "entry_limit_price": round(opp.limit_price or opp.combined_cost, 4),
            "seconds_left":      round(m.seconds_to_expiry or 0.0, 1),
            "market_liquidity":  round(m.liquidity, 2),
            "mode":              mode,
            "status":            status,
            "skip_reason":       skip_reason,
        }, _SIGNALS_HEADERS)

    def log_order(
        self,
        opp: "ArbitrageOpportunity",
        order_info: "OrderInfo",
        mode: str,
        executor_type: str,   # "DRY_RUN" | "PAPER" | "LIVE"
    ) -> None:
        """
        Записать результат попытки исполнения.

        order_status: FILLED, PARTIAL, CANCELLED, FAILED.
        cancel_reason: производное поле для быстрой фильтрации незаполненных ордеров.
        placed_at:     когда ордер был выставлен (из OrderInfo); разность timestamp-placed_at = fill_latency.
        """
        # Derive cancel_reason — позволяет отличить timeout/safety/error без JOIN
        s, f = order_info.status, order_info.filled_size
        if s == "FAILED":
            cancel_reason = "execution_error"
        elif s == "CANCELLED" and f > 0:
            cancel_reason = "partial_timeout"
        elif s == "CANCELLED" and f == 0:
            cancel_reason = "not_filled"
        else:
            cancel_reason = ""

        self._append(self._orders, {
            "order_event_id": _short_id(),
            "signal_id":      opp.signal_id,
            "timestamp":      _now_iso(),
            "market_id":      opp.market.id,
            "mode":           mode,
            "executor_type":  executor_type,
            "limit_price":    round(opp.limit_price or opp.combined_cost, 4),
            "requested_size": round(opp.trade_size, 2),
            "filled_size":    round(order_info.filled_size, 2),
            "unfilled_size":  round(order_info.unfilled_size, 2),
            "avg_fill_price": round(order_info.avg_fill_price, 4),
            "slippage":       round((opp.limit_price or opp.combined_cost) - order_info.avg_fill_price, 4) if order_info.filled_size > 0 else "",
            "order_status":   order_info.status,
            "cancel_reason":  cancel_reason,
            "placed_at":      order_info.placed_at.isoformat(),
        }, _ORDERS_HEADERS)

    def log_resolution(
        self,
        signal_id: str,
        market_id: str,
        mode: str,
        side: str,
        filled_size: float,
        limit_price: float,
        avg_fill_price: float,
        market_liquidity: float,
        outcome: str,         # "WIN" | "LOSS"
        winner_side: str,
        profit_usd: float,
        entered_at: datetime,
    ) -> None:
        """
        Записать финальный исход позиции после экспирации рынка.

        seconds_held = время от трейда до резолва.
        slippage = limit_price - avg_fill_price (сколько заплатили меньше лимита).
        """
        now = datetime.now(timezone.utc)
        seconds_held = (now - entered_at).total_seconds()
        slippage = round(limit_price - avg_fill_price, 4) if avg_fill_price > 0 else ""
        self._append(self._resolutions, {
            "resolution_id":  _short_id(),
            "signal_id":      signal_id,
            "timestamp":      now.isoformat(),
            "market_id":      market_id,
            "mode":           mode,
            "side":           side,
            "filled_size":    round(filled_size, 2),
            "limit_price":    round(limit_price, 4),
            "avg_fill_price": round(avg_fill_price, 4),
            "slippage":       slippage,
            "market_liquidity": round(market_liquidity, 2),
            "outcome":        outcome,
            "winner_side":    winner_side or "",
            "profit_usd":     round(profit_usd, 4),
            "seconds_held":   round(seconds_held, 1),
        }, _RESOLUTIONS_HEADERS)
