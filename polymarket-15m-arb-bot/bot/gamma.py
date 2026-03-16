"""
Клиент Gamma API для поиска активных "Up or Down" рынков.

Polymarket создаёт "Up or Down" рынки для BTC/ETH/SOL/XRP и др.
каждые 5 минут на весь следующий торговый день.

Структура:
- Event title: "Bitcoin Up or Down - March 17, 11:30AM-11:45AM ET"  (15-мин)
               "Bitcoin Up or Down - March 17, 11:30AM-11:35AM ET"  (5-мин)
- Event endDate = конец временного окна (время резолюции)
- Market clobTokenIds[0] = YES token ID, [1] = NO token ID
- Market outcomePrices[0] = цена YES, [1] = цена NO
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import Config
from .models import Market, Token

log = logging.getLogger(__name__)


class GammaClient:
    """Клиент Gamma API (https://gamma-api.polymarket.com)."""

    EVENTS_URL = "https://gamma-api.polymarket.com/events"

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_updown_markets(self) -> list[Market]:
        """
        Вернуть все активные "Up or Down" рынки.

        Возвращаем ВСЕ такие рынки — фильтрацию по активному окну
        делает ArbitrageAnalyzer через max_seconds_to_expiry.
        """
        session = await self._get_session()
        all_markets: list[Market] = []
        offset = 0
        limit = 500

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "tag_slug": "crypto",
                "order": "endDate",
                "ascending": "true",
            }

            try:
                async with session.get(self.EVENTS_URL, params=params) as resp:
                    if resp.status != 200:
                        log.error("Gamma API error: HTTP %s", resp.status)
                        break
                    events = await resp.json()
            except Exception as e:
                log.error("Gamma API request failed: %s", e)
                break

            if not events:
                break

            now = datetime.now(timezone.utc)

            for event in events:
                title = event.get("title") or ""
                if "Up or Down" not in title:
                    continue

                end_str = event.get("endDate") or ""
                end_dt = self._parse_dt(end_str)
                if end_dt is None or end_dt <= now:
                    continue  # уже истёк

                for sub in (event.get("markets") or []):
                    market = self._parse_sub_market(sub, end_dt, title)
                    if market:
                        all_markets.append(market)

            if len(events) < limit:
                break
            offset += limit

        log.info("Fetched %d Up-or-Down markets from Gamma API", len(all_markets))
        return all_markets

    def _parse_sub_market(
        self, data: dict, end_date: datetime, event_title: str
    ) -> Optional[Market]:
        """Распарсить суб-рынок из события."""
        try:
            # clobTokenIds и outcomes приходят как JSON-строки
            def parse_json_field(val):
                if isinstance(val, str):
                    return json.loads(val)
                return val or []

            token_ids = parse_json_field(data.get("clobTokenIds"))
            outcomes = parse_json_field(data.get("outcomes"))
            prices_raw = parse_json_field(data.get("outcomePrices"))

            if len(token_ids) < 2:
                return None

            # Если outcomes не заданы — YES=index 0, NO=index 1
            yes_idx, no_idx = 0, 1
            for i, outcome in enumerate(outcomes):
                o = str(outcome).strip().lower()
                if o in ("yes", "up"):
                    yes_idx = i
                elif o in ("no", "down"):
                    no_idx = i

            def safe_price(idx: int) -> float:
                try:
                    return float(prices_raw[idx]) if idx < len(prices_raw) else 0.5
                except (ValueError, TypeError):
                    return 0.5

            yes_token = Token(
                token_id=token_ids[yes_idx],
                outcome="Yes",
                price=safe_price(yes_idx),
            )
            no_token = Token(
                token_id=token_ids[no_idx],
                outcome="No",
                price=safe_price(no_idx),
            )

            # Используем вопрос суб-рынка или название события
            question = data.get("question") or event_title
            start_date = self._parse_dt(data.get("startDate"))

            liquidity = float(
                data.get("liquidityClob") or data.get("liquidity") or 0
            )

            return Market(
                id=str(data.get("id") or data.get("conditionId") or ""),
                question=question,
                slug=data.get("slug") or "",
                yes_token=yes_token,
                no_token=no_token,
                end_date=end_date,
                start_date=start_date,
                liquidity=liquidity,
                active=bool(data.get("active", True)),
            )

        except Exception as e:
            log.debug("Failed to parse sub-market: %s", e)
            return None

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
