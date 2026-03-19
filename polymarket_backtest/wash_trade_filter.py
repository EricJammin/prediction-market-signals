"""
WashTradeFilter: identifies wallets whose trading activity looks like wash trading.

A wallet is flagged as a wash trader if BOTH conditions hold:

  1. ROUND-TRIP pattern: the wallet executed at least 2 buy+sell pairs on the
     SAME outcome (YES or NO) within a short window (default 1 hour), and the
     prices were similar (within 5 cents). This suggests cyclic trading rather
     than directional conviction.

  2. NEAR-ZERO NET POSITION: across all trading in a (market, side) pair,
     |total_bought - total_sold| / total_volume < NET_POSITION_THRESHOLD (5%).
     A genuine informational trader should have strong net directional exposure.

Design note: Polymarket's data-api returns one record per taker trade. We can
see each wallet's BUY and SELL activity (via the `direction` column), but we
cannot see counterparty addresses, so we cannot detect coordinated ring trading
between wallets. The single-wallet round-trip + net-position approach catches
self-cleaning wash trades without counterparty data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

import config

logger = logging.getLogger(__name__)

# How close prices must be to count as a round-trip (same outcome, opposite direction)
ROUND_TRIP_PRICE_TOLERANCE = 0.05   # cents/probability points
# Max seconds between buy and matching sell to count as a round-trip
ROUND_TRIP_WINDOW_SECONDS = 3600    # 1 hour
# Min number of round-trip pairs to flag a wallet
MIN_ROUND_TRIPS = 2
# Max |bought - sold| / total_volume to consider net position "near zero"
NET_POSITION_THRESHOLD = 0.05       # 5%


@dataclass
class WashTradeResult:
    wallet: str
    market_id: str
    side: str              # YES or NO outcome being washed
    round_trips: int       # number of qualifying round-trip pairs found
    net_position_ratio: float   # |bought - sold| / total_volume (lower = more washed)
    is_wash_trader: bool


class WashTradeFilter:
    """
    Scans all trades and flags (wallet, market_id, side) triples that exhibit
    wash-trading patterns.
    """

    def __init__(self, all_trades: pd.DataFrame) -> None:
        if "direction" not in all_trades.columns:
            raise ValueError(
                "all_trades must have a 'direction' column (BUY/SELL). "
                "Re-fetch data with the updated DataCollector."
            )
        self._trades = all_trades.sort_values("timestamp").reset_index(drop=True)

    def build_wash_set(self) -> set[str]:
        """
        Run wash trade detection and return the set of wallet addresses flagged
        as wash traders (across any market+side pair).
        """
        results = self.analyze_all()
        flagged = {r.wallet for r in results if r.is_wash_trader}
        logger.info(
            "Wash trade filter: %d wallets flagged out of %d analyzed",
            len(flagged),
            len(results),
        )
        return flagged

    def analyze_all(self) -> list[WashTradeResult]:
        """Run analysis on every (wallet, market_id, side) triple."""
        results = []
        grouped = self._trades.groupby(["wallet", "market_id", "side"])
        for (wallet, market_id, side), group in grouped:
            result = self._analyze_group(wallet, market_id, side, group)
            results.append(result)
        return results

    # ── Private ────────────────────────────────────────────────────────────────

    def _analyze_group(
        self,
        wallet: str,
        market_id: str,
        side: str,
        trades: pd.DataFrame,
    ) -> WashTradeResult:
        buys = trades[trades["direction"] == "BUY"].copy()
        sells = trades[trades["direction"] == "SELL"].copy()

        total_bought = float(buys["size_usdc"].sum())
        total_sold = float(sells["size_usdc"].sum())
        total_volume = total_bought + total_sold

        # Net position ratio: 0 = perfectly washed, 1 = purely directional
        if total_volume > 0:
            net_ratio = abs(total_bought - total_sold) / total_volume
        else:
            net_ratio = 1.0  # no volume at all — not a wash trader

        round_trips = self._count_round_trips(buys, sells)
        is_wash = (
            round_trips >= MIN_ROUND_TRIPS
            and net_ratio < NET_POSITION_THRESHOLD
        )

        return WashTradeResult(
            wallet=wallet,
            market_id=market_id,
            side=side,
            round_trips=round_trips,
            net_position_ratio=net_ratio,
            is_wash_trader=is_wash,
        )

    @staticmethod
    def _count_round_trips(buys: pd.DataFrame, sells: pd.DataFrame) -> int:
        """
        Count how many buy trades have a matching sell trade:
          - within ROUND_TRIP_WINDOW_SECONDS after the buy
          - at a price within ROUND_TRIP_PRICE_TOLERANCE of the buy price

        Each sell can only be used to match one buy (greedy, earliest-first).
        """
        if buys.empty or sells.empty:
            return 0

        buy_records = buys[["timestamp", "price"]].sort_values("timestamp").to_dict("records")
        sell_records = sells[["timestamp", "price"]].sort_values("timestamp").to_dict("records")

        used_sells: set[int] = set()
        round_trips = 0

        for buy in buy_records:
            buy_ts = buy["timestamp"]
            buy_price = buy["price"]
            window_end = buy_ts + ROUND_TRIP_WINDOW_SECONDS

            for i, sell in enumerate(sell_records):
                if i in used_sells:
                    continue
                sell_ts = sell["timestamp"]
                if sell_ts < buy_ts:
                    continue
                if sell_ts > window_end:
                    break  # sells are sorted; no point looking further
                sell_price = sell["price"]
                if abs(sell_price - buy_price) <= ROUND_TRIP_PRICE_TOLERANCE:
                    used_sells.add(i)
                    round_trips += 1
                    break

        return round_trips
