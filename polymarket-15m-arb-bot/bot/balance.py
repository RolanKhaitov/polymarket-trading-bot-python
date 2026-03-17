"""
Async wallet balance fetcher — USDC.e and POL (native) on Polygon.

Uses raw JSON-RPC batch call — no extra dependencies beyond aiohttp.
Called in a background task every BALANCE_REFRESH_INTERVAL seconds;
never blocks the event loop or scanner hot path.

Contracts (Polygon mainnet):
    USDC.e  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174  (6 decimals)
    POL     native token                                 (18 decimals)
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Public Polygon RPC endpoints — tried in order if one fails
_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://matic-mainnet.chainstacklabs.com",
]

_USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_BALANCE_OF_SELECTOR = "0x70a08231"   # keccak256("balanceOf(address)")[:4]

BALANCE_REFRESH_INTERVAL = 30   # секунд между обновлениями


def _encode_balance_of(address: str) -> str:
    """Encode ERC20 balanceOf(address) call data."""
    addr_hex = address.lower().removeprefix("0x").zfill(64)
    return f"{_BALANCE_OF_SELECTOR}{addr_hex}"


async def fetch_balances(address: str) -> dict[str, float]:
    """
    Fetch USDC.e and POL balances for address on Polygon mainnet.

    Sends a single JSON-RPC batch request (2 calls in one HTTP round-trip).
    Falls back to backup RPCs if the primary fails.

    Returns:
        {"usdc": float, "pol": float}

    Raises:
        RuntimeError if all RPC endpoints fail.
    """
    import aiohttp

    payload = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBalance",
            "params": [address, "latest"],
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_call",
            "params": [
                {
                    "to": _USDC_E_CONTRACT,
                    "data": _encode_balance_of(address),
                },
                "latest",
            ],
        },
    ]

    last_exc: Optional[Exception] = None
    for rpc_url in _RPC_URLS:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    rpc_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json(content_type=None)

            by_id = {item["id"]: item for item in data}

            pol_hex  = by_id[1].get("result", "0x0")
            usdc_hex = by_id[2].get("result", "0x0")

            pol  = int(pol_hex,  16) / 10 ** 18
            usdc = int(usdc_hex, 16) / 10 ** 6

            return {"usdc": usdc, "pol": pol}

        except Exception as exc:
            last_exc = exc
            log.debug("RPC %s failed: %s", rpc_url, exc)
            continue

    raise RuntimeError(f"All Polygon RPC endpoints failed: {last_exc}") from last_exc
