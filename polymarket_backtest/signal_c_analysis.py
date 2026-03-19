"""
Signal C Deep-Dive: timeline analysis for Venezuela and Iran Strike markets.

For each surge event, reports:
  - Surge timestamp and ratio
  - YES price at the time of the surge
  - YES price 24h and 48h after the surge
  - Return if you had bought YES at the surge price (given final resolution)
  - Corresponding real-world event (from known_events map)

Run from the polymarket_backtest/ directory:
  python3 signal_c_analysis.py
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
import datetime

import pandas as pd

# ── Real-world event timeline ──────────────────────────────────────────────────
# Key dates mapped to brief descriptions for context
TIMELINE = {
    # Venezuela
    "2025-11-05": "US Election Day — Trump wins",
    "2025-11-06": "Trump victory confirmed, Venezuela rhetoric intensifies",
    "2025-11-18": "Trump announces Venezuela/Maduro policy intentions",
    "2025-11-21": "Reports of US military planning for Caribbean",
    "2025-12-02": "Trump admin issues Venezuela ultimatum",
    "2025-12-09": "US closes Venezuelan embassy, escalation signals",
    "2025-12-13": "Reports of US carrier group repositioning",
    "2025-12-15": "Official military buildup confirmed by press",
    "2025-12-17": "Trump tweets direct threat to Maduro",
    "2025-12-22": "Maduro refuses US demands",
    "2025-12-25": "US special forces pre-positioning reported",
    "2025-12-29": "Final ultimatum deadline passes",
    "2026-01-03": "*** US INVASION BEGINS ***",
    # Iran
    "2026-02-06": "*** US-IRAN MEETING OCCURS ***",
    "2026-02-10": "Iran nuclear talks breakdown",
    "2026-02-15": "US carrier strike group enters Persian Gulf",
    "2026-02-20": "Iran test-fires ballistic missiles",
    "2026-02-24": "Israel intelligence reports Iran strike imminent",
    "2026-02-28": "*** IRAN STRIKES ISRAEL; US BEGINS STRIKES ON IRAN ***",
}

MARKETS_TO_ANALYZE = [
    {
        "condition_id": "0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68",
        "name": "Venezuela Invasion by Dec 31, 2025",
        "resolution": "NO",  # market expired Dec 31 before invasion Jan 3
        "real_event_date": "2026-01-03",
        "real_event_happened": True,
        "note": "Market resolved NO (deadline Dec 31) but invasion DID happen Jan 3.",
    },
    {
        "condition_id": "0xb3ebf217cf2f393a66030c072b04b893268506923e01b23f1bcf3504c3d319c2",
        "name": "Iran Strike on Israel by Feb 28, 2026",
        "resolution": "YES",
        "real_event_date": "2026-02-28",
        "real_event_happened": True,
        "note": "Resolved YES on Feb 28.",
    },
]

SURGE_MULTIPLIER = 5.0
SURGE_WINDOW_SECONDS = 3600
SURGE_LOOKBACK_HOURS = 7 * 24
SURGE_MIN_BASELINE = 500.0


@dataclass
class SurgeEvent:
    hour_ts: int
    datetime_utc: datetime.datetime
    surge_volume: float
    baseline_volume: float
    surge_ratio: float
    yes_price_at_surge: float | None
    yes_price_24h: float | None
    yes_price_48h: float | None
    resolution: str | None          # YES or NO (market resolution)
    real_event_happened: bool
    return_if_bought_yes: float | None  # based on market resolution
    real_world_return: float | None     # based on real event (for NO-resolved markets where event happened)
    nearest_event: str | None


def load_trades(condition_id: str) -> pd.DataFrame:
    path = Path(f"data/raw_trades/{condition_id}.json")
    meta_path = Path(f"data/raw_markets/{condition_id}.json")
    if not path.exists():
        raise FileNotFoundError(f"No trades for {condition_id}")

    raw = json.loads(path.read_text())
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    yes_token = meta.get("yes_token_id", "")
    no_token = meta.get("no_token_id", "")

    rows = []
    for t in raw:
        ts = int(float(t.get("timestamp", 0)))
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        asset = t.get("asset_id") or t.get("assetId") or t.get("asset") or ""
        outcome = (t.get("outcome") or "").strip().upper()

        if yes_token and asset == yes_token:
            side = "YES"
        elif no_token and asset == no_token:
            side = "NO"
        elif outcome in ("YES", "TRUE"):
            side = "YES"
        elif outcome in ("NO", "FALSE"):
            side = "NO"
        else:
            continue

        if size < 10 or price <= 0:
            continue
        rows.append({"timestamp": ts, "side": side, "price": price, "size": size})

    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return df


def yes_price_at(df: pd.DataFrame, timestamp: int) -> float | None:
    """Most recent YES trade price at or before timestamp."""
    prior = df[(df["timestamp"] <= timestamp) & (df["side"] == "YES")]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["price"])


def nearest_timeline_event(ts: int, window_days: int = 3) -> str | None:
    """Find the nearest real-world event within window_days of ts."""
    dt = datetime.datetime.utcfromtimestamp(ts)
    date_str = dt.strftime("%Y-%m-%d")
    best = None
    best_delta = window_days * 86400

    for event_date_str, desc in TIMELINE.items():
        event_dt = datetime.datetime.strptime(event_date_str, "%Y-%m-%d")
        delta = abs((event_dt - dt).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best = f"{event_date_str}: {desc}"

    return best


def detect_surges(df: pd.DataFrame) -> list[SurgeEvent]:
    """Detect Signal C surge events and compute price context."""
    df = df.copy()
    df["hour_bucket"] = (df["timestamp"] // SURGE_WINDOW_SECONDS) * SURGE_WINDOW_SECONDS
    hourly = df.groupby("hour_bucket")["size"].sum().sort_index()

    if len(hourly) < 2:
        return []

    surges = []
    vols = list(hourly.values)
    tss = list(hourly.index)

    for i, (hour_ts, vol) in enumerate(zip(tss, vols)):
        start = max(0, i - SURGE_LOOKBACK_HOURS)
        prior = vols[start:i]
        if len(prior) < 5:
            continue
        baseline = statistics.median(prior)
        if baseline < SURGE_MIN_BASELINE:
            continue
        ratio = vol / baseline
        if ratio < SURGE_MULTIPLIER:
            continue

        dt = datetime.datetime.utcfromtimestamp(hour_ts)
        yes_now = yes_price_at(df, hour_ts + SURGE_WINDOW_SECONDS - 1)
        yes_24h = yes_price_at(df, hour_ts + 86400)
        yes_48h = yes_price_at(df, hour_ts + 172800)

        surges.append(SurgeEvent(
            hour_ts=int(hour_ts),
            datetime_utc=dt,
            surge_volume=vol,
            baseline_volume=baseline,
            surge_ratio=ratio,
            yes_price_at_surge=yes_now,
            yes_price_24h=yes_24h,
            yes_price_48h=yes_48h,
            resolution=None,  # filled in below
            real_event_happened=False,
            return_if_bought_yes=None,
            real_world_return=None,
            nearest_event=nearest_timeline_event(int(hour_ts)),
        ))

    return surges


def compute_returns(surge: SurgeEvent, resolution: str, real_event_happened: bool) -> None:
    """Annotate a surge with return calculations."""
    surge.resolution = resolution
    surge.real_event_happened = real_event_happened

    if surge.yes_price_at_surge and surge.yes_price_at_surge > 0:
        p = surge.yes_price_at_surge
        # Market return: based on official resolution
        if resolution == "YES":
            surge.return_if_bought_yes = (1.0 - p) / p
        else:
            surge.return_if_bought_yes = -1.0

        # Real-world return: what you'd earn if the event ACTUALLY happened
        if real_event_happened:
            surge.real_world_return = (1.0 - p) / p
        else:
            surge.real_world_return = -1.0


def print_analysis(market: dict, surges: list[SurgeEvent]) -> None:
    w = 100
    print("=" * w)
    print(f"SIGNAL C DEEP-DIVE: {market['name']}")
    print(f"  Market resolution: {market['resolution']}  |  Real event: {'YES' if market['real_event_happened'] else 'NO'} ({market['real_event_date']})")
    print(f"  Note: {market['note']}")
    print("=" * w)
    print(f"\n{'Date/Time (UTC)':<20} {'Ratio':>7} {'Vol ($)':>10} {'Baseline':>10} "
          f"{'YES@surge':>10} {'YES+24h':>8} {'YES+48h':>8} "
          f"{'Mkt Ret':>8} {'Real Ret':>8}  Nearby Event")
    print("-" * w)

    buyable = [s for s in surges if s.yes_price_at_surge is not None and s.return_if_bought_yes is not None]

    for s in surges:
        dt_str = s.datetime_utc.strftime("%Y-%m-%d %H:%M")
        yes_str = f"{s.yes_price_at_surge:.3f}" if s.yes_price_at_surge else "  N/A"
        y24_str = f"{s.yes_price_24h:.3f}" if s.yes_price_24h else "  N/A"
        y48_str = f"{s.yes_price_48h:.3f}" if s.yes_price_48h else "  N/A"
        mkt_ret = f"{s.return_if_bought_yes:+.0%}" if s.return_if_bought_yes is not None else "   N/A"
        real_ret = f"{s.real_world_return:+.0%}" if s.real_world_return is not None else "   N/A"
        event = (s.nearest_event or "")[:38]
        flag = " ◄◄" if s.nearest_event and "***" in s.nearest_event else ""

        print(f"{dt_str:<20} {s.surge_ratio:>7.1f}x {s.surge_volume:>10,.0f} {s.baseline_volume:>10,.0f} "
              f"{yes_str:>10} {y24_str:>8} {y48_str:>8} "
              f"{mkt_ret:>8} {real_ret:>8}  {event}{flag}")

    print()
    # Summary stats
    if buyable:
        mkt_rets = [s.return_if_bought_yes for s in buyable]
        real_rets = [s.real_world_return for s in buyable if s.real_world_return is not None]
        avg_yes = sum(s.yes_price_at_surge for s in buyable) / len(buyable)
        print(f"  Actionable surges (YES price available): {len(buyable)} of {len(surges)} total surges")
        print(f"  Avg YES price at surge:    {avg_yes:.3f}")
        print(f"  Avg market return (buy YES at surge): {sum(mkt_rets)/len(mkt_rets):+.1%}")
        if real_rets:
            print(f"  Avg real-event return:                {sum(real_rets)/len(real_rets):+.1%}")

        # Timing analysis: lead time to real event
        real_event_ts = int(datetime.datetime.strptime(market["real_event_date"], "%Y-%m-%d").timestamp())
        lead_times = [(real_event_ts - s.hour_ts) / 86400 for s in buyable]
        print(f"\n  Lead time to real event ({market['real_event_date']}):")
        print(f"    Earliest surge: {max(lead_times):.0f} days before")
        print(f"    Latest surge:   {min(lead_times):.0f} days before")
        print(f"    Median lead:    {sorted(lead_times)[len(lead_times)//2]:.0f} days before")

        # Price trajectory: did YES price rise after surges?
        rising = sum(1 for s in buyable if s.yes_price_24h and s.yes_price_24h > (s.yes_price_at_surge or 0))
        print(f"\n  YES price rose within 24h of surge: {rising}/{len(buyable)} times ({rising/len(buyable):.0%})")
        rising_48 = sum(1 for s in buyable if s.yes_price_48h and s.yes_price_48h > (s.yes_price_at_surge or 0))
        print(f"  YES price rose within 48h of surge: {rising_48}/{len(buyable)} times ({rising_48/len(buyable):.0%})")
    print()


def main() -> None:
    for market in MARKETS_TO_ANALYZE:
        cid = market["condition_id"]
        try:
            df = load_trades(cid)
        except FileNotFoundError:
            print(f"No trade data for {market['name']} — skipping.\n")
            continue

        surges = detect_surges(df)
        for s in surges:
            compute_returns(s, market["resolution"], market["real_event_happened"])

        print_analysis(market, surges)


if __name__ == "__main__":
    main()
