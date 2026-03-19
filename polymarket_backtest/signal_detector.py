"""
SignalDetector: Round 2 signal detection with two signal types.

Signal A — Burner Account
  A new wallet (≤14 days old on-chain) with concentrated, conviction-style
  trading in a market. Requires 4 of 5 criteria:
    1. WALLET_AGE     — on-chain first tx ≤ 14 days before the triggering trade
                        (via Polygonscan; falls back to dataset-first-trade if unavailable)
    2. SIZE           — single trade > threshold OR cumulative (market, side) > $25K
    3. CONCENTRATION  — > 80% of wallet's Polymarket volume in this (market, side)
    4. PRICE_INSENSITIVITY — bought at rising prices (≥10¢ spread across trades)
    5. BUY_DIRECTION  — net buyer (more USDC bought than sold in this outcome)

  Logged per signal: which 4 of 5 criteria were met.

Signal C — Volume Surge
  A market-level anomaly: trading volume in a short window spikes far above the
  market's own rolling baseline. No wallet-level criteria required — this fires
  once per market per surge event, identifying when aggregate interest suddenly
  accelerates.

  Method: within each market, compute hourly volume. A surge fires when an hour's
  volume exceeds SURGE_MULTIPLIER × (rolling 7-day median hourly volume).
  Minimum baseline volume required to suppress noise on illiquid markets.

Wash trade exclusion applies to Signal A only (wallet-level filter).
Signal C is market-level and not affected by individual wallet wash trading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config
from polygonscan_client import PolygonscanClient
from wallet_profiler import WalletProfile
from wash_trade_filter import WashTradeFilter

logger = logging.getLogger(__name__)

# ── Signal A config ────────────────────────────────────────────────────────────
BURNER_AGE_DAYS = 14                # wallet must be ≤ this old on-chain
MIN_CRITERIA_A = 4                  # require 4 of 5 criteria

# ── Signal C config ────────────────────────────────────────────────────────────
SURGE_WINDOW_SECONDS = 3600         # 1-hour bucket
SURGE_LOOKBACK_HOURS = 7 * 24      # 7-day rolling baseline
SURGE_MULTIPLIER = 5.0             # volume must be 5× median to fire
SURGE_MIN_BASELINE_USDC = 500.0    # ignore surges in near-zero-volume markets


@dataclass
class SignalA:
    """Burner account signal."""
    signal_type: str = "A"
    market_id: str = ""
    wallet: str = ""
    trigger_timestamp: int = 0
    side: str = ""              # YES or NO
    trigger_price: float = 0.0
    trigger_trade_size: float = 0.0
    cumulative_position: float = 0.0
    threshold: float = 0.0
    criteria_met: dict[str, bool] = field(default_factory=dict)
    n_criteria: int = 0
    wallet_age_days: float | None = None   # None = Polygonscan unavailable
    resolution: str | None = None


@dataclass
class SignalC:
    """Volume surge signal."""
    signal_type: str = "C"
    market_id: str = ""
    trigger_timestamp: int = 0     # start of the surge hour
    surge_volume_usdc: float = 0.0
    baseline_volume_usdc: float = 0.0
    surge_ratio: float = 0.0       # surge_volume / baseline
    resolution: str | None = None


class SignalDetector:
    def __init__(
        self,
        all_trades: pd.DataFrame,
        wallet_profiles: dict[str, WalletProfile],
        market_metadata: dict[str, dict],
        polygonscan: PolygonscanClient | None = None,
    ) -> None:
        self._trades = all_trades.sort_values("timestamp").reset_index(drop=True)
        self._profiles = wallet_profiles
        self._metadata = market_metadata
        self._poly = polygonscan

        # Build wash trade exclusion set
        logger.info("Running wash trade filter...")
        wtf = WashTradeFilter(all_trades)
        self._wash_wallets = wtf.build_wash_set()
        if self._wash_wallets:
            logger.info("Excluding %d wash-trading wallets from Signal A", len(self._wash_wallets))

    # ── Public API ─────────────────────────────────────────────────────────────

    def detect_signal_a(self) -> list[SignalA]:
        """Detect burner account signals across all size thresholds."""
        all_signals: list[SignalA] = []
        for threshold in config.SIZE_THRESHOLDS:
            signals = self._detect_a_at_threshold(threshold)
            logger.info("Signal A @ $%s: %d signals", f"{threshold:,.0f}", len(signals))
            all_signals.extend(signals)

        self._annotate_resolution(all_signals)
        logger.info("Signal A total: %d", len(all_signals))
        return all_signals

    def detect_signal_c(self) -> list[SignalC]:
        """Detect volume surge signals across all markets."""
        signals: list[SignalC] = []
        for market_id, group in self._trades.groupby("market_id"):
            market_signals = self._detect_surges(str(market_id), group)
            signals.extend(market_signals)

        self._annotate_resolution(signals)
        logger.info("Signal C total: %d surge events", len(signals))
        return signals

    # ── Signal A internals ─────────────────────────────────────────────────────

    def _detect_a_at_threshold(self, threshold: float) -> list[SignalA]:
        signals: list[SignalA] = []
        # Track whether signal already fired per (wallet, market, side)
        fired: set[tuple[str, str, str]] = set()
        # Running state for cumulative position and price tracking
        cum_usdc: dict[tuple[str, str, str], float] = {}
        buy_prices: dict[tuple[str, str, str], list[float]] = {}

        for row in self._trades.itertuples(index=False):
            wallet = row.wallet
            market_id = row.market_id
            side = row.side
            price = float(row.price)
            size = float(row.size_usdc)
            ts = int(row.timestamp)
            direction = getattr(row, "direction", None)

            key = (wallet, market_id, side)

            # Accumulate cumulative BUY volume and prices (for price insensitivity)
            if direction == "BUY":
                cum_usdc[key] = cum_usdc.get(key, 0.0) + size
                buy_prices.setdefault(key, []).append(price)

            if key in fired:
                continue

            # Price floor/ceiling: ignore lottery tickets and already-priced-in bets
            if not (config.SIGNAL_A_MIN_PRICE <= price <= config.SIGNAL_A_MAX_PRICE):
                continue

            # Wash trade exclusion
            if wallet in self._wash_wallets:
                continue

            profile = self._profiles.get(wallet)
            if profile is None:
                continue

            # Score the 4 cheap (no API call) criteria first.
            # Only look up wallet age if at least 3 others pass — otherwise
            # we can't reach MIN_CRITERIA_A (4) regardless.
            cheap = self._score_cheap_criteria(
                market_id=market_id,
                side=side,
                trade_size=size if direction == "BUY" else 0.0,
                cumulative=cum_usdc.get(key, 0.0),
                buy_prices_list=buy_prices.get(key, []),
                profile=profile,
                threshold=threshold,
            )
            if sum(cheap.values()) < MIN_CRITERIA_A - 1:
                continue

            age_days = self._get_wallet_age(wallet, ts)
            wallet_age_ok = self._eval_wallet_age(age_days, wallet, ts, profile)
            criteria = {"wallet_age": wallet_age_ok, **cheap}

            n_met = sum(criteria.values())
            if n_met >= MIN_CRITERIA_A:
                fired.add(key)
                signals.append(SignalA(
                    market_id=market_id,
                    wallet=wallet,
                    trigger_timestamp=ts,
                    side=side,
                    trigger_price=price,
                    trigger_trade_size=size,
                    cumulative_position=cum_usdc.get(key, 0.0),
                    threshold=threshold,
                    criteria_met=criteria,
                    n_criteria=n_met,
                    wallet_age_days=age_days,
                ))

        return signals

    def _score_cheap_criteria(
        self,
        market_id: str,
        side: str,
        trade_size: float,
        cumulative: float,
        buy_prices_list: list[float],
        profile: WalletProfile,
        threshold: float,
    ) -> dict[str, bool]:
        """Score the 4 criteria that require no API calls."""
        return {
            "size": (trade_size >= threshold or cumulative >= config.CUMULATIVE_THRESHOLD),
            "concentration": profile.concentration(market_id, side) >= config.CONCENTRATION_THRESHOLD,
            "price_insensitivity": self._check_price_insensitivity(buy_prices_list),
            "net_buyer": profile.is_net_buyer(market_id, side),
        }

    def _eval_wallet_age(
        self,
        age_days: float | None,
        wallet: str,
        trade_ts: int,
        profile: WalletProfile,
    ) -> bool:
        """Evaluate wallet_age criterion from a pre-fetched age_days value."""
        if age_days is not None:
            return age_days <= BURNER_AGE_DAYS
        # Fallback: dataset-relative freshness
        freshness_window = config.FRESHNESS_WINDOW_HOURS * 3600
        return profile.is_fresh_at(trade_ts, freshness_window)

    def _get_wallet_age(self, wallet: str, as_of_ts: int) -> float | None:
        if self._poly is None:
            return None
        return self._poly.wallet_age_days(wallet, as_of_ts)

    @staticmethod
    def _check_price_insensitivity(prices: list[float]) -> bool:
        if len(prices) < 2:
            return False
        return (max(prices) - min(prices)) >= config.PRICE_INSENSITIVITY_DELTA

    # ── Signal C internals ─────────────────────────────────────────────────────

    def _detect_surges(self, market_id: str, trades: pd.DataFrame) -> list[SignalC]:
        if trades.empty:
            return []

        # Bucket volume into hourly bins
        trades = trades.copy()
        trades["hour_bucket"] = (trades["timestamp"] // SURGE_WINDOW_SECONDS) * SURGE_WINDOW_SECONDS
        hourly = trades.groupby("hour_bucket")["size_usdc"].sum().sort_index()

        if len(hourly) < 2:
            return []

        signals = []
        lookback_buckets = SURGE_LOOKBACK_HOURS  # 1 bucket = 1 hour

        for i, (hour_ts, vol) in enumerate(hourly.items()):
            # Rolling median of preceding hours (exclude current bucket)
            start = max(0, i - lookback_buckets)
            prior_vols = hourly.iloc[start:i]
            if prior_vols.empty:
                continue

            baseline = float(prior_vols.median())
            if baseline < SURGE_MIN_BASELINE_USDC:
                continue

            ratio = vol / baseline
            if ratio >= SURGE_MULTIPLIER:
                signals.append(SignalC(
                    market_id=market_id,
                    trigger_timestamp=int(hour_ts),
                    surge_volume_usdc=float(vol),
                    baseline_volume_usdc=baseline,
                    surge_ratio=ratio,
                ))

        return signals

    # ── Shared ────────────────────────────────────────────────────────────────

    def _annotate_resolution(self, signals: list) -> None:
        for signal in signals:
            meta = self._metadata.get(signal.market_id, {})
            signal.resolution = meta.get("resolution")

    # ── DataFrame helpers ─────────────────────────────────────────────────────

    @staticmethod
    def signals_a_to_dataframe(signals: list[SignalA]) -> pd.DataFrame:
        if not signals:
            return pd.DataFrame()
        rows = []
        for s in signals:
            row = {
                "signal_type": s.signal_type,
                "market_id": s.market_id,
                "wallet": s.wallet,
                "trigger_timestamp": s.trigger_timestamp,
                "side": s.side,
                "trigger_price": s.trigger_price,
                "trigger_trade_size": s.trigger_trade_size,
                "cumulative_position": s.cumulative_position,
                "threshold": s.threshold,
                "n_criteria": s.n_criteria,
                "wallet_age_days": s.wallet_age_days,
                "resolution": s.resolution,
            }
            row.update({f"criterion_{k}": v for k, v in s.criteria_met.items()})
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def signals_c_to_dataframe(signals: list[SignalC]) -> pd.DataFrame:
        if not signals:
            return pd.DataFrame()
        return pd.DataFrame([{
            "signal_type": s.signal_type,
            "market_id": s.market_id,
            "trigger_timestamp": s.trigger_timestamp,
            "surge_volume_usdc": s.surge_volume_usdc,
            "baseline_volume_usdc": s.baseline_volume_usdc,
            "surge_ratio": s.surge_ratio,
            "resolution": s.resolution,
        } for s in signals])
