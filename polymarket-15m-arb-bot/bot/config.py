"""Конфигурация бота из переменных окружения / .env файла.

Главный параметр режима — BOT_MODE:
    dry     — симуляция, реальных ордеров нет
    paper   — виртуальные ордера по реальным ценам  ← по умолчанию
    live    — реальная торговля (требует wallet credentials + LIVE_TRADING_ENABLED=true)

Обратная совместимость: если BOT_MODE не задан, читаем DRY_RUN / PAPER_TRADING.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Загружаем .env из папки с ботом
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class Config:

    # ── Bot mode ──────────────────────────────────────────────────────────────
    # Единственный параметр выбора режима.
    # dry_run и paper_trading выводятся из bot_mode в __post_init__ и
    # используются executor-ами — менять их вручную не нужно.
    bot_mode: str = "paper"                             # "dry" | "paper" | "live"
    dry_run: bool = field(default=False, init=False)    # derived: bot_mode == "dry"
    paper_trading: bool = field(default=True, init=False)  # derived: bot_mode == "paper"

    # ── Wallet credentials (для BOT_MODE=live) ────────────────────────────────
    # Основные поля — используются bot/wallet.py
    polymarket_private_key: Optional[str] = None
    polymarket_wallet_address: Optional[str] = None
    polymarket_proxy_address: Optional[str] = None

    # Legacy CLOB API keys (для py-clob-client, если потребуется)
    private_key: Optional[str] = None
    wallet_address: Optional[str] = None
    poly_api_key: Optional[str] = None
    poly_api_secret: Optional[str] = None
    poly_api_passphrase: Optional[str] = None

    # ── Стратегия ─────────────────────────────────────────────────────────────
    max_position_size: float = 1.0        # USD на один рынок
    min_profit_pct: float = 0.005         # (legacy, не используется в Gabagool)
    min_liquidity_usd: float = 10.0       # мин. ликвидность на стороне (USD)
    min_favorite_price: float = 0.75      # мин. цена чтобы считать сторону "фаворитом"
    candidate_near_delta: float = 0.05   # зона кандидатов ниже порога (0.05 = 5¢)
    limit_discount: float = 0.03          # на сколько ниже ask ставим лимит (3¢)
    min_seconds_to_expiry: int = 10       # не входить если < N сек до закрытия
    max_seconds_to_expiry: int = 120      # входим только в последние 2 минуты
    upcoming_window_seconds: int = 600    # показывать "скоро" рынки в окне N сек
    market_refresh_interval: int = 60     # обновлять список рынков каждые N сек

    # ── Риск ──────────────────────────────────────────────────────────────────
    max_daily_loss: float = 50.0          # USD дневной лимит убытка
    max_concurrent_positions: int = 10    # макс. параллельных позиций

    # ── Live safety (блокираторы) ─────────────────────────────────────────────
    live_trading_enabled: bool = False     # явный opt-in; False = live запрещён
    max_live_orders_per_session: int = 20  # лимит live ордеров за сессию
    max_live_position_usd: float = 1.0    # жёсткий cap одной live позиции (USD)
    kill_switch: bool = False             # начальное значение (динамически перечитывается)

    # ── Исполнение ордеров ────────────────────────────────────────────────────
    order_fill_timeout: float = 8.0       # секунд ждать fill до отмены (live)
    order_poll_interval: float = 0.5      # интервал поллинга статуса ордера (live)
    paper_fill_timeout: float = 8.0       # секунд ждать fill в paper mode
    paper_poll_interval: float = 0.25     # интервал чтения цен из state (paper)
    paper_fill_confirm_ticks: int = 2     # тиков подряд ask ≤ limit_price для fill

    # ── API эндпоинты ─────────────────────────────────────────────────────────
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ── Data source ───────────────────────────────────────────────────────────
    data_source: str = "direct"            # "direct" | "monitoring" | "auto"
    monitoring_gamma_url: str = ""         # Gamma-compatible REST endpoint
    monitoring_ws_url: str = ""            # WebSocket URL (Polymarket format)
    monitoring_probe_timeout: float = 3.0  # seconds for health probe in auto mode

    # ── Логи ──────────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ─────────────────────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        """Производные поля — всегда вычисляются из bot_mode."""
        self.dry_run = self.bot_mode == "dry"
        self.paper_trading = self.bot_mode == "paper"

    @classmethod
    def from_env(cls) -> "Config":
        """Загрузить конфигурацию из переменных окружения.

        BOT_MODE — главный параметр режима.
        Если не задан, читаем DRY_RUN / PAPER_TRADING для совместимости.
        """
        bot_mode = os.getenv("BOT_MODE", "").lower().strip()
        if not bot_mode:
            # Backward compatibility: старые .env с DRY_RUN / PAPER_TRADING
            if os.getenv("PAPER_TRADING", "false").lower() == "true":
                bot_mode = "paper"
            elif os.getenv("DRY_RUN", "true").lower() == "false":
                bot_mode = "live"
            else:
                bot_mode = "paper"

        return cls(
            bot_mode=bot_mode,
            # ── Wallet ──────────────────────────────────────────────────────
            polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY") or None,
            polymarket_wallet_address=os.getenv("POLYMARKET_WALLET_ADDRESS") or None,
            polymarket_proxy_address=os.getenv("POLYMARKET_PROXY_ADDRESS") or None,
            # ── Legacy ──────────────────────────────────────────────────────
            private_key=os.getenv("PRIVATE_KEY") or None,
            wallet_address=os.getenv("WALLET_ADDRESS") or None,
            poly_api_key=os.getenv("POLY_API_KEY") or None,
            poly_api_secret=os.getenv("POLY_API_SECRET") or None,
            poly_api_passphrase=os.getenv("POLY_API_PASSPHRASE") or None,
            # ── Safety ──────────────────────────────────────────────────────
            live_trading_enabled=os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true",
            max_live_orders_per_session=int(os.getenv("MAX_LIVE_ORDERS_PER_SESSION", "20")),
            max_live_position_usd=float(os.getenv("MAX_LIVE_POSITION_USD", "1.0")),
            kill_switch=os.getenv("KILL_SWITCH", "false").lower() == "true",
            # ── Strategy ────────────────────────────────────────────────────
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "1")),
            min_profit_pct=float(os.getenv("MIN_PROFIT_PCT", "0.005")),
            min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "10")),
            min_favorite_price=float(os.getenv("MIN_FAVORITE_PRICE", "0.75")),
            candidate_near_delta=float(os.getenv("CANDIDATE_NEAR_DELTA", "0.05")),
            limit_discount=float(os.getenv("LIMIT_DISCOUNT", "0.03")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "50")),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "10")),
            min_seconds_to_expiry=int(os.getenv("MIN_SECONDS_TO_EXPIRY", "10")),
            max_seconds_to_expiry=int(os.getenv("MAX_SECONDS_TO_EXPIRY", "120")),
            upcoming_window_seconds=int(os.getenv("UPCOMING_WINDOW_SECONDS", "600")),
            market_refresh_interval=int(os.getenv("MARKET_REFRESH_INTERVAL", "60")),
            # ── Execution ───────────────────────────────────────────────────
            order_fill_timeout=float(os.getenv("ORDER_FILL_TIMEOUT", "8.0")),
            order_poll_interval=float(os.getenv("ORDER_POLL_INTERVAL", "0.5")),
            paper_fill_timeout=float(os.getenv("PAPER_FILL_TIMEOUT", "8.0")),
            paper_poll_interval=float(os.getenv("PAPER_POLL_INTERVAL", "0.25")),
            paper_fill_confirm_ticks=int(os.getenv("PAPER_FILL_CONFIRM_TICKS", "2")),
            # ── Misc ────────────────────────────────────────────────────────
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            data_source=os.getenv("DATA_SOURCE", "direct").lower(),
            # MONITORING_API_URL is the primary name; MONITORING_GAMMA_URL is a legacy alias
            monitoring_gamma_url=(
                os.getenv("MONITORING_API_URL")
                or os.getenv("MONITORING_GAMMA_URL", "")
            ),
            monitoring_ws_url=os.getenv("MONITORING_WS_URL", ""),
            monitoring_probe_timeout=float(os.getenv("MONITORING_PROBE_TIMEOUT", "3.0")),
        )

    def validate(self) -> None:
        """Проверить конфигурацию на корректность.

        Собирает ВСЕ ошибки (не останавливается на первой) и бросает
        ValueError с понятным описанием. Вызывается один раз при старте.
        """
        errors: list[str] = []

        # ── Bot mode ──────────────────────────────────────────────────────────
        if self.bot_mode not in ("dry", "paper", "live"):
            errors.append(
                f"BOT_MODE={self.bot_mode!r} — неизвестный режим. "
                "Допустимые значения: dry | paper | live"
            )

        # ── Data source ───────────────────────────────────────────────────────
        if self.data_source not in ("direct", "monitoring", "auto"):
            errors.append(
                f"DATA_SOURCE={self.data_source!r} — неизвестный режим. "
                "Допустимые значения: direct | monitoring | auto"
            )

        # ── Live safety interlocks ─────────────────────────────────────────────
        if self.bot_mode == "live":
            if not self.live_trading_enabled:
                errors.append(
                    "BOT_MODE=live требует LIVE_TRADING_ENABLED=true в .env — "
                    "явное подтверждение live торговли"
                )
            if not self.polymarket_private_key:
                errors.append(
                    "LIVE mode requires wallet configuration. "
                    "Set POLYMARKET_PRIVATE_KEY in .env"
                )
            if not (self.polymarket_wallet_address or self.wallet_address):
                errors.append(
                    "LIVE mode requires a wallet address. "
                    "Set POLYMARKET_WALLET_ADDRESS in .env"
                )

        # ── Monitoring URLs ────────────────────────────────────────────────────
        if self.data_source == "monitoring":
            if not (self.monitoring_gamma_url or "").strip():
                errors.append(
                    "DATA_SOURCE=monitoring требует MONITORING_API_URL\n"
                    "  Пример: MONITORING_API_URL=http://localhost:8000"
                )
            if not (self.monitoring_ws_url or "").strip():
                errors.append(
                    "DATA_SOURCE=monitoring требует MONITORING_WS_URL\n"
                    "  Пример: MONITORING_WS_URL=ws://localhost:8000/ws"
                )

        # ── Numeric sanity ─────────────────────────────────────────────────────
        if self.max_position_size <= 0:
            errors.append(
                f"MAX_POSITION_SIZE должен быть > 0 (получено {self.max_position_size})"
            )
        if self.max_daily_loss <= 0:
            errors.append(
                f"MAX_DAILY_LOSS должен быть > 0 (получено {self.max_daily_loss})"
            )
        if self.min_seconds_to_expiry < 0:
            errors.append(
                f"MIN_SECONDS_TO_EXPIRY должен быть >= 0 (получено {self.min_seconds_to_expiry})"
            )
        if self.max_seconds_to_expiry <= self.min_seconds_to_expiry:
            errors.append(
                f"MAX_SECONDS_TO_EXPIRY ({self.max_seconds_to_expiry}) должен быть "
                f"> MIN_SECONDS_TO_EXPIRY ({self.min_seconds_to_expiry})"
            )
        if self.max_concurrent_positions < 1:
            errors.append(
                f"MAX_CONCURRENT_POSITIONS должен быть >= 1 "
                f"(получено {self.max_concurrent_positions})"
            )

        if errors:
            raise ValueError(
                "Ошибки конфигурации:\n" + "\n".join(f"  • {e}" for e in errors)
            )

    def is_live_configured(self) -> bool:
        """Проверить, настроен ли кошелёк для live торговли."""
        return bool(self.polymarket_private_key)

    def wallet_status(self) -> str:
        """Человекочитаемый статус кошелька для UI."""
        if self.polymarket_private_key:
            addr = self.polymarket_wallet_address or ""
            short = f" ({addr[:6]}...{addr[-4:]})" if len(addr) > 10 else ""
            return f"configured{short}"
        return "missing"
