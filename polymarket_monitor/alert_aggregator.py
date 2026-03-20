"""
AlertAggregator: composite scoring and tier classification.

Scoring per source (all contribute 0.0 / 0.5 / 1.0):
  signal_c  — volume surge score from SignalC.detect_surge()
  news      — news cross-reference score from NewsChecker.check()
  pizzint   — PizzINT DOUGHCON score (live from API; 0.0 for non-military markets)
  insider   — open-source insider tracker alert (placeholder, always 0.0 in Phase 1)

Composite = signal_c + news + pizzint + insider  (max 4.0)

Tier logic:
  HIGH   — composite >= 2.5  OR  (signal_c >= 0.5 AND news == 1.0)
  MEDIUM — composite >= 1.5
  LOW    — composite >= 0.5  (bare surge, no corroboration)
  None   — composite < 0.5   (no meaningful signal)

Additional gates:
  - Price gate: skip if YES price < SIGNAL_C_MIN_PRICE (lottery ticket)
  - Deduplication: skip if last alert for this market was < ALERT_COOLDOWN_SECONDS ago
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import config
from news_checker import NewsChecker, NewsResult
from pizzint_monitor import PizzINTMonitor
from signal_c import SurgeEvent
from state import StateDB

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    market_id: str
    tier: str                     # 'HIGH', 'MEDIUM', 'LOW'
    composite_score: float
    signal_c_score: float
    news_score: float
    pizzint_score: float
    insider_score: float          # 0.0 in Phase 1
    surge_event: SurgeEvent
    news_result: NewsResult
    yes_price: float | None
    no_price: float | None
    market_question: str
    resolution_date: str
    slug: str
    yes_price_change_pct: float | None = None  # % change from 24h ago (if available)


class AlertAggregator:
    def __init__(
        self,
        db: StateDB,
        news_checker: NewsChecker,
        pizzint: PizzINTMonitor | None = None,
    ) -> None:
        self._db = db
        self._news = news_checker
        self._pizzint = pizzint

    def evaluate(
        self,
        surge_event: SurgeEvent,
        market_meta: dict,
    ) -> Alert | None:
        """
        Compute composite score for a detected surge and return an Alert
        if it meets the minimum tier threshold, or None to suppress.
        """
        market_id = surge_event.market_id
        yes_price = surge_event.yes_price
        no_price = surge_event.no_price

        # ── Price gate ───────────────────────────────────────────────────────
        if yes_price is not None and yes_price < config.SIGNAL_C_MIN_PRICE:
            logger.debug(
                "Market %s suppressed: YES price %.3f below floor %.3f",
                market_id[:16], yes_price, config.SIGNAL_C_MIN_PRICE,
            )
            return None

        # ── Deduplication ────────────────────────────────────────────────────
        last_alert = self._db.last_alert_at(market_id)
        if last_alert and (time.time() - last_alert) < config.ALERT_COOLDOWN_SECONDS:
            logger.debug(
                "Market %s suppressed: alerted %d min ago (cooldown %d min)",
                market_id[:16],
                int((time.time() - last_alert) / 60),
                config.ALERT_COOLDOWN_SECONDS // 60,
            )
            return None

        # ── Scores ───────────────────────────────────────────────────────────
        signal_c_score = surge_event.signal_c_score  # 0.5 or 1.0

        question = market_meta.get("question", "")
        keywords = market_meta.get("keywords", [])
        if isinstance(keywords, str):
            import json
            try:
                keywords = json.loads(keywords)
            except Exception:
                keywords = []
        news_result = self._news.check(question, keywords)
        news_score = news_result.score

        # PizzINT score: only applied for US military action markets
        pizzint_relevant = market_meta.get("pizzint_relevant", False)
        if pizzint_relevant and self._pizzint is not None:
            pizzint_score = self._pizzint.score
        else:
            pizzint_score = 0.0

        insider_score = 0.0  # placeholder — Phase 2

        composite = signal_c_score + news_score + pizzint_score + insider_score

        # ── Tier classification ───────────────────────────────────────────────
        if composite >= config.HIGH_TIER_COMPOSITE or (
            signal_c_score >= config.HIGH_TIER_SIGNAL_C_MIN
            and news_score >= config.HIGH_TIER_NEWS_UNEXPLAINED
        ):
            tier = "HIGH"
        elif composite >= config.MEDIUM_TIER_COMPOSITE:
            tier = "MEDIUM"
        elif composite >= 0.5:
            tier = "LOW"
        else:
            return None

        return Alert(
            market_id=market_id,
            tier=tier,
            composite_score=composite,
            signal_c_score=signal_c_score,
            news_score=news_score,
            pizzint_score=pizzint_score,
            insider_score=insider_score,
            surge_event=surge_event,
            news_result=news_result,
            yes_price=yes_price,
            no_price=no_price,
            market_question=question,
            resolution_date=market_meta.get("resolution_date", ""),
            slug=market_meta.get("slug", ""),
        )
