"""
Backtester: simulates following each whale signal at various detection delays.

For each signal and each delay (5 min / 30 min / 2 hr):
  1. Compute the simulated entry timestamp = signal.trigger_timestamp + delay
  2. Find the market price at that entry time by scanning subsequent trades
     (the most-recent trade price before the entry time is used as a proxy
     for the mid/ask — a conservative approximation without order book data)
  3. Compute return: (resolution_price - entry_price) / entry_price
     where resolution_price = 1.0 (win) or 0.0 (loss) for binary markets
  4. Aggregate statistics across all signals

Assumptions and limitations:
  - Entry price = most recent trade price at/before entry time.
    This may underestimate the true ask (you'd typically pay slightly above
    the last trade price). Results are therefore optimistic by a few cents.
  - Position size is fixed at $1 notional for comparability. In practice
    fill size and liquidity constraints would affect realised returns.
  - A signal that fired on the WRONG side (whale bet YES, market resolved NO)
    results in a -100% return (total loss of the USDC position).

Key output metrics per (threshold, delay):
  - n_signals:        number of signals
  - n_resolved:       signals with a known resolution
  - hit_rate:         fraction where resolution == signal.side
  - avg_entry_price:  average price at simulated entry
  - avg_return:       average (resolution_price - entry_price) / entry_price
  - ev_per_dollar:    expected value per $1 risked = avg_return (signed)
  - pct_price_moved:  how much of the final move already happened by entry time
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from whale_detector import WhaleSignal
from signal_detector import SignalA

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    signal: WhaleSignal | SignalA
    # delay_seconds → entry metrics
    entries: dict[int, "EntryMetrics"]
    # Price 24h before detection (used as baseline)
    price_24h_before_detection: float | None


@dataclass
class EntryMetrics:
    delay_seconds: int
    entry_timestamp: int
    entry_price: float | None      # None if no trades found after entry time
    resolution: str | None         # YES or NO
    return_pct: float | None       # None if entry_price is None or unresolved
    hit: bool | None               # True = signal was correct, False = wrong, None = unknown


class Backtester:
    def __init__(
        self,
        signals: list[WhaleSignal | SignalA],
        all_trades: pd.DataFrame,
        market_metadata: dict[str, dict],
    ) -> None:
        self._signals = signals
        self._metadata = market_metadata
        # Pre-index trades by market_id for fast lookups
        self._market_trades: dict[str, pd.DataFrame] = {}
        for market_id, group in all_trades.groupby("market_id"):
            self._market_trades[str(market_id)] = group.sort_values("timestamp").reset_index(drop=True)

    def run(self) -> list[SignalResult]:
        results = []
        for signal in self._signals:
            result = self._simulate_signal(signal)
            results.append(result)

        logger.info("Backtest complete: %d signal results", len(results))
        return results

    def aggregate_stats(self, results: list[SignalResult]) -> pd.DataFrame:
        """
        Return a DataFrame of aggregate statistics broken down by
        (threshold, delay_seconds).
        """
        rows = []
        # Group signals by threshold
        from itertools import groupby
        by_threshold: dict[float, list[SignalResult]] = {}
        for r in results:
            t = r.signal.threshold
            by_threshold.setdefault(t, []).append(r)

        for threshold, group_results in sorted(by_threshold.items()):
            for delay in config.DETECTION_DELAYS_SECONDS:
                row = self._stats_for_group(group_results, threshold, delay)
                rows.append(row)

        return pd.DataFrame(rows)

    def resolution_split_stats(self, results: list[SignalResult]) -> pd.DataFrame:
        """
        Break out EV by whether the signal was on the correct side vs wrong side,
        and by market resolution (YES vs NO).

        For each result, a signal is "aligned" if signal.side == market resolution.
        This separates "smart money that was right" from "big bets that were wrong".
        """
        rows = []
        for delay in config.DETECTION_DELAYS_SECONDS:
            for aligned in (True, False):
                group = [
                    r for r in results
                    if r.signal.resolution is not None
                    and (r.signal.side == r.signal.resolution) == aligned
                    and delay in r.entries
                ]
                if not group:
                    continue
                with_return = [
                    r.entries[delay] for r in group
                    if r.entries[delay].return_pct is not None
                ]
                rows.append({
                    "delay_label": _delay_label(delay),
                    "delay_seconds": delay,
                    "signal_aligned_with_resolution": aligned,
                    "n_signals": len(group),
                    "n_with_return": len(with_return),
                    "hit_rate": sum(1 for e in with_return if e.hit) / len(with_return) if with_return else None,
                    "avg_entry_price": float(np.mean([e.entry_price for e in with_return if e.entry_price])) if with_return else None,
                    "avg_return_pct": float(np.mean([e.return_pct for e in with_return])) if with_return else None,
                })
        return pd.DataFrame(rows)

    def per_market_stats(self, results: list[SignalResult]) -> pd.DataFrame:
        """Return per-(market, threshold, delay) breakdown."""
        rows = []
        by_market: dict[str, list[SignalResult]] = {}
        for r in results:
            by_market.setdefault(r.signal.market_id, []).append(r)

        for market_id, group_results in sorted(by_market.items()):
            meta = self._metadata.get(market_id, {})
            question = meta.get("question", market_id)
            thresholds = sorted({r.signal.threshold for r in group_results})
            for threshold in thresholds:
                th_results = [r for r in group_results if r.signal.threshold == threshold]
                for delay in config.DETECTION_DELAYS_SECONDS:
                    row = self._stats_for_group(th_results, threshold, delay)
                    row["market_id"] = market_id
                    row["question"] = question[:80]
                    rows.append(row)

        return pd.DataFrame(rows)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _simulate_signal(self, signal: WhaleSignal) -> SignalResult:
        trades = self._market_trades.get(signal.market_id, pd.DataFrame())
        resolution = signal.resolution

        # Price 24h before the signal as baseline comparison (same side)
        baseline_ts = signal.trigger_timestamp - 24 * 3600
        price_24h_before = self._price_at(trades, baseline_ts, side=signal.side)

        entries: dict[int, EntryMetrics] = {}
        for delay in config.DETECTION_DELAYS_SECONDS:
            entry_ts = signal.trigger_timestamp + delay
            entry_price = self._price_at(trades, entry_ts, side=signal.side)

            if entry_price is None or resolution is None:
                entries[delay] = EntryMetrics(
                    delay_seconds=delay,
                    entry_timestamp=entry_ts,
                    entry_price=entry_price,
                    resolution=resolution,
                    return_pct=None,
                    hit=None,
                )
                continue

            hit = resolution == signal.side
            return_pct = _compute_return(entry_price, hit)

            entries[delay] = EntryMetrics(
                delay_seconds=delay,
                entry_timestamp=entry_ts,
                entry_price=entry_price,
                resolution=resolution,
                return_pct=return_pct,
                hit=hit,
            )

        return SignalResult(
            signal=signal,
            entries=entries,
            price_24h_before_detection=price_24h_before,
        )

    @staticmethod
    def _price_at(trades: pd.DataFrame, timestamp: int, side: str | None = None) -> float | None:
        """
        Return the most-recent trade price at or before `timestamp`.
        Optionally filtered to a specific side (YES or NO).
        Returns None if no matching trades exist.
        """
        if trades.empty:
            return None
        prior = trades[trades["timestamp"] <= timestamp]
        if side:
            prior = prior[prior["side"] == side]
        if prior.empty:
            return None
        return float(prior.iloc[-1]["price"])

    @staticmethod
    def _stats_for_group(
        results: list[SignalResult],
        threshold: float,
        delay: int,
    ) -> dict:
        n_signals = len(results)
        entries = [r.entries[delay] for r in results if delay in r.entries]

        resolved = [e for e in entries if e.resolution is not None]
        with_price = [e for e in entries if e.entry_price is not None]
        with_return = [e for e in entries if e.return_pct is not None]
        hits = [e for e in resolved if e.hit is True]

        avg_entry = float(np.mean([e.entry_price for e in with_price])) if with_price else None
        avg_return = float(np.mean([e.return_pct for e in with_return])) if with_return else None

        # Baseline: price_24h_before for signals with both prices available
        baselines = [
            r for r in results
            if r.price_24h_before_detection is not None
            and delay in r.entries
            and r.entries[delay].entry_price is not None
        ]
        avg_baseline = (
            float(np.mean([r.price_24h_before_detection for r in baselines]))
            if baselines else None
        )

        return {
            "threshold": threshold,
            "delay_seconds": delay,
            "delay_label": _delay_label(delay),
            "n_signals": n_signals,
            "n_resolved": len(resolved),
            "n_with_price": len(with_price),
            "hit_rate": len(hits) / len(resolved) if resolved else None,
            "avg_entry_price": avg_entry,
            "avg_return_pct": avg_return,
            "ev_per_dollar": avg_return,   # same as avg_return for unit position
            "avg_baseline_price_24h_prior": avg_baseline,
            "entry_vs_baseline_delta": (
                avg_entry - avg_baseline
                if avg_entry is not None and avg_baseline is not None
                else None
            ),
        }


# ── Utility ───────────────────────────────────────────────────────────────────

def _compute_return(entry_price: float, hit: bool) -> float:
    """
    Binary market return:
      - Win:  shares resolve to $1.00, bought at entry_price
              → return = (1.0 - entry_price) / entry_price
      - Loss: shares resolve to $0.00
              → return = -1.0  (total loss)
    """
    if not hit:
        return -1.0
    if entry_price >= 1.0:
        return 0.0  # already at resolution — no gain possible
    return (1.0 - entry_price) / entry_price


def _delay_label(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}min"
    return f"{seconds // 3600}hr"
