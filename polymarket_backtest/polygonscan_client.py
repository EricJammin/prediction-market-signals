"""
PolygonscanClient: looks up on-chain wallet age via Polygonscan API.

Wallet age = timestamp of the wallet's first-ever transaction on Polygon.
This is the ground truth for "burner account" detection — far more reliable
than checking our limited trade dataset.

Caching: results are persisted to data/wallet_ages.json so each wallet is
looked up at most once across all backtest runs.

Usage:
    client = PolygonscanClient()
    age_days = client.wallet_age_days(wallet_address, as_of_timestamp)
    # Returns None if lookup fails or wallet has no transactions.
    # Returns 0 if wallet was created on the same day.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# Etherscan V2 unified API — chainid=137 targets Polygon
POLYGONSCAN_API = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID = 137
CACHE_PATH = Path("data/wallet_ages.json")

# Etherscan free tier: 3 calls/sec. We make 2 calls per wallet.
# Per-call delay of 0.38s ≈ 2.6 calls/sec — safely under the limit.
_DELAY_WITH_KEY = 0.38
_DELAY_WITHOUT_KEY = 1.1   # ~1/sec without key


class PolygonscanClient:
    def __init__(self) -> None:
        self._api_key = os.getenv("POLYGONSCAN_API_KEY", "")
        self._delay = _DELAY_WITH_KEY if self._api_key else _DELAY_WITHOUT_KEY
        self._cache: dict[str, int | None] = self._load_cache()
        self._last_call = 0.0

        if not self._api_key:
            logger.warning(
                "POLYGONSCAN_API_KEY not set — using unauthenticated calls (~1 req/sec). "
                "Set key in .env for 5x faster lookups."
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def wallet_age_days(self, wallet: str, as_of_timestamp: int) -> float | None:
        """
        Return how many days old the wallet was at `as_of_timestamp`.

        Returns:
            float   — age in days (can be fractional; 0.0 means same-day creation)
            None    — lookup failed or wallet has no transactions
        """
        first_tx = self.first_transaction_timestamp(wallet)
        if first_tx is None:
            return None
        age_seconds = max(0, as_of_timestamp - first_tx)
        return age_seconds / 86400.0

    def first_transaction_timestamp(self, wallet: str) -> int | None:
        """
        Return Unix timestamp of wallet's first transaction on Polygon.
        Result is cached; returns None on API failure.
        """
        wallet = wallet.lower()
        if wallet in self._cache:
            return self._cache[wallet]

        ts = self._fetch_first_tx(wallet)
        self._cache[wallet] = ts
        self._save_cache()
        return ts

    def prefetch(self, wallets: list[str]) -> None:
        """
        Batch-fetch first-transaction timestamps for all wallets not yet cached.
        Useful to populate cache before running the backtest.
        """
        needed = [w.lower() for w in wallets if w.lower() not in self._cache]
        if not needed:
            logger.info("All %d wallets already cached", len(wallets))
            return

        logger.info("Fetching wallet ages for %d wallets...", len(needed))
        for i, wallet in enumerate(needed):
            ts = self._fetch_first_tx(wallet)
            self._cache[wallet] = ts
            if (i + 1) % 50 == 0:
                self._save_cache()
                logger.info("  %d / %d complete", i + 1, len(needed))

        self._save_cache()
        logger.info("Prefetch complete. %d wallets cached.", len(self._cache))

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_first_tx(self, wallet: str) -> int | None:
        """
        Query Etherscan V2 (Polygon) for the earliest transaction of this wallet.

        Polymarket wallets are proxy contracts created via internal transactions,
        so we check both regular txs and internal txs and return the minimum.
        Returns the Unix timestamp, or None on failure.
        """
        ts_regular = self._query_action(wallet, "txlist")
        ts_internal = self._query_action(wallet, "txlistinternal")

        candidates = [ts for ts in [ts_regular, ts_internal] if ts is not None]
        return min(candidates) if candidates else None

    def _query_action(self, wallet: str, action: str) -> int | None:
        """Single Etherscan API call for one action type; returns first tx timestamp."""
        self._rate_limit()

        params: dict = {
            "chainid": POLYGON_CHAIN_ID,
            "module": "account",
            "action": action,
            "address": wallet,
            "startblock": 0,
            "endblock": 99999999,
            "sort": "asc",
            "page": 1,
            "offset": 1,
        }
        if self._api_key:
            params["apikey"] = self._api_key

        try:
            resp = requests.get(POLYGONSCAN_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Etherscan request failed for %s (%s): %s", wallet, action, exc)
            return None

        status = data.get("status")
        result = data.get("result", [])

        if status == "0":
            msg = data.get("message", "")
            if "rate limit" in msg.lower():
                # Back off and retry once
                logger.debug("Rate limited on %s (%s), retrying after 2s", wallet, action)
                time.sleep(2.0)
                return self._query_action(wallet, action)
            # Normal case: wallet has no transactions of this type
            return None

        if not result or not isinstance(result, list):
            return None

        try:
            return int(result[0]["timeStamp"])
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("Could not parse timestamp for %s (%s): %s", wallet, action, exc)
            return None

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_call = time.time()

    def _load_cache(self) -> dict[str, int | None]:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text())
            except Exception:
                logger.warning("Could not load wallet age cache; starting fresh.")
        return {}

    def _save_cache(self) -> None:
        CACHE_PATH.write_text(json.dumps(self._cache, indent=2))
