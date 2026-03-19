"""
WhaleDetector: scans all trades and fires whale signals when the composite
score reaches config.MIN_SIGNAL_SCORE (default 3 of 4 criteria).

Criteria (each worth 1 point):
  1. FRESHNESS     — wallet's first-ever trade was within 96h of this bet,
                     or this IS their first trade ever
  2. SIZE          — single trade > threshold (tested at $10K / $25K / $50K)
                     OR cumulative position in this (market, side) > $25K
  3. CONCENTRATION — > 80% of wallet's total Polymarket volume is in this market
  4. PRICE_INSENSITIVITY — wallet kept buying the same side after price rose
                           by > 10 cents (signals strong conviction / inside info)

Freshness and concentration are weighted most heavily per spec, so a signal
fires on (freshness + concentration + any 1 other) even without size — useful
for smaller but highly concentrated fresh wallets.

The detector deduplicates: if a wallet fires a signal on an intermediate trade
(e.g. trade 3 of 5), subsequent trades by the same wallet in the same market
don't fire new signals (they are folded into the existing signal's cumulative
position). However, the trigger trade is the first trade that pushed the score
to MIN_SIGNAL_SCORE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

import config
from wallet_profiler import WalletProfile

logger = logging.getLogger(__name__)


@dataclass
class WhaleSignal:
    market_id: str
    wallet: str
    trigger_timestamp: int          # unix seconds — timestamp of the triggering trade
    side: str                       # YES or NO
    trigger_price: float            # price of the triggering trade
    trigger_trade_size: float       # size (USDC) of the triggering trade
    cumulative_position: float      # total (market, side) position at trigger time
    score: int                      # 1-4 criteria met
    criteria: dict[str, bool]       # which criteria fired
    threshold: float                # SIZE threshold that produced this signal
    resolution: str | None = None   # populated after market resolves: YES / NO


@dataclass
class _WalletMarketState:
    """Mutable accumulator for a (wallet, market_id, side) triple."""
    cumulative_usdc: float = 0.0
    trade_prices: list[float] = field(default_factory=list)
    signal_fired: bool = False      # prevent duplicate signals per (wallet, market, side)
    min_price_seen: float = float("inf")
    max_price_when_buying: float = 0.0


class WhaleDetector:
    def __init__(
        self,
        all_trades: pd.DataFrame,
        wallet_profiles: dict[str, WalletProfile],
        market_metadata: dict[str, dict],
        size_threshold: float | None = None,
    ) -> None:
        """
        Args:
            all_trades:       normalised trades DataFrame
            wallet_profiles:  output of WalletProfiler.build_profiles()
            market_metadata:  condition_id → metadata dict
            size_threshold:   single SIZE threshold to use; if None, the detector
                              runs once per threshold in config.SIZE_THRESHOLDS and
                              returns the union of signals (tagged with their threshold)
        """
        self._trades = all_trades.sort_values("timestamp").reset_index(drop=True)
        self._profiles = wallet_profiles
        self._metadata = market_metadata
        self._threshold = size_threshold

    def detect_all(self) -> list[WhaleSignal]:
        """Run whale detection across all trades and all configured thresholds."""
        thresholds = (
            [self._threshold] if self._threshold is not None else config.SIZE_THRESHOLDS
        )
        all_signals: list[WhaleSignal] = []
        for threshold in thresholds:
            signals = self._detect_at_threshold(threshold)
            logger.info(
                "Threshold $%s: detected %d whale signals", f"{threshold:,.0f}", len(signals)
            )
            all_signals.extend(signals)

        # Annotate resolution
        for signal in all_signals:
            meta = self._metadata.get(signal.market_id, {})
            signal.resolution = meta.get("resolution")

        logger.info("Total whale signals: %d", len(all_signals))
        return all_signals

    def _detect_at_threshold(self, threshold: float) -> list[WhaleSignal]:
        freshness_window = config.FRESHNESS_WINDOW_HOURS * 3600
        signals: list[WhaleSignal] = []

        # state[(wallet, market_id, side)] → _WalletMarketState
        state: dict[tuple[str, str, str], _WalletMarketState] = {}

        for row in self._trades.itertuples(index=False):
            wallet = row.wallet
            market_id = row.market_id
            side = row.side
            price = float(row.price)
            size = float(row.size_usdc)
            ts = int(row.timestamp)

            profile = self._profiles.get(wallet)
            if profile is None:
                continue

            key = (wallet, market_id, side)
            if key not in state:
                state[key] = _WalletMarketState()
            s = state[key]

            # Update running state BEFORE scoring (so cumulative includes this trade)
            s.cumulative_usdc += size
            s.trade_prices.append(price)
            if price < s.min_price_seen:
                s.min_price_seen = price
            if price > s.max_price_when_buying:
                s.max_price_when_buying = price

            # Skip if signal already fired for this (wallet, market, side)
            if s.signal_fired:
                continue

            criteria = self._score_criteria(
                wallet=wallet,
                market_id=market_id,
                side=side,
                trade_ts=ts,
                trade_size=size,
                cumulative=s.cumulative_usdc,
                trade_prices=s.trade_prices,
                profile=profile,
                freshness_window=freshness_window,
                threshold=threshold,
            )

            score = sum(criteria.values())
            if score >= config.MIN_SIGNAL_SCORE:
                s.signal_fired = True
                signals.append(
                    WhaleSignal(
                        market_id=market_id,
                        wallet=wallet,
                        trigger_timestamp=ts,
                        side=side,
                        trigger_price=price,
                        trigger_trade_size=size,
                        cumulative_position=s.cumulative_usdc,
                        score=score,
                        criteria=criteria,
                        threshold=threshold,
                    )
                )

        return signals

    def _score_criteria(
        self,
        wallet: str,
        market_id: str,
        side: str,
        trade_ts: int,
        trade_size: float,
        cumulative: float,
        trade_prices: list[float],
        profile: WalletProfile,
        freshness_window: int,
        threshold: float,
    ) -> dict[str, bool]:
        return {
            "freshness": self._check_freshness(profile, trade_ts, freshness_window),
            "size": self._check_size(trade_size, cumulative, threshold),
            "concentration": self._check_concentration(profile, market_id, side),
            "price_insensitivity": self._check_price_insensitivity(trade_prices),
        }

    @staticmethod
    def _check_freshness(profile: WalletProfile, trade_ts: int, window: int) -> bool:
        return profile.is_fresh_at(trade_ts, window)

    @staticmethod
    def _check_size(trade_size: float, cumulative: float, threshold: float) -> bool:
        return trade_size >= threshold or cumulative >= config.CUMULATIVE_THRESHOLD

    @staticmethod
    def _check_concentration(
        profile: WalletProfile, market_id: str, side: str
    ) -> bool:
        return profile.concentration(market_id, side) >= config.CONCENTRATION_THRESHOLD

    @staticmethod
    def _check_price_insensitivity(prices: list[float]) -> bool:
        """
        True if the wallet placed multiple buys and the price rose by more than
        PRICE_INSENSITIVITY_DELTA between any two consecutive same-side purchases.
        """
        if len(prices) < 2:
            return False
        return (max(prices) - min(prices)) >= config.PRICE_INSENSITIVITY_DELTA

    # ── Reporting helpers ─────────────────────────────────────────────────────

    @staticmethod
    def signals_to_dataframe(signals: list[WhaleSignal]) -> pd.DataFrame:
        if not signals:
            return pd.DataFrame()
        return pd.DataFrame([
            {
                "market_id": s.market_id,
                "wallet": s.wallet,
                "trigger_timestamp": s.trigger_timestamp,
                "side": s.side,
                "trigger_price": s.trigger_price,
                "trigger_trade_size": s.trigger_trade_size,
                "cumulative_position": s.cumulative_position,
                "score": s.score,
                "threshold": s.threshold,
                "resolution": s.resolution,
                **{f"criterion_{k}": v for k, v in s.criteria.items()},
            }
            for s in signals
        ])
