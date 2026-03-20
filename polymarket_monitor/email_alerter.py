"""
EmailAlerter: daily digest sent via Gmail SMTP.

Fires once per day at DIGEST_HOUR_UTC (default 23 UTC / 6 PM ET).
Includes:
  - All Signal C + Signal A alerts from the past 24 hours
  - Current PizzINT DOUGHCON level
  - All watched markets with current YES price and category
  - System health: last successful poll time, any error count

Configuration (via .env):
  ALERT_EMAIL_FROM      — Gmail address to send from
  ALERT_EMAIL_PASSWORD  — Gmail App Password (not your account password)
  ALERT_EMAIL_TO        — Recipient address (can be the same Gmail)

If any of the three env vars are missing, digest is skipped and a warning
is logged (not an error — email is optional).
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.mime.text import MIMEText

import config
from state import StateDB

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


class EmailAlerter:
    def __init__(self) -> None:
        self._from    = os.getenv("ALERT_EMAIL_FROM", "")
        self._password = os.getenv("ALERT_EMAIL_PASSWORD", "")
        self._to      = os.getenv("ALERT_EMAIL_TO", "")
        self._last_digest_day: int = -1  # calendar day (0-6) of last sent digest

        if not all([self._from, self._password, self._to]):
            logger.info(
                "Email digest not configured — set ALERT_EMAIL_FROM, "
                "ALERT_EMAIL_PASSWORD, ALERT_EMAIL_TO in .env to enable."
            )

    @property
    def configured(self) -> bool:
        return bool(self._from and self._password and self._to)

    # ── Public API ─────────────────────────────────────────────────────────────

    def maybe_send_digest(
        self,
        db: StateDB,
        pizzint_status: str = "",
        error_count: int = 0,
        last_poll_ts: int = 0,
    ) -> bool:
        """
        Send the daily digest if the current UTC hour == DIGEST_HOUR_UTC and
        we haven't already sent today. Returns True if digest was sent.
        """
        if not self.configured:
            return False

        now_utc = time.gmtime()
        current_hour = now_utc.tm_hour
        today_yday   = now_utc.tm_yday  # day-of-year avoids year-boundary edge case

        if current_hour != config.DIGEST_HOUR_UTC:
            return False
        if today_yday == self._last_digest_day:
            return False  # already sent this hour

        sent = self.send_digest(db, pizzint_status, error_count, last_poll_ts)
        if sent:
            self._last_digest_day = today_yday
        return sent

    def send_digest(
        self,
        db: StateDB,
        pizzint_status: str = "",
        error_count: int = 0,
        last_poll_ts: int = 0,
        dry_run: bool = False,
    ) -> bool:
        """
        Build and send the daily digest immediately.
        dry_run=True prints to stdout instead of sending.
        Returns True on success.
        """
        body = self._build_body(db, pizzint_status, error_count, last_poll_ts)
        subject = self._build_subject(db)

        if dry_run:
            print(f"\n{'='*60}")
            print(f"DIGEST (dry-run)")
            print(f"Subject: {subject}")
            print(f"{'='*60}")
            print(body)
            print(f"{'='*60}\n")
            return True

        if not self.configured:
            logger.warning("Email not configured — digest not sent.")
            return False

        return self._send(subject, body)

    # ── Building the digest body ───────────────────────────────────────────────

    def _build_subject(self, db: StateDB) -> str:
        since = int(time.time()) - 86400
        alerts = db.get_recent_alerts(since)
        n = len(alerts)
        date_str = time.strftime("%Y-%m-%d", time.gmtime())
        if n == 0:
            return f"Polymarket Monitor — Daily Digest {date_str} (no alerts)"
        tiers = [a["tier"] for a in alerts]
        if "HIGH" in tiers:
            return f"Polymarket Monitor — Daily Digest {date_str} ⚑ {n} alert(s) incl. HIGH"
        return f"Polymarket Monitor — Daily Digest {date_str} ({n} alert(s))"

    def _build_body(
        self,
        db: StateDB,
        pizzint_status: str,
        error_count: int,
        last_poll_ts: int,
    ) -> str:
        now = int(time.time())
        since_24h = now - 86400
        lines: list[str] = []

        # ── Header ────────────────────────────────────────────────────────────
        date_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
        lines += [
            f"Polymarket Monitor — Daily Digest",
            f"Generated: {date_str}",
            "",
        ]

        # ── System health ─────────────────────────────────────────────────────
        lines.append("SYSTEM HEALTH")
        lines.append("-" * 40)
        if last_poll_ts:
            age_min = int((now - last_poll_ts) / 60)
            lines.append(f"Last poll:   {time.strftime('%H:%M UTC', time.gmtime(last_poll_ts))} ({age_min}m ago)")
        else:
            lines.append("Last poll:   unknown")
        lines.append(f"Error count: {error_count} (since last digest)")
        lines.append("")

        # ── PizzINT ───────────────────────────────────────────────────────────
        lines.append("PIZZINT")
        lines.append("-" * 40)
        lines.append(pizzint_status or "Not available")
        lines.append("")

        # ── Alerts from last 24h ──────────────────────────────────────────────
        alerts = db.get_recent_alerts(since_24h)
        lines.append(f"ALERTS (LAST 24H) — {len(alerts)} total")
        lines.append("-" * 40)

        if not alerts:
            lines.append("No alerts fired.")
        else:
            for a in alerts:
                fired_str = time.strftime("%H:%M UTC", time.gmtime(a["fired_at"]))
                meta = db.get_market_meta(a["market_id"])
                question = (meta.get("question", a["market_id"])[:70]) if meta else a["market_id"][:24]
                tier_mark = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(a["tier"], "⚪")
                lines.append(
                    f"  {tier_mark} {a['tier']:<6}  {fired_str}  "
                    f"ratio={a['surge_ratio']:.1f}x  score={a['signal_score']:.1f}  "
                    f"{question}"
                )
        lines.append("")

        # ── Watched markets table ─────────────────────────────────────────────
        markets = db.get_all_watched_markets()
        lines.append(f"WATCHED MARKETS ({len(markets)})")
        lines.append("-" * 40)
        lines.append(f"  {'YES':>5}  {'Category':<14}  Question")
        lines.append(f"  {'---':>5}  {'-'*14}  --------")

        for m in sorted(markets, key=lambda x: x.get("category", "")):
            yes_price, _ = db.get_price(m["market_id"])
            price_str = f"{yes_price:.2f}" if yes_price is not None else "  — "
            cat = (m.get("category") or "")[:14]
            question = (m.get("question") or m["market_id"])[:60]
            lines.append(f"  {price_str:>5}  {cat:<14}  {question}")

        lines.append("")
        lines.append("—")
        lines.append("Polymarket Monitor | To adjust settings, edit polymarket_monitor/config.py")

        return "\n".join(lines)

    # ── SMTP send ─────────────────────────────────────────────────────────────

    def _send(self, subject: str, body: str) -> bool:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = self._from
        msg["To"]      = self._to

        try:
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self._from, self._password)
                smtp.sendmail(self._from, [self._to], msg.as_string())
            logger.info("Daily digest sent to %s", self._to)
            return True
        except Exception as exc:
            logger.error("Email digest send failed: %s", exc)
            return False
