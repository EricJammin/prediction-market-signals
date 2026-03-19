"""
Signal A: live burner wallet detection.

On each poll, new trades are passed to ingest_trades(). For each BUY trade
above SIGNAL_A_MIN_SINGLE_TRADE_USDC, the wallet's cumulative position in
(market, side) is updated in SQLite and scored against 5 criteria.

Criteria (4 of 5 required to fire):
  1. freshness       — wallet on-chain age <= SIGNAL_A_BURNER_AGE_DAYS (14d)
                       (Polygonscan lookup, cached; fallback = dataset-relative age)
  2. size            — cumulative BUY >= SIGNAL_A_SIZE_THRESHOLD_USDC ($15K)
  3. concentration   — this (market, side) is >= 70% of wallet's total buy volume
  4. entry_price     — first buy price is in informational zone [0.10, 0.50]
  5. not_wash_trader — sells < 20% of buys (no round-trip detected)

State is persisted in wallet_positions table (see state.py).
Wallet ages are cached in WALLET_AGE_CACHE_PATH (data/wallet_ages.json).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from state import StateDB

logger = logging.getLogger(__name__)

# Etherscan V2 unified endpoint with Polygon chain ID
_POLYGONSCAN_API = "https://api.etherscan.io/v2/api"
_POLYGON_CHAIN_ID = 137
# Rate-limit delays: Etherscan free tier allows 3 calls/sec; we make 2 per wallet.
_DELAY_WITH_KEY    = 0.40   # ~2.5 calls/sec → safe under 3/sec limit
_DELAY_WITHOUT_KEY = 1.10   # ~0.9 calls/sec (unauthenticated)


@dataclass
class SignalAEvent:
    market_id: str
    wallet: str
    side: str                   # "YES" or "NO"
    trigger_ts: int             # timestamp of the trade that pushed criteria over threshold
    first_trade_ts: int         # timestamp of wallet's first observed BUY in this market
    first_buy_price: float      # price of the first BUY (entry price criterion)
    cumulative_buy_usdc: float
    cumulative_sell_usdc: float
    wallet_age_days: float | None   # None = Polygonscan unavailable
    criteria_met: dict[str, bool] = field(default_factory=dict)
    n_criteria: int = 0
    yes_price: float | None = None   # current market YES price at alert time
    question: str = ""               # filled by caller from market metadata
    slug: str = ""                   # filled by caller from market metadata


class SignalA:
    def __init__(self, db: StateDB) -> None:
        self._db = db
        self._api_key = os.getenv("POLYGONSCAN_API_KEY", "")
        self._delay = _DELAY_WITH_KEY if self._api_key else _DELAY_WITHOUT_KEY
        self._last_call = 0.0
        self._age_cache = self._load_age_cache()

        if not self._api_key:
            logger.warning(
                "POLYGONSCAN_API_KEY not set — wallet age lookups will be slow "
                "and may miss burner wallets. Set key in .env."
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest_trades(
        self,
        market_id: str,
        trades: list[dict],
        yes_token_id: str = "",
        no_token_id: str = "",
    ) -> list[SignalAEvent]:
        """
        Process new trades for a market. Updates wallet positions in the DB
        and returns a list of newly-fired SignalA events (empty if none).

        trades should be the same raw dicts from data-api.polymarket.com as
        passed to signal_c.ingest_trades() — they share the same fetch result.
        """
        if not trades:
            return []

        events: list[SignalAEvent] = []

        # data-api returns newest-first; process oldest-first so cumulative state
        # builds in chronological order and the trigger_ts is accurate.
        sorted_trades = sorted(trades, key=lambda t: _parse_ts(t) or 0)

        for trade in sorted_trades:
            event = self._process_trade(market_id, trade, yes_token_id, no_token_id)
            if event is not None:
                events.append(event)

        return events

    # ── Trade processing ───────────────────────────────────────────────────────

    def _process_trade(
        self,
        market_id: str,
        trade: dict,
        yes_token_id: str,
        no_token_id: str,
    ) -> SignalAEvent | None:
        ts = _parse_ts(trade)
        if ts is None:
            return None

        size = _safe_float(trade.get("size") or trade.get("amount") or 0)
        if size < config.SIGNAL_A_MIN_SINGLE_TRADE_USDC:
            return None

        price = _safe_float(trade.get("price") or 0)
        if price <= 0:
            return None

        wallet = (trade.get("proxyWallet") or "").lower()
        if not wallet:
            return None

        taker_side = (trade.get("side") or "").upper()   # "BUY" or "SELL"
        asset_id    = trade.get("asset") or trade.get("asset_id") or ""
        outcome     = trade.get("outcome") or ""
        outcome_side = _resolve_side(asset_id, outcome, yes_token_id, no_token_id)
        if outcome_side is None:
            return None

        is_buy = (taker_side == "BUY")

        # Don't re-fire on a (wallet, market, side) that already triggered
        if self._db.was_signal_a_fired(wallet, market_id, outcome_side):
            return None

        # Update the persisted position (both buys and sells tracked)
        self._db.update_wallet_position(
            wallet=wallet,
            market_id=market_id,
            side=outcome_side,
            buy_delta=size if is_buy else 0.0,
            sell_delta=0.0 if is_buy else size,
            first_buy_price=price if is_buy else None,
            first_trade_ts=ts if is_buy else None,
        )

        # Only evaluate Signal A on BUY-side trades
        if not is_buy:
            return None

        pos = self._db.get_wallet_position(wallet, market_id, outcome_side)
        if pos is None:
            return None

        return self._score_and_maybe_fire(wallet, market_id, outcome_side, pos, ts)

    # ── Criteria scoring ───────────────────────────────────────────────────────

    def _score_and_maybe_fire(
        self,
        wallet: str,
        market_id: str,
        side: str,
        pos: dict,
        trigger_ts: int,
    ) -> SignalAEvent | None:
        buy_usdc   = pos["buy_usdc"]
        sell_usdc  = pos["sell_usdc"]
        first_price = pos.get("first_buy_price") or 0.0
        first_ts    = pos.get("first_trade_ts") or trigger_ts

        # ── 4 cheap criteria (no network call) ────────────────────────────────
        size_ok = buy_usdc >= config.SIGNAL_A_SIZE_THRESHOLD_USDC

        total_buy   = self._db.get_wallet_total_buy_usdc(wallet)
        concentration_ok = (
            total_buy > 0
            and buy_usdc / total_buy >= config.SIGNAL_A_CONCENTRATION_MIN
        )

        entry_price_ok = (
            config.SIGNAL_A_ENTRY_PRICE_MIN <= first_price <= config.SIGNAL_A_ENTRY_PRICE_MAX
        )

        not_wash_ok = (
            sell_usdc < buy_usdc * config.SIGNAL_A_WASH_TRADE_MAX_SELL
        ) if buy_usdc > 0 else True

        cheap_count = sum([size_ok, concentration_ok, entry_price_ok, not_wash_ok])

        # Only look up wallet age if we could plausibly reach MIN_CRITERIA with it.
        # If cheap_count < MIN_CRITERIA - 1, even a passing age won't be enough.
        if cheap_count >= config.SIGNAL_A_MIN_CRITERIA - 1:
            age_days = self._get_wallet_age(wallet, first_ts)
            freshness_ok = self._eval_freshness(age_days, first_ts, trigger_ts)
        else:
            age_days = None
            freshness_ok = False

        criteria: dict[str, bool] = {
            "freshness":       freshness_ok,
            "size":            size_ok,
            "concentration":   concentration_ok,
            "entry_price":     entry_price_ok,
            "not_wash_trader": not_wash_ok,
        }
        n_met = sum(criteria.values())

        if n_met < config.SIGNAL_A_MIN_CRITERIA:
            return None

        # Mark fired before returning so repeat trades don't re-trigger
        self._db.mark_wallet_signal_a_fired(wallet, market_id, side)

        yes_price, _ = self._db.get_price(market_id)

        return SignalAEvent(
            market_id=market_id,
            wallet=wallet,
            side=side,
            trigger_ts=trigger_ts,
            first_trade_ts=first_ts,
            first_buy_price=first_price,
            cumulative_buy_usdc=buy_usdc,
            cumulative_sell_usdc=sell_usdc,
            wallet_age_days=age_days,
            criteria_met=criteria,
            n_criteria=n_met,
            yes_price=yes_price,
        )

    @staticmethod
    def _eval_freshness(
        age_days: float | None,
        first_trade_ts: int,
        trigger_ts: int,
    ) -> bool:
        if age_days is not None:
            return age_days <= config.SIGNAL_A_BURNER_AGE_DAYS
        # Polygonscan unavailable — fall back to dataset-relative age:
        # if the wallet's first observed BUY is within BURNER_AGE_DAYS of the trigger
        fallback_days = (trigger_ts - first_trade_ts) / 86_400
        return fallback_days <= config.SIGNAL_A_BURNER_AGE_DAYS

    # ── Polygonscan wallet age ─────────────────────────────────────────────────

    def _get_wallet_age(self, wallet: str, as_of_ts: int) -> float | None:
        """
        Return wallet age in days at `as_of_ts`. Caches results to avoid
        repeated Polygonscan calls for the same wallet.
        """
        wallet = wallet.lower()
        if wallet in self._age_cache:
            first_tx = self._age_cache[wallet]
            if first_tx is None:
                return None
            return max(0, as_of_ts - first_tx) / 86_400

        first_tx = self._fetch_first_tx(wallet)
        self._age_cache[wallet] = first_tx
        self._save_age_cache()

        if first_tx is None:
            return None
        return max(0, as_of_ts - first_tx) / 86_400

    def _fetch_first_tx(self, wallet: str) -> int | None:
        """Query Etherscan V2 (Polygon) for earliest tx. Returns Unix timestamp or None."""
        ts_regular  = self._query_etherscan(wallet, "txlist")
        ts_internal = self._query_etherscan(wallet, "txlistinternal")
        candidates  = [ts for ts in [ts_regular, ts_internal] if ts is not None]
        return min(candidates) if candidates else None

    def _query_etherscan(self, wallet: str, action: str) -> int | None:
        self._rate_limit()
        params: dict[str, Any] = {
            "chainid":    _POLYGON_CHAIN_ID,
            "module":     "account",
            "action":     action,
            "address":    wallet,
            "startblock": 0,
            "endblock":   99_999_999,
            "sort":       "asc",
            "page":       1,
            "offset":     1,
        }
        if self._api_key:
            params["apikey"] = self._api_key

        try:
            resp = requests.get(_POLYGONSCAN_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Etherscan %s failed for %s: %s", action, wallet[:12], exc)
            return None

        status = data.get("status")
        result = data.get("result", [])

        if status == "0":
            msg = data.get("message", "").lower()
            if "rate limit" in msg:
                logger.debug("Rate-limited on %s, retrying after 2s", wallet[:12])
                time.sleep(2.0)
                return self._query_etherscan(wallet, action)
            return None   # no transactions of this type

        if not result or not isinstance(result, list):
            return None

        try:
            return int(result[0]["timeStamp"])
        except (KeyError, ValueError, IndexError):
            return None

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_call = time.time()

    # ── Wallet age cache I/O ───────────────────────────────────────────────────

    def _load_age_cache(self) -> dict[str, int | None]:
        path = Path(config.WALLET_AGE_CACHE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                logger.warning("Could not load wallet age cache; starting fresh.")
        return {}

    def _save_age_cache(self) -> None:
        Path(config.WALLET_AGE_CACHE_PATH).write_text(
            json.dumps(self._age_cache, indent=2)
        )


# ── Helpers (shared with signal_c.py pattern) ─────────────────────────────────

def _parse_ts(trade: dict) -> int | None:
    for key in ("timestamp", "match_time", "matchTime", "created_at", "createdAt"):
        val = trade.get(key)
        if val is not None:
            try:
                return int(float(val))
            except (ValueError, TypeError):
                pass
    return None


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _resolve_side(
    asset_id: str,
    outcome_field: str,
    yes_token_id: str,
    no_token_id: str,
) -> str | None:
    if yes_token_id and asset_id == yes_token_id:
        return "YES"
    if no_token_id and asset_id == no_token_id:
        return "NO"
    upper = outcome_field.strip().upper()
    if upper in ("YES", "TRUE"):
        return "YES"
    if upper in ("NO", "FALSE"):
        return "NO"
    return None
