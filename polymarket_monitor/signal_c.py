"""
Signal C: live volume surge detection with SQLite-backed rolling state.

Unlike the backtest (which processes historical data in a single pass), this
module maintains rolling hourly volume state incrementally across polls.

Key design:
  - fetch_trades_since(): fetches only NEW trades since last poll (incremental)
  - ingest_trades(): buckets trades into hourly volumes in SQLite
  - detect_surge(): checks current hour against 7-day rolling baseline

Trade ordering note: data-api.polymarket.com returns trades newest-first.
fetch_trades_since() exploits this by stopping as soon as it sees a trade
older than since_ts — no need to scan all pages on every poll.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from state import StateDB

logger = logging.getLogger(__name__)


@dataclass
class SurgeEvent:
    market_id: str
    hour_ts: int            # start of surge hour bucket (Unix seconds, floored to 3600)
    surge_volume_usdc: float
    baseline_volume_usdc: float
    surge_ratio: float      # surge_volume / baseline
    signal_c_score: float   # 0.5 (3–5×) or 1.0 (>5×)
    yes_price: float | None
    no_price: float | None


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=config.RETRY_BACKOFF_SECONDS,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_timestamp(trade: dict) -> int | None:
    for field in ("timestamp", "match_time", "matchTime", "created_at", "createdAt"):
        val = trade.get(field)
        if val is not None:
            try:
                return int(float(val))
            except (ValueError, TypeError):
                pass
    return None


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


class SignalC:
    def __init__(self, db: StateDB) -> None:
        self._db = db
        self._session = _make_session()

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest_trades(
        self,
        market_id: str,
        trades: list[dict],
        yes_token_id: str = "",
        no_token_id: str = "",
    ) -> int:
        """
        Parse raw trade dicts, bucket by hour, upsert into hourly_volumes.
        Also updates price_history with the latest YES/NO prices seen.
        Returns the maximum trade timestamp seen (0 if trades is empty).
        """
        if not trades:
            return 0

        latest_yes_price: float | None = None
        latest_no_price: float | None = None
        # Track per-side timestamps separately so a later NO trade doesn't
        # block a subsequent YES price update (and vice versa).
        latest_yes_ts = 0
        latest_no_ts  = 0
        latest_ts = 0

        # Accumulate volume deltas per hour bucket before writing
        hour_deltas: dict[int, float] = {}

        for t in trades:
            ts = _parse_timestamp(t)
            if ts is None:
                continue
            size = _safe_float(t.get("size") or t.get("matched_amount") or t.get("amount"))
            if size < config.MIN_TRADE_SIZE_USDC:
                continue

            price = _safe_float(t.get("price"))
            asset_id = t.get("asset_id") or t.get("assetId") or t.get("asset") or ""
            outcome_field = t.get("outcome") or ""
            side = _resolve_side(asset_id, outcome_field, yes_token_id, no_token_id)

            hour_ts = (ts // config.SURGE_WINDOW_SECONDS) * config.SURGE_WINDOW_SECONDS
            hour_deltas[hour_ts] = hour_deltas.get(hour_ts, 0.0) + size

            if side == "YES" and price > 0 and ts > latest_yes_ts:
                latest_yes_price = price
                latest_yes_ts    = ts
            elif side == "NO" and price > 0 and ts > latest_no_ts:
                latest_no_price = price
                latest_no_ts    = ts

            if ts > latest_ts:
                latest_ts = ts

        for hour_ts, delta in hour_deltas.items():
            self._db.upsert_hourly_volume(market_id, hour_ts, delta)

        if latest_yes_price is not None or latest_no_price is not None:
            self._db.upsert_price(market_id, latest_yes_price, latest_no_price)

        return latest_ts

    def detect_surge(self, market_id: str) -> SurgeEvent | None:
        """
        Run surge detection for the current hour of market_id.

        Returns SurgeEvent if hourly volume >= SURGE_MULTIPLIER_LOW × baseline,
        else None. Returns None if the current-hour bucket is too young
        (< SURGE_MIN_BUCKET_AGE_SECONDS old) to avoid false positives from
        the first few trades of an anomalous hour.
        """
        now = int(time.time())
        current_hour_ts = (now // config.SURGE_WINDOW_SECONDS) * config.SURGE_WINDOW_SECONDS

        # Don't fire on a bucket that just started
        if (now - current_hour_ts) < config.SURGE_MIN_BUCKET_AGE_SECONDS:
            return None

        lookback_start = current_hour_ts - (config.SURGE_LOOKBACK_HOURS * config.SURGE_WINDOW_SECONDS)
        rows = self._db.get_hourly_volumes(market_id, lookback_start)

        if not rows:
            return None

        # Split into prior hours (baseline) and current hour
        prior_vols = [vol for ts, vol in rows if ts < current_hour_ts]
        current_rows = [(ts, vol) for ts, vol in rows if ts == current_hour_ts]
        current_vol = current_rows[0][1] if current_rows else 0.0

        if len(prior_vols) < config.SIGNAL_C_MIN_BASELINE_HOURS:
            return None  # cold-start: not enough history for a reliable baseline

        baseline = statistics.median(prior_vols)
        if baseline < config.SURGE_MIN_BASELINE_USDC:
            return None

        ratio = current_vol / baseline
        if ratio < config.SURGE_MULTIPLIER_LOW:
            return None

        signal_c_score = 1.0 if ratio >= config.SURGE_MULTIPLIER_HIGH else 0.5
        yes_price, no_price = self._db.get_price(market_id)

        return SurgeEvent(
            market_id=market_id,
            hour_ts=current_hour_ts,
            surge_volume_usdc=current_vol,
            baseline_volume_usdc=baseline,
            surge_ratio=ratio,
            signal_c_score=signal_c_score,
            yes_price=yes_price,
            no_price=no_price,
        )

    def get_baseline_stats(self, market_id: str) -> dict:
        """
        Return a summary of the rolling baseline state for one market.
        Used by the daily digest (Step 8) and --dry-run output.

        Returns a dict with:
          hours_of_data   — number of hourly buckets in the 7-day window
          baseline_ready  — True if >= SIGNAL_C_MIN_BASELINE_HOURS buckets exist
          median_hourly   — median hourly volume (USDC) over the baseline window
          current_hour_vol — volume in the current (incomplete) hour
          yes_price       — latest YES price seen
        """
        now = int(time.time())
        current_hour_ts = (now // config.SURGE_WINDOW_SECONDS) * config.SURGE_WINDOW_SECONDS
        lookback_start = current_hour_ts - (config.SURGE_LOOKBACK_HOURS * config.SURGE_WINDOW_SECONDS)

        rows = self._db.get_hourly_volumes(market_id, lookback_start)
        prior_vols = [vol for ts, vol in rows if ts < current_hour_ts]
        current_rows = [vol for ts, vol in rows if ts == current_hour_ts]

        baseline = statistics.median(prior_vols) if len(prior_vols) >= 2 else 0.0
        yes_price, _ = self._db.get_price(market_id)

        return {
            "hours_of_data":    len(prior_vols),
            "baseline_ready":   len(prior_vols) >= config.SIGNAL_C_MIN_BASELINE_HOURS,
            "median_hourly":    baseline,
            "current_hour_vol": current_rows[0] if current_rows else 0.0,
            "yes_price":        yes_price,
        }

    @staticmethod
    def fetch_trades_since(
        condition_id: str,
        since_ts: int,
        session: requests.Session | None = None,
    ) -> list[dict]:
        """
        Fetch trades from data-api newer than since_ts (Unix seconds).

        data-api returns trades newest-first. We stop as soon as we see a
        trade with timestamp <= since_ts, avoiding full page sweeps on every poll.
        Max 4 pages (4000 trades) as a hard safety cap.

        Returns raw trade dicts, still newest-first. Caller (ingest_trades)
        handles normalization.
        """
        if session is None:
            session = _make_session()

        url = f"{config.DATA_API_BASE}/trades"
        page_size = 1000
        max_offset = 3000
        seen_hashes: set[str] = set()
        result: list[dict] = []

        offset = 0
        while offset <= max_offset:
            try:
                resp = session.get(
                    url,
                    params={"market": condition_id, "limit": page_size, "offset": offset},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=30,
                )
                resp.raise_for_status()
                page = resp.json()
            except Exception as exc:
                logger.warning("fetch_trades_since error for %s: %s", condition_id[:16], exc)
                break

            if not isinstance(page, list) or not page:
                break

            done = False
            for t in page:
                tx = t.get("transaction_hash") or t.get("transactionHash") or ""
                if tx in seen_hashes:
                    continue
                ts = _parse_timestamp(t)
                if ts is not None and ts <= since_ts:
                    done = True
                    break  # everything from here is older than our cursor
                seen_hashes.add(tx)
                result.append(t)

            if done or len(page) < page_size:
                break

            offset += page_size
            time.sleep(config.REQUEST_DELAY_SECONDS)

        return result
