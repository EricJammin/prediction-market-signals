"""
TelegramAlerter: formats and sends alerts to a Telegram chat via the Bot API.

Uses httpx for the HTTP POST (sync, no event loop needed).
HTML parse_mode is used for formatting — all user-supplied strings are escaped.

Alert format example:
  🔴 HIGH ALERT: Iran Strike Market
  Volume: 12.4× baseline ($24,000 vs $1,940/hr)
  Price: YES $0.18 → NOW $0.31 (+72%)
  News: ✅ No recent articles found (unexplained)
  PizzINT: — (not monitored)
  Insider tracker: — (no alerts)
  Resolution: 2026-02-28
  Score: 2.0 / 4.0
  [View on Polymarket]
"""

from __future__ import annotations

import html
import logging
import os

import httpx

from alert_aggregator import Alert
from signal_a import SignalAEvent

logger = logging.getLogger(__name__)

POLYMARKET_BASE = "https://polymarket.com/event"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_TIER_EMOJI = {
    "HIGH":   "🔴",
    "MEDIUM": "🟡",
    "LOW":    "🟢",
}


class TelegramAlerter:
    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

        if not self._token or not self._chat_id:
            logger.warning(
                "Telegram credentials not set — alerts will be logged only. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
            )

    def send_alert(self, alert: Alert) -> bool:
        """
        Format and POST alert to Telegram Bot API.
        Returns True if HTTP 200, False otherwise.
        Never raises — logs error and returns False on failure.
        """
        message = self._format_message(alert)
        logger.info(
            "[%s] %s | ratio=%.1fx | score=%.1f | news=%.1f",
            alert.tier, alert.market_question[:60],
            alert.surge_event.surge_ratio, alert.composite_score, alert.news_score,
        )

        if not self._token or not self._chat_id:
            logger.info("(Telegram not configured — alert logged only)")
            return False

        url = TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            resp = httpx.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            logger.error(
                "Telegram API error %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    def send_signal_a_alert(self, event: SignalAEvent) -> bool:
        """Format and send a TIER 2 Signal A burner wallet alert."""
        message = self._format_signal_a(event)
        question_short = event.question[:80] if event.question else event.market_id[:24]
        logger.info(
            "[SIGNAL A] %s | wallet=%s | %d/5 criteria | buy=$%,.0f",
            question_short, event.wallet[:14], event.n_criteria, event.cumulative_buy_usdc,
        )
        if not self._token or not self._chat_id:
            logger.info("(Telegram not configured — Signal A alert logged only)")
            return False
        url = TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram Signal A send failed: %s", exc)
            return False

    def send_text(self, text: str) -> bool:
        """Send a plain text message (for startup notifications, etc.)."""
        if not self._token or not self._chat_id:
            return False
        url = TELEGRAM_API.format(token=self._token)
        try:
            resp = httpx.post(
                url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram send_text failed: %s", exc)
            return False

    # ── Formatting ─────────────────────────────────────────────────────────────

    @staticmethod
    def _format_message(alert: Alert) -> str:
        e = html.escape  # escape all user-supplied strings for HTML parse_mode
        surge = alert.surge_event

        tier_emoji = _TIER_EMOJI.get(alert.tier, "⚪")
        question = e(alert.market_question[:120])
        resolution = e(alert.resolution_date) if alert.resolution_date else "unknown"

        # Volume line
        vol_line = (
            f"Volume: <b>{surge.surge_ratio:.1f}× baseline</b>"
            f"  (${surge.surge_volume_usdc:,.0f} vs ${surge.baseline_volume_usdc:,.0f}/hr)"
        )

        # Price line
        if alert.yes_price is not None:
            yes_pct = int(alert.yes_price * 100)
            price_line = f"Price: YES <b>${alert.yes_price:.3f}</b> ({yes_pct}% implied)"
        else:
            price_line = "Price: <i>unknown</i>"

        # News line
        if alert.news_score >= 1.0:
            news_line = "News: ✅ <b>No recent articles</b> — surge unexplained"
        elif alert.news_score >= 0.5:
            news_line = "News: ⚠️ 1 article found (ambiguous)"
        else:
            n = len(alert.news_result.matched_articles)
            news_line = f"News: 📰 {n} recent articles — likely news-driven"
            if alert.news_result.matched_articles:
                top = e(alert.news_result.matched_articles[0][:80])
                news_line += f"\n  └ <i>{top}</i>"

        # Scores
        score_line = (
            f"Score: <b>{alert.composite_score:.1f}</b> / 4.0"
            f"  (signal={alert.signal_c_score:.1f}"
            f"  news={alert.news_score:.1f}"
            f"  pizzint=—  insider=—)"
        )

        # Link
        if alert.slug:
            link = f'<a href="{POLYMARKET_BASE}/{e(alert.slug)}">View on Polymarket</a>'
        else:
            link = f'<a href="https://polymarket.com">Polymarket</a>'

        return "\n".join([
            f"{tier_emoji} <b>{alert.tier} ALERT</b>: {question}",
            "",
            vol_line,
            price_line,
            news_line,
            f"PizzINT: — (Phase 2)",
            f"Insider tracker: — (Phase 2)",
            f"Resolution: {resolution}",
            score_line,
            link,
        ])

    @staticmethod
    def _format_signal_a(event: SignalAEvent) -> str:
        e = html.escape
        question = e(event.question[:120]) if event.question else e(event.market_id[:40])
        wallet_short = e(event.wallet[:18] + "…")

        # Wallet age line
        if event.wallet_age_days is not None:
            age_str = f"{int(event.wallet_age_days)}d old on-chain"
        else:
            age_str = "age unknown (Polygonscan unavailable)"

        # Criteria summary
        criteria_lines = []
        labels = {
            "freshness":       "Wallet age",
            "size":            "Position size",
            "concentration":   "Concentration",
            "entry_price":     "Entry price",
            "not_wash_trader": "Not wash trader",
        }
        for key, label in labels.items():
            passed = event.criteria_met.get(key, False)
            mark = "✅" if passed else "✗"
            criteria_lines.append(f"  {mark} {label}")

        # Position details
        net = event.cumulative_buy_usdc - event.cumulative_sell_usdc
        conc_total = event.cumulative_buy_usdc  # relative to this market — not wallet total
        pos_line = (
            f"Position: <b>${event.cumulative_buy_usdc:,.0f}</b> YES at ${event.first_buy_price:.3f}"
        )
        if event.cumulative_sell_usdc > 0:
            pos_line += f"  (net ${net:,.0f} after ${event.cumulative_sell_usdc:,.0f} sells)"

        price_line = ""
        if event.yes_price is not None:
            yes_pct = int(event.yes_price * 100)
            price_line = f"Current YES price: <b>${event.yes_price:.3f}</b> ({yes_pct}%)"

        import time as _time
        trigger_utc = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime(event.trigger_ts))

        return "\n".join(filter(None, [
            f"⚠️ <b>TIER 2 — Signal A: Burner Wallet</b>: {question}",
            "",
            f"Wallet: <code>{wallet_short}</code>  ({age_str})",
            f"Criteria: <b>{event.n_criteria}/5</b>",
            "\n".join(criteria_lines),
            "",
            pos_line,
            price_line,
            f"Triggered: {trigger_utc}",
            "",
            "<i>Brand-new wallet making concentrated bet. Evaluate immediately.</i>",
        ]))
