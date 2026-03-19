"""
ReportGenerator: produces human-readable console output and a JSON report file.

Output sections:
  1. Executive summary — aggregate stats across all markets and thresholds
  2. Per-market breakdown — signals, hit rate, avg entry price, avg return
  3. Best / worst signals — top 5 and bottom 5 by return at 30-min delay
  4. Threshold sensitivity — how metrics change at $10K vs $25K vs $50K
  5. Delay degradation — how EV degrades from 5min → 30min → 2hr detection
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import config
from backtester import Backtester, SignalResult
from signal_detector import SignalA, SignalC

logger = logging.getLogger(__name__)


class ReportGenerator:
    def __init__(
        self,
        results: list[SignalResult],
        signals_a: list[SignalA],
        signals_c: list[SignalC],
        market_metadata: dict[str, dict],
        backtester: Backtester,
    ) -> None:
        self._results = results
        self._signals_a = signals_a
        self._signals_c = signals_c
        self._metadata = market_metadata
        self._backtester = backtester

    def generate(self) -> dict:
        """
        Generate the full report.

        Prints to stdout and saves JSON to config.REPORT_PATH.
        Returns the report dict.
        """
        agg_df = self._backtester.aggregate_stats(self._results)
        market_df = self._backtester.per_market_stats(self._results)

        split_df = self._backtester.resolution_split_stats(self._results)

        report = {
            "summary": self._build_summary(agg_df),
            "per_market": self._build_per_market(market_df),
            "threshold_sensitivity": self._build_threshold_sensitivity(agg_df),
            "delay_degradation": self._build_delay_degradation(agg_df),
            "resolution_split": split_df.to_dict("records") if not split_df.empty else [],
            "best_signals": self._best_signals(5),
            "worst_signals": self._worst_signals(5),
            "signal_c_surges": self._build_signal_c(),
        }

        self._print_report(report, agg_df, market_df, split_df)
        self._save_report(report)
        return report

    # ── Report sections ───────────────────────────────────────────────────────

    def _build_summary(self, agg_df: pd.DataFrame) -> dict:
        n_markets = len({s.market_id for s in self._signals_a})
        n_signals = len(self._signals_a)
        n_resolved = sum(1 for s in self._signals_a if s.resolution is not None)

        # Use the 30-minute delay and lowest threshold as the "headline" row
        headline = self._headline_row(agg_df)

        return {
            "n_markets_analyzed": n_markets,
            "n_whale_signals_total": n_signals,
            "n_signal_c_total": len(self._signals_c),
            "n_signals_with_resolution": n_resolved,
            "headline_threshold": headline.get("threshold"),
            "headline_delay_label": headline.get("delay_label"),
            "headline_hit_rate": headline.get("hit_rate"),
            "headline_avg_entry_price": headline.get("avg_entry_price"),
            "headline_ev_per_dollar": headline.get("ev_per_dollar"),
        }

    def _build_threshold_sensitivity(self, agg_df: pd.DataFrame) -> list[dict]:
        # Fix the delay at 30 minutes, vary threshold
        thirty_min = 30 * 60
        rows = agg_df[agg_df["delay_seconds"] == thirty_min].to_dict("records")
        return rows

    def _build_delay_degradation(self, agg_df: pd.DataFrame) -> list[dict]:
        # Fix the threshold at the lowest, vary delay
        if agg_df.empty:
            return []
        lowest_threshold = agg_df["threshold"].min()
        rows = agg_df[agg_df["threshold"] == lowest_threshold].to_dict("records")
        return rows

    def _build_per_market(self, market_df: pd.DataFrame) -> list[dict]:
        if market_df.empty:
            return []
        # Summarise at lowest threshold, 30-min delay
        if "threshold" in market_df.columns and "delay_seconds" in market_df.columns:
            lowest_th = market_df["threshold"].min()
            thirty = 30 * 60
            filtered = market_df[
                (market_df["threshold"] == lowest_th) & (market_df["delay_seconds"] == thirty)
            ]
        else:
            filtered = market_df

        records = filtered.to_dict("records")
        # Sort by hit_rate descending
        records.sort(key=lambda r: r.get("hit_rate") or 0, reverse=True)
        return records

    def _build_signal_c(self) -> list[dict]:
        rows = []
        for s in self._signals_c:
            meta = self._metadata.get(s.market_id, {})
            rows.append({
                "market_id": s.market_id,
                "question": meta.get("question", s.market_id)[:80],
                "trigger_timestamp": s.trigger_timestamp,
                "surge_volume_usdc": s.surge_volume_usdc,
                "baseline_volume_usdc": s.baseline_volume_usdc,
                "surge_ratio": s.surge_ratio,
                "resolution": s.resolution,
            })
        rows.sort(key=lambda r: r["surge_ratio"], reverse=True)
        return rows

    def _best_signals(self, n: int) -> list[dict]:
        return self._ranked_signals(n, ascending=False)

    def _worst_signals(self, n: int) -> list[dict]:
        return self._ranked_signals(n, ascending=True)

    def _ranked_signals(self, n: int, ascending: bool) -> list[dict]:
        """Top/bottom N signals by return at the 30-minute delay."""
        delay = 30 * 60
        rows = []
        for r in self._results:
            entry = r.entries.get(delay)
            if entry is None or entry.return_pct is None:
                continue
            meta = self._metadata.get(r.signal.market_id, {})
            rows.append({
                "market_id": r.signal.market_id,
                "question": meta.get("question", r.signal.market_id)[:80],
                "wallet": r.signal.wallet,
                "side": r.signal.side,
                "trigger_price": r.signal.trigger_price,
                "entry_price_30min": entry.entry_price,
                "return_pct": entry.return_pct,
                "resolution": r.signal.resolution,
                "n_criteria": r.signal.n_criteria,
                "criteria_met": r.signal.criteria_met,
                "threshold": r.signal.threshold,
                "wallet_age_days": r.signal.wallet_age_days,
            })

        rows.sort(key=lambda x: x["return_pct"], reverse=not ascending)
        return rows[:n]

    # ── Console output ────────────────────────────────────────────────────────

    def _print_report(self, report: dict, agg_df: pd.DataFrame, market_df: pd.DataFrame, split_df: pd.DataFrame) -> None:
        _hr("=")
        print("POLYMARKET WHALE DETECTION BACKTEST — ROUND 2 REPORT")
        _hr("=")

        s = report["summary"]
        print(f"\nMarkets analyzed:        {s['n_markets_analyzed']}")
        print(f"Signal A (burner acct):  {s['n_whale_signals_total']}")
        print(f"Signal C (vol surge):    {s['n_signal_c_total']}")
        print(f"Signals A with resolution: {s['n_signals_with_resolution']}")
        if s["headline_hit_rate"] is not None:
            print(
                f"\nHeadline result (${s['headline_threshold']:,.0f} threshold, "
                f"{s['headline_delay_label']} delay):"
            )
            print(f"  Hit rate:              {s['headline_hit_rate']:.1%}")
            print(f"  Avg entry price:       {_fmt_price(s['headline_avg_entry_price'])}")
            print(f"  EV per $1 risked:      {_fmt_return(s['headline_ev_per_dollar'])}")

        _hr()
        print("\nTHRESHOLD SENSITIVITY (30-minute detection delay)")
        _hr()
        if not agg_df.empty:
            th_df = agg_df[agg_df["delay_seconds"] == 30 * 60]
            _print_table(th_df, [
                ("threshold", "Threshold", lambda v: f"${v:,.0f}"),
                ("n_signals", "Signals", str),
                ("hit_rate", "Hit Rate", lambda v: f"{v:.1%}" if v is not None else "N/A"),
                ("avg_entry_price", "Avg Entry", _fmt_price),
                ("ev_per_dollar", "EV/$1", _fmt_return),
            ])

        _hr()
        print("\nDETECTION DELAY DEGRADATION ($10,000 threshold)")
        _hr()
        if not agg_df.empty:
            lowest_th = agg_df["threshold"].min()
            delay_df = agg_df[agg_df["threshold"] == lowest_th]
            _print_table(delay_df, [
                ("delay_label", "Delay", str),
                ("n_signals", "Signals", str),
                ("hit_rate", "Hit Rate", lambda v: f"{v:.1%}" if v is not None else "N/A"),
                ("avg_entry_price", "Avg Entry", _fmt_price),
                ("ev_per_dollar", "EV/$1", _fmt_return),
                ("entry_vs_baseline_delta", "Δ vs 24h-prior", lambda v: _fmt_price(v, signed=True) if v is not None else "N/A"),
            ])

        _hr()
        print("\nPER-MARKET BREAKDOWN ($10,000 threshold, 30-min delay)")
        _hr()
        if not market_df.empty:
            lowest_th = market_df["threshold"].min() if "threshold" in market_df.columns else None
            if lowest_th is not None:
                display_df = market_df[
                    (market_df["threshold"] == lowest_th)
                    & (market_df["delay_seconds"] == 30 * 60)
                ]
            else:
                display_df = market_df
            _print_table(display_df, [
                ("question", "Market", lambda v: v[:50]),
                ("n_signals", "Signals", str),
                ("hit_rate", "Hit Rate", lambda v: f"{v:.1%}" if v is not None else "N/A"),
                ("avg_entry_price", "Avg Entry", _fmt_price),
                ("ev_per_dollar", "EV/$1", _fmt_return),
            ])

        _hr()
        print("\nSIGNAL SIDE ALIGNMENT (30-min delay) — did the whale bet the right way?")
        _hr()
        if not split_df.empty:
            thirty = split_df[split_df["delay_seconds"] == 30 * 60]
            for _, row in thirty.iterrows():
                label = "Correct side" if row["signal_aligned_with_resolution"] else "Wrong side  "
                print(f"  {label}  n={int(row['n_signals']):<5}  "
                      f"hit_rate={_fmt_pct(row['hit_rate'])}  "
                      f"avg_entry={_fmt_price(row['avg_entry_price'])}  "
                      f"EV={_fmt_return(row['avg_return_pct'])}")
        else:
            print("  (no resolved signals)")

        _hr()
        print("\nBEST SIGNALS A (by 30-min return)")
        _hr()
        for i, sig in enumerate(report["best_signals"], 1):
            age = f"  wallet_age={sig['wallet_age_days']:.0f}d" if sig['wallet_age_days'] is not None else ""
            print(f"  {i}. {sig['question'][:60]}")
            print(f"     {sig['side']} @ {_fmt_price(sig['trigger_price'])} → "
                  f"entry {_fmt_price(sig['entry_price_30min'])} → "
                  f"return {_fmt_return(sig['return_pct'])} "
                  f"(resolved {sig['resolution']}, {sig['n_criteria']}/5 criteria{age})")

        _hr()
        print("\nWORST SIGNALS A (by 30-min return)")
        _hr()
        for i, sig in enumerate(report["worst_signals"], 1):
            age = f"  wallet_age={sig['wallet_age_days']:.0f}d" if sig['wallet_age_days'] is not None else ""
            print(f"  {i}. {sig['question'][:60]}")
            print(f"     {sig['side']} @ {_fmt_price(sig['trigger_price'])} → "
                  f"entry {_fmt_price(sig['entry_price_30min'])} → "
                  f"return {_fmt_return(sig['return_pct'])} "
                  f"(resolved {sig['resolution']}, {sig['n_criteria']}/5 criteria{age})")

        _hr()
        print("\nSIGNAL C — VOLUME SURGES")
        _hr()
        if self._signals_c:
            for sig in sorted(self._signals_c, key=lambda s: s.surge_ratio, reverse=True)[:10]:
                meta = self._metadata.get(sig.market_id, {})
                q = meta.get("question", sig.market_id)[:55]
                import datetime
                ts = datetime.datetime.utcfromtimestamp(sig.trigger_timestamp).strftime("%Y-%m-%d %H:%M")
                print(f"  {ts}  {q}  {sig.surge_ratio:.1f}x  "
                      f"(${sig.surge_volume_usdc:,.0f} vs ${sig.baseline_volume_usdc:,.0f} baseline)")
        else:
            print("  (none)")

        _hr("=")
        print(f"Full report saved to: {config.REPORT_PATH}")
        _hr("=")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_report(self, report: dict) -> None:
        path = Path(config.REPORT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=_json_default))
        logger.info("Report saved to %s", path)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _headline_row(self, agg_df: pd.DataFrame) -> dict:
        if agg_df.empty:
            return {}
        thirty = 30 * 60
        lowest_th = agg_df["threshold"].min()
        rows = agg_df[(agg_df["delay_seconds"] == thirty) & (agg_df["threshold"] == lowest_th)]
        return rows.iloc[0].to_dict() if not rows.empty else {}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_pct(v) -> str:
    return f"{v:.1%}" if v is not None else "N/A"


def _fmt_price(v, signed: bool = False) -> str:
    if v is None:
        return "N/A"
    prefix = "+" if signed and v > 0 else ""
    return f"{prefix}{v:.3f}"


def _fmt_return(v) -> str:
    if v is None:
        return "N/A"
    prefix = "+" if v > 0 else ""
    return f"{prefix}{v:.1%}"


def _hr(char: str = "-") -> None:
    print(char * 70)


def _print_table(df: pd.DataFrame, cols: list[tuple]) -> None:
    """Print a simple ASCII table from a DataFrame."""
    if df.empty:
        print("  (no data)")
        return
    headers = [c[1] for c in cols]
    widths = [max(len(h), 10) for h in headers]
    header_row = "  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(header_row)
    print("  " + "  ".join("-" * w for w in widths))
    for _, row in df.iterrows():
        parts = []
        for (col, _, fmt), w in zip(cols, widths):
            val = row.get(col)
            try:
                parts.append(fmt(val).ljust(w))
            except Exception:
                parts.append("N/A".ljust(w))
        print("  " + "  ".join(parts))


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(obj)}")
