"""Конфигурация бота из переменных окружения / .env файла."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Загружаем .env из папки с ботом
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class Config:
    # ── Кошелёк ───────────────────────────────────────────────────────────
    private_key: Optional[str] = None
    wallet_address: Optional[str] = None

    # ── Polymarket API ─────────────────────────────────────────────────────
    poly_api_key: Optional[str] = None
    poly_api_secret: Optional[str] = None
    poly_api_passphrase: Optional[str] = None

    # ── Режим ─────────────────────────────────────────────────────────────
    dry_run: bool = True

    # ── Торговые параметры ────────────────────────────────────────────────
    max_position_size: float = 50.0       # USD на рынок
    min_profit_pct: float = 0.02          # 2% gross (≈1% net после комиссий)
    min_liquidity_usd: float = 50.0       # мин. ликвидность на стороне

    # ── Риск ──────────────────────────────────────────────────────────────
    max_seconds_to_expiry: int = 1000     # не торговать если окно ещё не началось
    max_daily_loss: float = 20.0          # USD дневной лимит убытка
    max_concurrent_positions: int = 5     # макс. параллельных позиций

    # ── Таймауты ──────────────────────────────────────────────────────────
    min_seconds_to_expiry: int = 30       # не входить если < 30 сек до закрытия
    max_seconds_to_expiry: int = 1000     # не входить если > 16 мин 40 сек (окно ещё не началось)
    market_refresh_interval: int = 60     # обновлять список рынков каждые N сек

    # ── API эндпоинты ─────────────────────────────────────────────────────
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ── Логи ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        """Загрузить конфигурацию из переменных окружения."""
        return cls(
            private_key=os.getenv("PRIVATE_KEY") or None,
            wallet_address=os.getenv("WALLET_ADDRESS") or None,
            poly_api_key=os.getenv("POLY_API_KEY") or None,
            poly_api_secret=os.getenv("POLY_API_SECRET") or None,
            poly_api_passphrase=os.getenv("POLY_API_PASSPHRASE") or None,
            dry_run=os.getenv("DRY_RUN", "true").lower() != "false",
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "50")),
            min_profit_pct=float(os.getenv("MIN_PROFIT_PCT", "0.02")),
            min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "50")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "20")),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "5")),
            min_seconds_to_expiry=int(os.getenv("MIN_SECONDS_TO_EXPIRY", "30")),
            max_seconds_to_expiry=int(os.getenv("MAX_SECONDS_TO_EXPIRY", "1000")),
            market_refresh_interval=int(os.getenv("MARKET_REFRESH_INTERVAL", "60")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def is_live_configured(self) -> bool:
        """Проверить, настроены ли credentials для live торговли."""
        return bool(self.private_key and self.wallet_address and self.poly_api_key)
