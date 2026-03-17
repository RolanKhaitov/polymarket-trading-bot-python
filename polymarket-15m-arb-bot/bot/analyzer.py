"""Анализатор торговых возможностей — стратегия Gabagool (Favourite-Leg).

Логика:
1. Найти рынок с ЧЁТКИМ ФАВОРИТОМ: одна сторона торгуется >= MIN_FAVORITE_PRICE
   (например, YES=0.93, NO=0.09 → YES явно фаворит)

2. В последние MAX_SECONDS_TO_EXPIRY секунд перед закрытием:
   - Маркетмейкеры убирают ликвидность
   - Кто-то в панике продаёт токены ниже справедливой цены
   - Мы ставим лимит ниже текущего аска и ловим эти продажи

3. Лимитная цена = favorite_ask - LIMIT_DISCOUNT
   (например: YES ask=0.96, discount=0.03 → bid=0.93)

4. При закрытии: фаворит выигрывает в ~96% случаев (согласно рыночной цене)
   Profit per share = 1.00 - 0.93 = 0.07 = +7.5%
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .models import ArbitrageOpportunity, MarketPrices

log = logging.getLogger(__name__)

# Комиссия Polymarket (~1% от суммы сделки)
POLYMARKET_FEE_RATE = 0.01

# Нижний порог лимитной цены — не ставим заявки ниже 50¢
# (при threshold=0.75 и discount=0.03 limit=0.72, далеко выше этого флора)
LIMIT_PRICE_MIN_FLOOR = 0.50

# Доля от доступной ликвидности для размера позиции (safety margin)
LIQUIDITY_SAFETY_MARGIN = 0.50


class FavoriteAnalyzer:
    """
    Габагул-стратегия: покупаем ОДНУ сторону (фаворита) в последние минуты.

    Условие входа:
    - Одна сторона стоит >= min_favorite_price (например, 85¢)
    - До закрытия осталось <= max_seconds_to_expiry (например, 120 сек)
    - Наш лимитный бид = favorite_ask - limit_discount

    Это НЕ гарантированный арбитраж, но при покупке по 91-96¢
    фаворит выигрывает с вероятностью ~91-96%.
    """

    def __init__(self, config: Config):
        self.config = config
        self._total_analyzed = 0
        self._total_found = 0
        self._rejected_no_prices = 0
        self._rejected_no_favorite = 0
        self._rejected_out_of_window = 0
        self._rejected_low_liquidity = 0
        self._rejected_size = 0
        self._log_every = 200   # логируем воронку каждые N анализов

        # Last in-window rejection detail — readable string for dashboard diagnostics.
        # Updated every time a market IS in the entry window but fails a filter.
        # Shows exactly which filter failed and what the values were.
        self._last_window_reject: str = ""

    def analyze(self, prices: MarketPrices) -> Optional[ArbitrageOpportunity]:
        """
        Проверить MarketPrices на наличие возможности купить фаворита.

        Порядок фильтров (от самого частого к самому редкому):
          1. prices exist?        — иначе no_prices
          2. in entry window?     — иначе out_of_window  (самый частый reject: 99% рынков)
          3. favourite >= thresh? — иначе no_favorite    (в окне, но рынок не решился)
          4. liquidity OK?        — иначе low_liquidity
          5. trade_size >= 1?     — иначе size_zero

        Важно: window check идёт ДО favourite check — иначе все ~560 рынков
        с ценами ~0.50 (вне окна) считались бы как "rejected_no_favourite",
        что делает воронку бесполезной для диагностики.

        Returns:
            ArbitrageOpportunity если нашли фаворита в нужном окне, иначе None.
        """
        self._total_analyzed += 1

        if prices.yes_best_ask is None or prices.no_best_ask is None:
            self._rejected_no_prices += 1
            return None

        yes_ask = prices.yes_best_ask
        no_ask = prices.no_best_ask

        # ── 1. Проверка временного окна (самый частый фильтр) ────────────────
        # Большинство рынков (~560) не в окне последних N секунд.
        # Проверяем ПЕРВЫМ чтобы rejected_no_favourite отражал реальные случаи
        # "в окне, но рынок не решился", а не "просто не наступило время".
        seconds_left = prices.market.seconds_to_expiry
        if seconds_left is None:
            self._rejected_out_of_window += 1
            return None

        if seconds_left < self.config.min_seconds_to_expiry:
            log.debug("Skipping %s — too close to expiry (%.0fs)", prices.market.question[:35], seconds_left)
            self._rejected_out_of_window += 1
            return None

        if seconds_left > self.config.max_seconds_to_expiry:
            self._rejected_out_of_window += 1
            return None  # окно ещё не открылось

        # ── 2. Определяем фаворита ───────────────────────────────────────────
        # Фаворит = сторона с ВЫСОКОЙ ценой (рынок считает её более вероятной).
        # Достигаем этой точки только для рынков в последних max_seconds_to_expiry.
        if yes_ask >= no_ask and yes_ask >= self.config.min_favorite_price:
            side = "YES"
            fav_ask = yes_ask
            fav_liq = prices.yes_best_ask_size or 0.0
        elif no_ask > yes_ask and no_ask >= self.config.min_favorite_price:
            side = "NO"
            fav_ask = no_ask
            fav_liq = prices.no_best_ask_size or 0.0
        else:
            # В окне, но нет чёткого фаворита (рынок ещё ~50/50)
            self._last_window_reject = (
                f"{prices.market.question[:32]} | "
                f"yes={yes_ask:.3f} no={no_ask:.3f} | left={seconds_left:.0f}s | "
                f"no_fav (max={max(yes_ask, no_ask):.3f} < thresh={self.config.min_favorite_price:.2f})"
            )
            log.debug("In-window reject: %s", self._last_window_reject)
            self._rejected_no_favorite += 1
            return None

        # ── Наш лимитный бид ────────────────────────────────────────────────
        # Мы ставим лимит НИЖЕ текущего аска — рассчитываем что кто-то продаст дешевле
        limit_price = round(fav_ask - self.config.limit_discount, 4)

        # Санитарная проверка: не ставим заявку ниже флора
        if limit_price <= LIMIT_PRICE_MIN_FLOOR:
            self._last_window_reject = (
                f"{prices.market.question[:32]} | "
                f"{side}={fav_ask:.3f} | left={seconds_left:.0f}s | "
                f"limit_floor (limit={limit_price:.3f} ≤ {LIMIT_PRICE_MIN_FLOOR})"
            )
            self._rejected_low_liquidity += 1
            return None

        # ── Проверка ликвидности ─────────────────────────────────────────────
        liq_usd = fav_liq * fav_ask
        if liq_usd < self.config.min_liquidity_usd:
            self._last_window_reject = (
                f"{prices.market.question[:32]} | "
                f"{side}={fav_ask:.3f} liq=${liq_usd:.1f} | left={seconds_left:.0f}s | "
                f"low_liq (${liq_usd:.1f} < ${self.config.min_liquidity_usd:.0f})"
            )
            log.debug("In-window reject: %s", self._last_window_reject)
            self._rejected_low_liquidity += 1
            return None

        # ── Размер позиции (одна сторона) ────────────────────────────────────
        trade_size = self._calc_trade_size(limit_price, fav_liq)
        if trade_size <= 0:
            self._last_window_reject = (
                f"{prices.market.question[:32]} | "
                f"{side}={fav_ask:.3f} liq=${liq_usd:.1f} | left={seconds_left:.0f}s | "
                f"size=0 (max_pos=${self.config.max_position_size:.0f}, liq_shares={fav_liq:.1f})"
            )
            self._rejected_size += 1
            return None

        # ── Периодический лог воронки ─────────────────────────────────────────
        if self._total_analyzed % self._log_every == 0:
            log.info(
                "[FUNNEL] analyzed=%d | no_prices=%d | no_fav=%d | out_of_window=%d"
                " | low_liq=%d | size=%d | found=%d",
                self._total_analyzed,
                self._rejected_no_prices,
                self._rejected_no_favorite,
                self._rejected_out_of_window,
                self._rejected_low_liquidity,
                self._rejected_size,
                self._total_found,
            )

        # ── Ожидаемый P&L (если фаворит выиграет) ───────────────────────────
        estimated_cost = trade_size * limit_price
        fee = estimated_cost * POLYMARKET_FEE_RATE
        gross_profit = trade_size * (1.0 - limit_price)
        estimated_profit = gross_profit - fee
        gross_profit_pct = 1.0 - limit_price  # в долях (если выиграем)

        win_probability = fav_ask  # рыночная оценка вероятности победы
        log.debug(
            "Favourite found: %s | %s @ limit=%.4f | size=%.0f | "
            "expected_profit=$%.2f | win_prob=%.0f%% | left=%.0fs",
            prices.market.question[:40], side, limit_price, trade_size,
            estimated_profit, win_probability * 100, seconds_left,
        )

        self._total_found += 1

        return ArbitrageOpportunity(
            market=prices.market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=limit_price,   # repurposed: цена которую мы платим
            gross_profit_pct=gross_profit_pct,
            yes_liquidity=prices.yes_best_ask_size or 0.0,
            no_liquidity=prices.no_best_ask_size or 0.0,
            trade_size=trade_size,
            estimated_cost=estimated_cost,
            estimated_profit=estimated_profit,
            detected_at=datetime.now(timezone.utc),
            side=side,
            limit_price=limit_price,
        )

    def _calc_trade_size(self, limit_price: float, fav_liq: float) -> float:
        """Рассчитать размер позиции (одна сторона)."""
        # Макс по размеру позиции
        max_by_pos = self.config.max_position_size / limit_price

        # Макс по ликвидности (safety margin — не берём больше половины стакана)
        max_by_liq = fav_liq * LIQUIDITY_SAFETY_MARGIN

        trade_size = int(min(max_by_pos, max_by_liq))
        return float(trade_size) if trade_size >= 1 else 0.0

    def get_stats(self) -> dict:
        return {
            "analyzed": self._total_analyzed,
            "found": self._total_found,
            "rejected_no_prices": self._rejected_no_prices,
            "rejected_no_favorite": self._rejected_no_favorite,
            "rejected_out_of_window": self._rejected_out_of_window,
            "rejected_low_liquidity": self._rejected_low_liquidity,
            "rejected_size": self._rejected_size,
            "last_window_reject": self._last_window_reject,
        }


# Алиас для обратной совместимости (main.py импортирует ArbitrageAnalyzer)
ArbitrageAnalyzer = FavoriteAnalyzer
