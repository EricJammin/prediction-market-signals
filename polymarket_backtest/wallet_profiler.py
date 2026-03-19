"""
WalletProfiler: builds per-wallet activity profiles across all collected markets.

For each wallet we track:
  - first_trade_ts:   earliest trade timestamp across ALL markets (freshness)
  - total_volume:     total USDC traded across all markets (all sides)
  - per_market:       per-(market, side) volume breakdown
  - concentration:    fraction of total volume in each (market, side) pair

These profiles feed directly into the 4-criterion whale scoring in WhaleDetector.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class WalletProfile:
    wallet: str
    first_trade_ts: int                           # unix seconds
    total_volume_usdc: float                      # across all markets
    # (market_id, side) → total USDC (both directions, for concentration)
    market_side_volume: dict[tuple[str, str], float] = field(default_factory=dict)
    # (market_id, side, direction) → USDC; direction = "BUY" or "SELL"
    market_side_dir_volume: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def concentration(self, market_id: str, side: str) -> float:
        """Fraction of total volume concentrated in (market_id, side)."""
        if self.total_volume_usdc <= 0:
            return 0.0
        return self.market_side_volume.get((market_id, side), 0.0) / self.total_volume_usdc

    def cumulative_position(self, market_id: str, side: str) -> float:
        """Total USDC position in a specific (market, side) pair."""
        return self.market_side_volume.get((market_id, side), 0.0)

    def is_net_buyer(self, market_id: str, side: str) -> bool:
        """True if wallet bought more than it sold in this (market, side)."""
        bought = self.market_side_dir_volume.get((market_id, side, "BUY"), 0.0)
        sold = self.market_side_dir_volume.get((market_id, side, "SELL"), 0.0)
        return bought > sold

    def is_fresh_at(self, trade_ts: int, freshness_window_seconds: int) -> bool:
        """
        Return True if the wallet's first-ever trade was within the freshness
        window before (or at the same time as) this trade.

        Also returns True if this trade IS their first trade ever.
        """
        return (trade_ts - self.first_trade_ts) <= freshness_window_seconds


class WalletProfiler:
    def __init__(self, all_trades: pd.DataFrame) -> None:
        """
        Args:
            all_trades: normalised trades DataFrame from DataCollector.load_all_data()
                        Expected columns: wallet, market_id, timestamp, side, size_usdc
        """
        if all_trades.empty:
            raise ValueError("all_trades DataFrame is empty — cannot build wallet profiles")
        self._trades = all_trades

    def build_profiles(self) -> dict[str, WalletProfile]:
        """
        Iterate over all trades once and accumulate per-wallet statistics.

        Returns dict: wallet (lowercase hex) → WalletProfile
        """
        logger.info("Building wallet profiles from %d trades across %d wallets...",
                    len(self._trades),
                    self._trades["wallet"].nunique())

        profiles: dict[str, WalletProfile] = {}

        # Sort by timestamp so we process chronologically (matters for first_trade)
        sorted_trades = self._trades.sort_values("timestamp")

        for row in sorted_trades.itertuples(index=False):
            wallet = row.wallet
            ts = int(row.timestamp)
            market_id = row.market_id
            side = row.side
            size = float(row.size_usdc)

            if wallet not in profiles:
                profiles[wallet] = WalletProfile(
                    wallet=wallet,
                    first_trade_ts=ts,
                    total_volume_usdc=0.0,
                )

            p = profiles[wallet]
            p.total_volume_usdc += size
            key = (market_id, side)
            p.market_side_volume[key] = p.market_side_volume.get(key, 0.0) + size
            direction = getattr(row, "direction", None) or "UNKNOWN"
            dir_key = (market_id, side, direction)
            p.market_side_dir_volume[dir_key] = p.market_side_dir_volume.get(dir_key, 0.0) + size

        logger.info("Built profiles for %d wallets", len(profiles))
        return profiles

    def summary_dataframe(self, profiles: dict[str, WalletProfile]) -> pd.DataFrame:
        """Convert profiles to a flat DataFrame for inspection / export."""
        rows = []
        for wallet, p in profiles.items():
            for (market_id, side), vol in p.market_side_volume.items():
                rows.append({
                    "wallet": wallet,
                    "market_id": market_id,
                    "side": side,
                    "volume_usdc": vol,
                    "total_volume_usdc": p.total_volume_usdc,
                    "concentration": p.concentration(market_id, side),
                    "first_trade_ts": p.first_trade_ts,
                })
        return pd.DataFrame(rows)
