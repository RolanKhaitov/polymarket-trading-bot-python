"""
Клиент Gamma API для поиска активных 5-минутных крипто-рынков Polymarket.

Polymarket создаёт "Up or Down" рынки для BTC/ETH/SOL/XRP и др.
строго каждые 5 минут в течение торгового дня (09:30–16:00 ET).

Структура ответа /markets:
- question: "Will Bitcoin be higher or lower at 11:45AM ET?"
         или "Bitcoin Up or Down - March 17, 11:45AM ET"
- endDate:  конец 5-минутного окна (время резолюции)
- clobTokenIds[0] = YES token ID, [1] = NO token ID
- outcomePrices[0] = цена YES, [1] = цена NO
- outcomes: ["Yes","No"] или ["Up","Down"]

Discovery filter order:
  1. binary outcomes (exactly 2) + valid token ids (≥ 2)
  2. endDate in the future (not expired)
  3. 5-minute format: question contains "up or down" OR "higher or lower"
     — Polymarket's specific phrasing used ONLY for 5-min crypto series
     — window sanity check: if startDate present, window must be ≤ FIVE_MIN_MAX_WINDOW_S
  4. crypto asset name in question
  5. parse + add

The strategy targets ONLY 5-minute crypto markets. 15-minute, 1-hour, daily,
and all other horizon markets are explicitly excluded at step 3.

config.max_seconds_to_expiry (trading entry, 120s default): how close to
    market close ArbitrageAnalyzer will enter a trade.  Applied by the
    analyzer, not here.  These two values are independent.
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from .config import Config
from .models import Market, Token

log = logging.getLogger(__name__)

# ── Discovery constants ───────────────────────────────────────────────────────

# 5-minute market identification.
# The primary filter is the question TEXT (most reliable signal).
# The window check is a secondary sanity gate for markets that DO have startDate.
#
# Polymarket 5-min markets: window = exactly 300s (5 min).
# We allow up to 600s (10 min) to handle any creation-timing edge cases
# while still excluding all 15-min (900s), hourly, and daily markets.
FIVE_MIN_WINDOW_S: int = 300       # target window for a 5-minute market
FIVE_MIN_MAX_WINDOW_S: int = 600   # sanity cap: anything > 10 min is NOT a 5-min market

# Question substrings that identify Polymarket's crypto direction markets.
# NOTE: "up or down" is used for BOTH 5-min and 15-min markets.
#   5-min:  "Bitcoin Up or Down - March 18, 9:45AM-9:50AM ET"
#   15-min: "Bitcoin Up or Down - March 18, 9:45AM-10:00AM ET"
# "higher or lower" is the legacy format — always 5-min.
# Time-range parsing in _is_5min_question() is the true discriminator.
_FIVE_MIN_QUESTION_PATTERNS: tuple[str, ...] = (
    "up or down",       # current format (both 5-min and 15-min — further filtered by time range)
    "higher or lower",  # legacy format — always 5-min
)

# Regex to extract a time range like "9:45AM-9:50AM" or "9:45am-10:00pm"
_TIME_RANGE_RE = re.compile(
    r'(\d{1,2}):(\d{2})\s*(am|pm)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(am|pm)',
    re.IGNORECASE,
)

# Crypto asset terms for question matching.
# Applied AFTER the duration filter — so "Fed lower bound / FDV above /
# earthquake higher than X" are already gone by this point.
# Covers full names and common ticker symbols.
_CRYPTO_TERMS = frozenset([
    # Full names
    "bitcoin", "ethereum", "ripple", "dogecoin",
    "cardano", "avalanche", "chainlink", "polkadot",
    "litecoin", "uniswap", "solana", "binance coin",
    "hyperliquid",
    # Ticker symbols — kept short to avoid false positives on generic text.
    # "btc", "eth", "xrp", "bnb", "ada", "avax", "link", "dot",
    # "shib", "ltc", "trx", "uni", "sol" — added below with space guards.
])

# Ticker-only terms that need extra care — checked as whole words.
# e.g. "eth" could appear in "method"; "sol" in "solution".
# We check these against the lowercased question with surrounding space padding.
_CRYPTO_TICKERS = frozenset([
    "btc", "eth", "xrp", "bnb", "ada", "avax",
    "shib", "ltc", "trx", "uni", "sol", "doge", "hype",
])

# Coin keywords for _coin_name() grouping / top-N filter
_COIN_KEYWORDS = [
    "Bitcoin", "Ethereum", "XRP", "Dogecoin",
    "BNB", "Cardano", "Avalanche", "Chainlink",
    "Polkadot", "Shiba", "Litecoin", "TRON", "Uniswap", "Solana",
    "Hyperliquid",
]

_EXCLUDED_COINS: frozenset[str] = frozenset()
TOP_N_COINS = 10


def _parse_question_window_min(question: str) -> Optional[int]:
    """
    Extract the trading window length (in minutes) from a time-range in the question.

    Parses "9:45AM-9:50AM" → 5, "9:45AM-10:00AM" → 15.
    Returns None if no time range is found.
    """
    match = _TIME_RANGE_RE.search(question)
    if not match:
        return None

    def to_minutes(h: str, m: str, meridiem: str) -> int:
        hour, minute = int(h), int(m)
        mer = meridiem.lower()
        if mer == "pm" and hour != 12:
            hour += 12
        elif mer == "am" and hour == 12:
            hour = 0
        return hour * 60 + minute

    start_min = to_minutes(match.group(1), match.group(2), match.group(3))
    end_min   = to_minutes(match.group(4), match.group(5), match.group(6))
    return (end_min - start_min) % (24 * 60)


def _is_5min_question(question: str) -> bool:
    """
    Return True if the question is a 5-minute crypto direction market.

    Polymarket creates BOTH 5-min and 15-min "Up or Down" markets.
    They share identical phrasing — the only reliable discriminator is the
    time range embedded in the question:
        5-min:  "Bitcoin Up or Down - March 18, 9:45AM-9:50AM ET"   → 5 min ✓
        15-min: "Bitcoin Up or Down - March 18, 9:45AM-10:00AM ET"  → 15 min ✗

    "higher or lower" is the legacy format (always 5-min, no time range).
    """
    q = question.lower()

    # Legacy format: always 5-minute
    if "higher or lower" in q:
        return True

    if "up or down" not in q:
        return False

    # Parse time range to distinguish 5-min from 15-min
    window_min = _parse_question_window_min(question)
    if window_min is None:
        # No time range in question — accept (assume legacy/unknown format is 5-min)
        return True
    return window_min == 5


def _is_crypto_question(question: str) -> bool:
    """
    Return True if the question is about a known crypto asset.

    Full-name check: substring match (e.g. "bitcoin" in "Will Bitcoin go up?").
    Ticker check: whole-word match only (e.g. " eth " not "method").
    """
    q = question.lower()

    # Full name — substring is safe (names are long enough to be unambiguous)
    if any(term in q for term in _CRYPTO_TERMS):
        return True

    # Ticker — require word boundary via space padding
    padded = f" {q} "
    return any(f" {ticker} " in padded for ticker in _CRYPTO_TICKERS)


class GammaClient:
    """Клиент Gamma API (https://gamma-api.polymarket.com)."""

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ─────────────────────────────────────────────────────────────────────────

    async def fetch_updown_markets(self) -> list[Market]:
        """
        Return all active short-window crypto direction markets.

        Source: GET /markets?active=true&closed=false
        Filter: binary → not-expired → window ≤ DISCOVERY_MAX_WINDOW_S →
                crypto question → parse
        """
        session = await self._get_session()
        all_markets: list[Market] = []
        offset = 0
        limit = 500
        markets_url = f"{self.config.gamma_url}/markets"

        log.info(
            "[GAMMA] targeting 5-minute crypto markets | "
            "question-range-parse discriminator | trading_entry=%ds",
            self.config.max_seconds_to_expiry,
        )

        # Collect 5-min questions across pages (up to 10) for diagnosis
        all_5min_questions: list[str] = []
        total_5min_across_pages = 0

        # Cutoff for early termination: stop fetching once we see markets past the horizon.
        # Re-computed each loop iteration so it stays accurate during long fetches.
        tracking_horizon_s = (
            self.config.max_seconds_to_expiry
            + self.config.upcoming_window_seconds
            + self.config.market_refresh_interval * 2
        )

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "endDate",   # soonest-expiring first → finds today's markets on page 1
                "ascending": "true",
            }

            query_str = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{markets_url}?{query_str}"
            log.info("[GAMMA] GET %s", full_url)

            http_status = None
            try:
                async with session.get(markets_url, params=params) as resp:
                    http_status = resp.status
                    if resp.status != 200:
                        log.error("[GAMMA] /markets HTTP %s | URL=%s", resp.status, full_url)
                        break
                    raw = await resp.json()
            except Exception as e:
                log.error("[GAMMA] /markets request failed: %s | URL=%s", e, full_url)
                break

            page: list = raw if isinstance(raw, list) else (raw or {}).get("markets", [])
            log.info("[GAMMA] HTTP %s | response count=%d | offset=%d",
                     http_status, len(page), offset)

            # ── First-page sanity check ───────────────────────────────────────
            if offset == 0:
                sample_qs = [m.get("question", "?")[:80] for m in (page or [])[:3]]
                log.info("[GAMMA] First page: %d markets | sample questions: %s",
                         len(page or []),
                         sample_qs or "(empty — API returned no markets)")
                if not page:
                    log.warning("[GAMMA] /markets?active=true&closed=false returned 0 results.")
                    await self._log_diagnostic_sample(session, markets_url)
                    break

            if not page:
                break

            now = datetime.now(timezone.utc)

            # ── Per-page funnel counters ──────────────────────────────────────
            n_total          = len(page)
            n_binary         = 0
            n_not_expired    = 0
            n_5min_format    = 0   # question matches 5-min pattern + time range
            n_not_5min       = 0   # question does NOT match (dropped)
            n_crypto         = 0
            n_not_crypto     = 0
            n_added          = 0

            not_5min_samples:   list[str] = []   # up to 5 non-5min questions
            not_crypto_samples: list[str] = []   # up to 5 5-min but non-crypto
            added_samples:      list[str] = []   # up to 5 added

            for m in page:
                question = m.get("question") or ""

                # ── 1. Binary structure ───────────────────────────────────────
                token_ids = self._parse_json_field(m.get("clobTokenIds"))
                outcomes  = self._parse_json_field(m.get("outcomes"))
                if len(token_ids) < 2 or len(outcomes) != 2:
                    continue
                n_binary += 1

                # ── 2. Not expired ────────────────────────────────────────────
                end_dt = self._parse_dt(m.get("endDate") or "")
                if end_dt is None or end_dt <= now:
                    continue
                n_not_expired += 1

                # ── 3. 5-minute format (primary discriminator) ────────────────
                # _is_5min_question() checks:
                #   a) "up or down" or "higher or lower" in question
                #   b) parses the embedded time range (e.g. "9:45AM-9:50AM") to confirm
                #      a 5-minute window — this is what separates 5-min from 15-min markets.
                #      Both use "up or down"; time range parsing is the only true discriminator.
                #
                # NOTE: startDate is the market CREATION time, not the window start.
                #   endDate - startDate ≈ 24h, not 5 min. The old window check is wrong.
                if not _is_5min_question(question):
                    n_not_5min += 1
                    if len(not_5min_samples) < 5:
                        not_5min_samples.append(question[:70])
                    continue

                n_5min_format += 1

                # Collect for cross-page diagnosis (up to 10)
                if len(all_5min_questions) < 10:
                    all_5min_questions.append(question[:80])

                # ── 4. Crypto asset in question ───────────────────────────────
                if not _is_crypto_question(question):
                    n_not_crypto += 1
                    if len(not_crypto_samples) < 5:
                        not_crypto_samples.append(question[:70])
                    continue
                n_crypto += 1

                # ── 5. Parse + add ────────────────────────────────────────────
                market = self._parse_market(m, end_dt)
                if market:
                    n_added += 1
                    all_markets.append(market)
                    if len(added_samples) < 5:
                        added_samples.append(question[:70])

            total_5min_across_pages += n_5min_format

            # ── Per-page funnel log ───────────────────────────────────────────
            log.info(
                "[GAMMA] page offset=%d: %d total | %d binary | %d not-expired"
                " | %d 5min-format | %d not-5min | %d crypto | %d not-crypto | %d added",
                offset, n_total, n_binary, n_not_expired,
                n_5min_format, n_not_5min, n_crypto, n_not_crypto, n_added,
            )
            if not_5min_samples:
                log.debug("[GAMMA] not-5min-format samples (dropped): %s", not_5min_samples)
            if not_crypto_samples:
                log.info("[GAMMA] 5min but not-crypto samples (dropped):\n%s",
                         "\n".join(f"  • {q}" for q in not_crypto_samples))
            if added_samples:
                log.info("[GAMMA] added: %s", added_samples)

            if len(page) < limit:
                break

            # Early exit: since we sort by endDate ascending, once the last market
            # on this page is past our tracking horizon we won't find anything useful.
            now_check = datetime.now(timezone.utc)
            cutoff_check = now_check + timedelta(seconds=tracking_horizon_s)
            last_end = self._parse_dt(page[-1].get("endDate") or "")
            if last_end and last_end > cutoff_check:
                log.info(
                    "[GAMMA] Early exit at offset=%d — last endDate %s > horizon cutoff %s",
                    offset, last_end.strftime("%H:%M"), cutoff_check.strftime("%H:%M"),
                )
                break

            offset += limit

        # ── Post-loop diagnostics ─────────────────────────────────────────────

        if total_5min_across_pages == 0:
            log.warning(
                "[GAMMA] No active 5-minute crypto markets found. "
                "Polymarket pre-creates next-session markets after ~13:00 UTC daily. "
                "If it is before that time, markets may not yet exist. "
                "Bot will retry in %ds.",
                self.config.market_refresh_interval,
            )
        else:
            log.info(
                "[GAMMA] 5-minute markets observed: %d across all pages. "
                "First %d questions:\n%s",
                total_5min_across_pages,
                len(all_5min_questions),
                "\n".join(f"  {i+1}. {q}" for i, q in enumerate(all_5min_questions)),
            )
            if not all_markets:
                log.warning(
                    "[GAMMA] %d 5-minute format markets found but NONE passed the "
                    "crypto asset filter. The question format may have changed. "
                    "Check the 5-min questions above to identify the pattern.",
                    total_5min_across_pages,
                )

        # ── Coin filter: top-N by liquidity ──────────────────────────────────
        pre_coin_count = len(all_markets)
        all_markets = self._filter_top_coins(all_markets)

        # ── Horizon filter: only track markets expiring soon ──────────────────
        # We only need markets expiring within: entry_window + upcoming + 2×refresh.
        # This keeps the WS subscription list tiny (≤ ~30 tokens) instead of 4000+,
        # preventing "INVALID OPERATION" errors from Polymarket WS batch limits.
        tracking_horizon_s = (
            self.config.max_seconds_to_expiry       # 120s  — entry window
            + self.config.upcoming_window_seconds   # 600s  — "coming soon" view
            + self.config.market_refresh_interval * 2  # 120s  — buffer for refresh lag
        )
        now_for_filter = datetime.now(timezone.utc)
        cutoff = now_for_filter + timedelta(seconds=tracking_horizon_s)
        pre_horizon = len(all_markets)
        all_markets = [
            m for m in all_markets
            if m.end_date and m.end_date <= cutoff
        ]
        log.info(
            "[GAMMA] Fetched %d Up-or-Down markets (top %d coins, horizon=%ds). "
            "Before coin filter: %d | before horizon filter: %d",
            len(all_markets), TOP_N_COINS, tracking_horizon_s,
            pre_coin_count, pre_horizon,
        )
        return all_markets

    # ─────────────────────────────────────────────────────────────────────────

    async def _log_diagnostic_sample(
        self, session: aiohttp.ClientSession, markets_url: str
    ) -> None:
        """
        Fallback: fetch /markets?limit=20 (no active/closed filter) and log
        the first 10 questions so we can see the actual question format.
        """
        diag_params = {"limit": 20}
        diag_url = markets_url + "?" + "&".join(f"{k}={v}" for k, v in diag_params.items())
        log.info("[GAMMA][DIAG] Fetching unfiltered sample: GET %s", diag_url)
        try:
            async with session.get(markets_url, params=diag_params) as resp:
                if resp.status != 200:
                    log.warning("[GAMMA][DIAG] HTTP %s — cannot retrieve sample", resp.status)
                    return
                raw = await resp.json()
                page: list = raw if isinstance(raw, list) else (raw or {}).get("markets", [])
                questions = [m.get("question", "?")[:80] for m in page[:10]]
                log.info(
                    "[GAMMA][DIAG] /markets (no filter) returned %d markets. "
                    "First %d questions:\n%s",
                    len(page),
                    len(questions),
                    "\n".join(f"  {i+1}. {q}" for i, q in enumerate(questions)),
                )
        except Exception as e:
            log.warning("[GAMMA][DIAG] diagnostic request failed: %s", e)

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _coin_name(question: str) -> str:
        q = question.lower()
        for kw in _COIN_KEYWORDS:
            if kw.lower() in q:
                return kw
        return question.split()[0] if question else "Unknown"

    def _filter_top_coins(self, markets: list) -> list:
        coin_liq: dict[str, float] = defaultdict(float)
        for m in markets:
            coin = self._coin_name(m.question)
            if coin in _EXCLUDED_COINS:
                continue
            coin_liq[coin] += m.liquidity

        top_coins = {
            coin for coin, _ in
            sorted(coin_liq.items(), key=lambda x: -x[1])[:TOP_N_COINS]
        }
        log.info("Top %d coins by liquidity: %s",
                 TOP_N_COINS, ", ".join(sorted(top_coins)) or "(none)")
        return [m for m in markets if self._coin_name(m.question) in top_coins]

    @staticmethod
    def _parse_json_field(val) -> list:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return []
        return val or []

    def _parse_market(self, data: dict, end_date: datetime) -> Optional[Market]:
        try:
            token_ids  = self._parse_json_field(data.get("clobTokenIds"))
            outcomes   = self._parse_json_field(data.get("outcomes"))
            prices_raw = self._parse_json_field(data.get("outcomePrices"))

            if len(token_ids) < 2:
                return None

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

            yes_token = Token(token_id=token_ids[yes_idx], outcome="Yes", price=safe_price(yes_idx))
            no_token  = Token(token_id=token_ids[no_idx],  outcome="No",  price=safe_price(no_idx))
            liquidity = float(data.get("liquidityClob") or data.get("liquidity") or 0)

            return Market(
                id=str(data.get("id") or data.get("conditionId") or ""),
                question=data.get("question") or "",
                slug=data.get("slug") or "",
                yes_token=yes_token,
                no_token=no_token,
                end_date=end_date,
                start_date=self._parse_dt(data.get("startDate")),
                liquidity=liquidity,
                active=bool(data.get("active", True)),
            )
        except Exception as e:
            log.debug("[GAMMA] Failed to parse market: %s", e)
            return None

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def fetch_market_resolution(self, market_id: str) -> Optional[str]:
        """
        Получить реальный исход рынка после его закрытия.
        Returns "YES" / "NO" if resolved, None otherwise.
        """
        session = await self._get_session()
        try:
            url = f"{self.config.gamma_url}/markets"
            async with session.get(url, params={"id": market_id}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                markets = data if isinstance(data, list) else [data]
                if not markets:
                    return None
                m = markets[0]

                prices_raw = self._parse_json_field(m.get("outcomePrices"))
                outcomes   = self._parse_json_field(m.get("outcomes"))

                yes_idx = 0
                for i, outcome in enumerate(outcomes):
                    if str(outcome).strip().lower() in ("yes", "up"):
                        yes_idx = i

                if yes_idx < len(prices_raw):
                    try:
                        yes_price = float(prices_raw[yes_idx])
                        if yes_price >= 0.99:
                            return "YES"
                        if yes_price <= 0.01:
                            return "NO"
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            log.debug("Failed to fetch resolution for %s: %s", market_id, e)
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
