"""Адаптер для Polymarket CLOB API.

Только I/O — никакой бизнес-логики, стратегий, риска или состояния бота.
Все sync-вызовы py-clob-client обёрнуты в asyncio.to_thread().
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Config

log = logging.getLogger(__name__)


# ── Нормализованные статусы ────────────────────────────────────────────────────

class OrderStatus:
    """Строковые константы статусов ордеров (нормализованные)."""
    OPEN      = "OPEN"       # размещён, не исполнен
    FILLED    = "FILLED"     # полностью исполнен
    PARTIAL   = "PARTIAL"    # частично исполнен
    CANCELLED = "CANCELLED"  # отменён (не исполнен)
    FAILED    = "FAILED"     # ошибка при размещении


# CLOB raw status → наш нормализованный статус
_STATUS_MAP: dict[str, str] = {
    "LIVE":               OrderStatus.OPEN,
    "MATCHED":            OrderStatus.FILLED,
    "PARTIALLY MATCHED":  OrderStatus.PARTIAL,
    "CANCELLED":          OrderStatus.CANCELLED,
    "CANCELED":           OrderStatus.CANCELLED,   # альтернативное написание
    "UNMATCHED":          OrderStatus.CANCELLED,
}


def _normalize_status(raw: str) -> str:
    return _STATUS_MAP.get(raw.upper(), OrderStatus.FAILED)


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class PlacedOrder:
    """Результат размещения ордера."""
    order_id:   str            # "" при ошибке
    status:     str            # OrderStatus.*
    raw_status: str            # строка от CLOB как есть
    error:      Optional[str]  # сообщение об ошибке, None при успехе


@dataclass
class OrderState:
    """Текущее состояние ордера (ответ get_order_status)."""
    order_id:       str
    status:         str            # OrderStatus.*
    raw_status:     str
    filled_size:    float          # реально исполнено (shares)
    remaining_size: float          # не исполнено (shares)
    avg_fill_price: float          # средняя цена исполнения (0.0 если не исполнен)
    error:          Optional[str]


# ── Исключение ─────────────────────────────────────────────────────────────────

class ClobApiError(Exception):
    """Ошибка вызова CLOB API."""
    def __init__(self, method: str, detail: str):
        super().__init__(f"CLOB {method}: {detail}")
        self.method = method
        self.detail = detail


# ── Клиент ─────────────────────────────────────────────────────────────────────

class ClobClient:
    """
    Тонкая asyncio-обёртка над py-clob-client.

    Создаёт синхронный ClobClient из библиотеки лениво (при первом вызове).
    Все sync-методы выполняются в отдельном треде через asyncio.to_thread().
    """

    def __init__(self, config: Config):
        self._config = config
        self._client = None   # lazy-init

    # ── Lazy init ──────────────────────────────────────────────────────────────

    def _get_client(self):
        """Вернуть (или создать) синхронный py_clob_client.ClobClient."""
        if self._client is not None:
            return self._client

        try:
            from py_clob_client.client import ClobClient as _SyncClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            raise ClobApiError("init", f"py-clob-client не установлен: {e}") from e

        cfg = self._config

        # Prefer new POLYMARKET_PRIVATE_KEY; fall back to legacy PRIVATE_KEY
        private_key = cfg.polymarket_private_key or cfg.private_key
        if not private_key:
            raise ClobApiError(
                "init",
                "Private key not configured. Set POLYMARKET_PRIVATE_KEY in .env",
            )

        # CLOB API credentials are optional — py_clob_client can sign-only mode
        # without them, but order posting requires them.
        creds: Optional[ApiCreds] = None
        if cfg.poly_api_key:
            creds = ApiCreds(
                api_key=cfg.poly_api_key,
                api_secret=cfg.poly_api_secret or "",
                api_passphrase=cfg.poly_api_passphrase or "",
            )

        self._client = _SyncClient(
            host=cfg.clob_url,
            key=private_key,
            creds=creds,
            chain_id=137,   # Polygon mainnet
        )
        log.debug("ClobClient инициализирован: %s", cfg.clob_url)
        return self._client

    # ── Публичные методы ───────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        token_id: str,
        side: str,         # "BUY" или "SELL"
        limit_price: float,
        size: float,
        expiration: Optional[int] = None,  # unix timestamp; None = GTC
    ) -> PlacedOrder:
        """
        Разместить лимитный ордер.

        Args:
            token_id:    ID токена YES или NO (Polymarket token address)
            side:        "BUY" | "SELL"
            limit_price: цена в долларах (0.0 – 1.0)
            size:        количество shares
            expiration:  unix timestamp истечения (None = Good Till Cancelled)

        Returns:
            PlacedOrder с нормализованным статусом
        """
        def _place():
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL

            client = self._get_client()

            _side = BUY if side.upper() == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=size,
                side=_side,
                expiration=expiration or 0,
            )
            return client.create_and_post_order(order_args)

        try:
            resp = await asyncio.to_thread(_place)
        except ClobApiError:
            raise
        except Exception as e:
            log.error("place_limit_order failed: %s", e)
            raise ClobApiError("place_limit_order", str(e)) from e

        # resp — dict от py-clob-client: {"orderID": "...", "status": "..."}
        raw_status = resp.get("status", "UNKNOWN")
        order_id   = resp.get("orderID", "")

        log.info(
            "Order placed: id=%s status=%s token=%s side=%s price=%.4f size=%.0f",
            order_id, raw_status, token_id[:12], side, limit_price, size,
        )
        return PlacedOrder(
            order_id=order_id,
            status=_normalize_status(raw_status),
            raw_status=raw_status,
            error=None,
        )

    async def get_order_status(self, order_id: str) -> OrderState:
        """
        Получить текущее состояние ордера.

        Returns:
            OrderState с нормализованным статусом и данными об исполнении
        """
        def _get():
            client = self._get_client()
            return client.get_order(order_id)

        try:
            resp = await asyncio.to_thread(_get)
        except ClobApiError:
            raise
        except Exception as e:
            log.error("get_order_status(%s) failed: %s", order_id[:12], e)
            raise ClobApiError("get_order_status", str(e)) from e

        raw_status     = resp.get("status", "UNKNOWN")
        filled_size    = float(resp.get("size_matched",   0) or 0)
        remaining_size = float(resp.get("size_remaining", 0) or 0)

        # avg_fill_price: API может возвращать разные поля — пробуем по приоритету.
        # Намеренно НЕ используем "price" (лимитная цена, не цена исполнения).
        # Если ни одно поле не найдено — возвращаем 0.0, executor сделает fallback.
        avg_price = (
            float(resp.get("average_price") or 0)
            or float(resp.get("avg_price") or 0)
        )

        return OrderState(
            order_id=order_id,
            status=_normalize_status(raw_status),
            raw_status=raw_status,
            filled_size=filled_size,
            remaining_size=remaining_size,
            avg_fill_price=avg_price,
            error=None,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер.

        Returns:
            True если отмена принята, False если ордер не найден / уже закрыт
        """
        def _cancel():
            client = self._get_client()
            return client.cancel(order_id)

        try:
            resp = await asyncio.to_thread(_cancel)
        except ClobApiError:
            raise
        except Exception as e:
            log.error("cancel_order(%s) failed: %s", order_id[:12], e)
            raise ClobApiError("cancel_order", str(e)) from e

        # resp обычно {"canceled": ["order_id"]} или {"not_canceled": [...]}
        canceled = resp.get("canceled", [])
        success  = order_id in canceled
        if not success:
            log.warning("cancel_order: %s not in canceled list: %s", order_id[:12], resp)
        return success
