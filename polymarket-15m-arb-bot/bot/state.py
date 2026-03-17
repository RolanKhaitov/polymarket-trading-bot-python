"""Общее состояние бота — читается дашбордом в реальном времени."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RecentTrade:
    """Запись о последней симулированной / реальной сделке."""
    market: str         # короткое название рынка
    yes_ask: float
    no_ask: float
    combined: float     # Для arb: yes+no. Для favourite: limit_price
    profit_pct: float
    profit_usd: float
    trade_size: float
    dry_run: bool
    seconds_left: float = 0.0
    side: str = "BOTH"          # "YES", "NO" (favourite), "BOTH" (arb)
    outcome: str = "~WIN"       # "~WIN" (ожидаем), "WIN", "LOSS", "PENDING"
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
    # Gabagool strategy
    favorite_side: Optional[str] = None    # "YES" или "NO" — чья цена выше
    favorite_price: Optional[float] = None # цена фаворита (например 0.93)


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
        self.risk_paused: bool = False       # пауза риск-менеджера
        self.bot_paused: bool = False        # ручная пауза (кнопка 'p')
        self.traded_this_epoch: int = 0      # сколько рынков заторгованы сейчас

        # Последние сделки (макс. 20)
        self.recent_trades: list[RecentTrade] = []

        # Активные рынки с ценами (только те что в окне)
        self.active_market_prices: dict[str, ActiveMarket] = {}  # market_id → ActiveMarket

        # Конфиг (для отображения) — синхронизируется из config в main.__init__
        self.min_profit_pct: float = 0.005
        self.max_position_size: float = 1.0
        self.min_seconds_to_expiry: int = 10
        self.max_seconds_to_expiry: int = 120
        self.upcoming_window_seconds: int = 600
        self.limit_discount: float = 0.03
        self.candidate_near_delta: float = 0.05

        # Latency metrics (обновляются на горячем пути)
        self.tick_latency_ms: float = 0.0        # последняя задержка WS-recv → callback
        self.signal_to_order_ms: float = 0.0    # последняя задержка сигнал → ордер отправлен

        # WS diagnostics
        self.ws_messages_total: int = 0          # всего сообщений получено от WS
        self.ws_unknown_tokens: int = 0          # сообщений с неизвестным token_id

        # Pipeline funnel counters (вычисляются в _state_sync_loop)
        self.markets_in_window: int = 0       # рынков сейчас в торговом окне
        self.markets_with_favorite: int = 0   # из них — с фаворитом >= порога
        self.rejected_no_favorite: int = 0    # анализов: нет фаворита
        self.rejected_out_of_window: int = 0  # анализов: вне временного окна
        self.rejected_low_liquidity: int = 0  # анализов: низкая ликвидность

        # Trade lifecycle counters
        self.active_positions_count: int = 0  # pending positions (filled, awaiting resolution)
        self.closed_positions_count: int = 0  # total resolved positions

        # Last-signal / last-reject strings for dashboard diagnostics
        self.last_signal_info: str = ""       # "YES @ 0.88 | left=95s | BTC..."
        self.last_window_reject: str = ""     # "BTC... | yes=0.72 | no_fav (thresh=0.75)"

        # In-window market snapshots — up to 3 markets currently in entry window.
        # Each entry is a dict: {q, yes, no, fav_side, fav_p, left, reject, traded}
        # Computed every second in _state_sync_loop for real-time dashboard debugging.
        self.window_market_samples: list = []

        # Favourite price distribution (по всем active markets, вычисляется каждую секунду)
        self.fav_dist_65_70: int = 0    # 0.65 ≤ fav < 0.70
        self.fav_dist_70_75: int = 0    # 0.70 ≤ fav < 0.75
        self.fav_dist_75_80: int = 0    # 0.75 ≤ fav < 0.80
        self.fav_dist_80plus: int = 0   # fav ≥ 0.80

        # Favourite tracker (Gabagool) — лучший фаворит виденный в окне входа
        self.best_comb_today: float = 0.0    # лучшая fav-цена в окне (0.0 = ничего)
        self.best_comb_market: str = ""      # рынок с лучшим фаворитом
        self.near_misses: int = 0            # фаворит в зоне кандидатов while in-window

        # Time-to-expiry distribution (по всем active markets)
        self.time_gt_5m: int = 0     # seconds_left > 300
        self.time_2_5m: int = 0      # 120 < seconds_left ≤ 300
        self.time_1_2m: int = 0      # 60 < seconds_left ≤ 120
        self.time_lt_1m: int = 0     # 0 < seconds_left ≤ 60

        # Лог событий для дашборда (ring buffer, newest first)
        self.recent_events: deque[str] = deque(maxlen=50)
        self.last_event: str = ""

        # Параметры стратегии (для блока кандидатов) — синхронизируется из config
        self.min_favorite_price: float = 0.75

        # Data source
        self.data_source: str = "DIRECT"     # "DIRECT" | "MONITORING"

        # Live safety state
        self.live_orders_session: int = 0    # live ордеров выполнено за сессию
        self.kill_switch_active: bool = False # текущий статус kill switch (перечитывается динамически)
        self.live_halted: bool = False        # live execution заблокирован (kill switch или session limit)

        # Wallet balance (обновляется фоновой задачей каждые 30с)
        # -1.0 = ещё не получено / кошелёк не настроен
        self.wallet_usdc: float = -1.0          # USDC.e баланс (торговый)
        self.wallet_pol: float = -1.0           # POL / MATIC баланс (для газа)
        self.wallet_address_short: str = ""     # "0xABCD...1234"
        self.wallet_proxy_short: str = ""       # "0xPROX...1234" или ""
        self.wallet_api_configured: bool = False  # все API creds заданы

    def add_event(self, message: str) -> None:
        """Добавить событие в ring buffer (newest first). Потокобезопасно для asyncio."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.recent_events.appendleft(f"{ts} {message}")
        self.last_event = message

    def add_trade(self, trade: RecentTrade) -> None:
        self.recent_trades.insert(0, trade)
        if len(self.recent_trades) > 20:
            self.recent_trades = self.recent_trades[:20]
        self.trades_executed += 1
        # PENDING trades не считаем выигранными сразу — обновим при resolution
        if trade.outcome != "PENDING" and trade.profit_usd > 0:
            self.winning_trades += 1
        # daily_pnl/total_pnl обновляются через _state_sync_loop из RiskManager

    def update_market_price(
        self,
        market_id: str,
        question: str,
        yes_ask: Optional[float],
        no_ask: Optional[float],
        seconds_left: Optional[float],
    ) -> None:
        combined = (yes_ask + no_ask) if (yes_ask is not None and no_ask is not None) else None
        profit_pct = (1.0 - combined) if (combined is not None and combined < 1.0) else None

        # Определяем фаворита (Gabagool strategy)
        favorite_side: Optional[str] = None
        favorite_price: Optional[float] = None
        if yes_ask is not None and no_ask is not None:
            if yes_ask >= no_ask and yes_ask >= 0.65:
                favorite_side = "YES"
                favorite_price = yes_ask
            elif no_ask > yes_ask and no_ask >= 0.65:
                favorite_side = "NO"
                favorite_price = no_ask

        self.active_market_prices[market_id] = ActiveMarket(
            question=question,
            yes_ask=yes_ask,
            no_ask=no_ask,
            seconds_left=seconds_left,
            combined=combined,
            profit_pct=profit_pct,
            favorite_side=favorite_side,
            favorite_price=favorite_price,
        )
        self.price_updates += 1

        # Favourite / near-miss tracker (Gabagool strategy)
        in_entry_window = (
            seconds_left is not None
            and self.min_seconds_to_expiry < seconds_left < self.max_seconds_to_expiry
        )
        if in_entry_window and favorite_price is not None:
            # Best favourite seen in entry window today
            if favorite_price > self.best_comb_today:
                self.best_comb_today = favorite_price
                self.best_comb_market = question
            # Near-miss: favourite was in candidate zone but below threshold
            near_floor = self.min_favorite_price - self.candidate_near_delta
            if near_floor <= favorite_price < self.min_favorite_price:
                self.near_misses += 1

    def get_top_opportunities(self, n: int = 8) -> list[ActiveMarket]:
        """Топ рынков в торговом окне — фавориты первыми (Gabagool strategy)."""
        markets = [
            m for m in self.active_market_prices.values()
            if m.seconds_left is not None
            and self.min_seconds_to_expiry < m.seconds_left < self.max_seconds_to_expiry
        ]
        # Сортируем: фавориты (высокая цена одной стороны) первыми
        markets.sort(key=lambda m: -(m.favorite_price or 0.0))
        return markets[:n]

    def get_upcoming_markets(self, n: int = 8) -> list[ActiveMarket]:
        """Рынки, которые войдут в торговое окно в ближайшие upcoming_window_seconds."""
        upper = self.max_seconds_to_expiry + self.upcoming_window_seconds
        markets = [
            m for m in self.active_market_prices.values()
            if m.seconds_left is not None
            and self.max_seconds_to_expiry <= m.seconds_left <= upper
        ]
        markets.sort(key=lambda m: m.seconds_left or 9999)
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
