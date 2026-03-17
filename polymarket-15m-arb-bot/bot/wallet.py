"""
Wallet module — loads a Polymarket wallet and signs CLOB orders.

Responsibilities:
    • Load private key from Config
    • Initialise an eth_account Account object
    • Expose the effective wallet address
    • sign_order() — EIP-712 stub; wired up by the live executor

Usage:
    from bot.wallet import Wallet, WalletError
    wallet = Wallet(config)
    print(wallet.address())
    signed = wallet.sign_order(order_data)
"""

import logging
from typing import Optional

from .config import Config

log = logging.getLogger(__name__)


class WalletError(Exception):
    """Raised when wallet initialisation or signing fails."""


def derive_api_credentials(config: Config) -> dict:
    """
    Create or derive Polymarket CLOB API credentials from the configured private key.

    Equivalent to the JS SDK's ``client.createOrDeriveApiKey()``:

        const credentials = await client.createOrDeriveApiKey();
        // { apiKey, secret, passphrase }

    Makes one POST request to the CLOB API — no orders are placed.
    The returned credentials can be saved to .env as:
        POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE

    Args:
        config: Bot config with polymarket_private_key and clob_url set.

    Returns:
        {"api_key": str, "api_secret": str, "api_passphrase": str}

    Raises:
        WalletError: if the private key is missing, libraries are not installed,
                     or the API call fails.
    """
    if not config.polymarket_private_key:
        raise WalletError(
            "POLYMARKET_PRIVATE_KEY is required to derive API credentials."
        )

    try:
        from py_clob_client.client import ClobClient as _SyncClient  # type: ignore[import]
    except ImportError as exc:
        raise WalletError(
            "py-clob-client is not installed. Run: pip install py-clob-client"
        ) from exc

    try:
        client = _SyncClient(
            host=config.clob_url,
            key=config.polymarket_private_key,
            chain_id=137,
        )
        creds = client.create_or_derive_api_creds()
    except WalletError:
        raise
    except Exception as exc:
        raise WalletError(f"Failed to derive credentials from CLOB API: {exc}") from exc

    return {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.api_passphrase,
    }


class Wallet:
    """
    Thin wrapper around an eth_account Account for Polymarket CLOB signing.

    Requires POLYMARKET_PRIVATE_KEY in .env (or config.polymarket_private_key).
    Optionally reads POLYMARKET_WALLET_ADDRESS as the on-chain address
    (falls back to the address derived from the private key).
    POLYMARKET_PROXY_ADDRESS is stored for use by the live executor when the
    account operates through a Polymarket proxy/gnosis safe.
    """

    def __init__(self, config: Config) -> None:
        if not config.polymarket_private_key:
            raise WalletError(
                "LIVE mode requires wallet configuration. "
                "Set POLYMARKET_PRIVATE_KEY in .env"
            )

        try:
            from eth_account import Account  # type: ignore[import]
        except ImportError as exc:
            raise WalletError(
                "eth-account is not installed. Run: pip install eth-account"
            ) from exc

        try:
            self._account = Account.from_key(config.polymarket_private_key)
        except Exception as exc:
            raise WalletError(f"Invalid private key: {exc}") from exc

        # Prefer the explicit address from config (proxy / gnosis safe scenario);
        # fall back to the address derived from the key.
        self._address: str = (
            config.polymarket_wallet_address or self._account.address
        )
        self._proxy_address: Optional[str] = config.polymarket_proxy_address or None

        log.info(
            "Wallet loaded | address=%s%s",
            self._address,
            f" | proxy={self._proxy_address}" if self._proxy_address else "",
        )

    # ──────────────────────────────────────────────────────────────────────────

    def address(self) -> str:
        """Return the effective on-chain wallet address."""
        return self._address

    def proxy_address(self) -> Optional[str]:
        """Return the proxy address if configured, else None."""
        return self._proxy_address

    def sign_order(self, order_data: dict) -> dict:
        """
        Sign a Polymarket CLOB order and return it with the signature attached.

        NOTE: In the current execution layer, EIP-712 order signing is handled
        internally by py_clob_client.ClobClient.create_and_post_order() — it
        receives the private key at construction time and signs every order
        before posting it to the CLOB REST API.  This method exists as the
        explicit signing interface for use cases that need raw signed payloads
        (e.g. batch orders, custom routing) and will delegate to py_clob_client
        when implemented.

        Args:
            order_data: Raw order dict as expected by the CLOB API.

        Returns:
            order_data with 'signature' key added.

        Raises:
            NotImplementedError: until raw signing is needed outside ClobClient.
        """
        raise NotImplementedError(
            "Direct sign_order() is not yet needed — py_clob_client handles "
            "EIP-712 signing internally via ClobClient.create_and_post_order(). "
            "Implement here when raw signed payloads are required."
        )

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """Return True if eth_account is installed and wallet can be created."""
        try:
            import eth_account  # noqa: F401  # type: ignore[import]
            return True
        except ImportError:
            return False

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def verify_credentials(config: Config) -> dict:
        """
        Verify all trading credentials without placing any orders.

        Checks:
        1. Required credential fields are present in config.
        2. eth_account library is importable.
        3. Private key parses as a valid Ethereum key.
        4. py_clob_client is importable and ClobClient can be instantiated
           (constructor only — no network call).

        Returns a dict:
            {
                "ready":           bool,         # True if ALL checks pass
                "missing":         list[str],    # names of missing required fields
                "configured":      list[str],    # names of present fields
                "wallet_address":  str | None,
                "proxy_address":   str | None,
                "key_valid":       bool,         # private key parsed OK
                "clob_init_ok":    bool,         # ClobClient constructor succeeded
                "clob_init_error": str | None,   # error message if constructor failed
            }
        """
        required_fields = {
            "POLYMARKET_PRIVATE_KEY":  config.polymarket_private_key,
            "POLYMARKET_WALLET_ADDRESS": config.polymarket_wallet_address,
            "POLY_API_KEY":            config.poly_api_key,
            "POLY_API_SECRET":         config.poly_api_secret,
            "POLY_API_PASSPHRASE":     config.poly_api_passphrase,
        }

        missing    = [k for k, v in required_fields.items() if not v]
        configured = [k for k, v in required_fields.items() if v]

        result: dict = {
            "ready":           False,
            "missing":         missing,
            "configured":      configured,
            "wallet_address":  config.polymarket_wallet_address or None,
            "proxy_address":   config.polymarket_proxy_address or None,
            "key_valid":       False,
            "clob_init_ok":    False,
            "clob_init_error": None,
        }

        # ── 1. Private key validation ──────────────────────────────────────
        if not config.polymarket_private_key:
            result["clob_init_error"] = "POLYMARKET_PRIVATE_KEY not set"
            return result

        try:
            from eth_account import Account  # type: ignore[import]
            Account.from_key(config.polymarket_private_key)
            result["key_valid"] = True
        except ImportError:
            result["clob_init_error"] = "eth-account not installed"
            return result
        except Exception as exc:
            result["clob_init_error"] = f"Invalid private key: {exc}"
            return result

        # ── 2. CLOB client constructor (no network calls) ─────────────────
        try:
            from py_clob_client.client import ClobClient as _SyncClient  # type: ignore[import]
            from py_clob_client.clob_types import ApiCreds               # type: ignore[import]

            creds = None
            if config.poly_api_key:
                creds = ApiCreds(
                    api_key=config.poly_api_key,
                    api_secret=config.poly_api_secret or "",
                    api_passphrase=config.poly_api_passphrase or "",
                )

            _SyncClient(
                host=config.clob_url,
                key=config.polymarket_private_key,
                creds=creds,
                chain_id=137,
            )
            result["clob_init_ok"] = True
        except ImportError:
            result["clob_init_error"] = "py-clob-client not installed"
            return result
        except Exception as exc:
            result["clob_init_error"] = f"ClobClient init failed: {exc}"
            return result

        result["ready"] = len(missing) == 0
        return result
