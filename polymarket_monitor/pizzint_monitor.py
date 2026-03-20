"""
PizzINT monitor: tracks DOUGHCON military readiness level.

PizzINT (pizzint.watch / t.me/pizzintwatchers) publishes a DOUGHCON level
(1–5) indicating US military force posture. DOUGHCON 1–2 correlates with
imminent or high-probability US military action.

Two fetch methods (tried in order):
  Option A — Telegram Bot API getUpdates:
    Requires PIZZINT_CHANNEL_ID in .env (the channel/group chat ID where
    DOUGHCON updates appear) and TELEGRAM_BOT_TOKEN to be set. The bot must
    be a member of that chat. Parses incoming messages for DOUGHCON keywords.

  Option B — Web scraping (pizzint.watch):
    No configuration required. Scrapes the public website with regex fallbacks
    covering several possible HTML layouts. Used when Option A is not configured
    or fails.

DOUGHCON score mapping (per BUILD_SPEC.md):
  5 = Peacetime   → 0.0
  4 = Normal      → 0.0
  3 = Elevated    → 0.3
  2 = High        → 0.7
  1 = Imminent    → 1.0

PizzINT score only contributes to composite scoring for markets tagged
pizzint_relevant=True in market_watchlist.py (US military action markets).
For all other markets, the caller should treat the score as 0.0.

State is persisted to PIZZINT_STATE_PATH (data/pizzint_state.json) so the
last known DOUGHCON level survives monitor restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

logger = logging.getLogger(__name__)

PIZZINT_URL = "https://pizzint.watch"

# DOUGHCON level → composite score contribution
DOUGHCON_SCORES: dict[int, float] = {
    1: 1.0,   # Imminent
    2: 0.7,   # High
    3: 0.3,   # Elevated
    4: 0.0,   # Normal
    5: 0.0,   # Peacetime
}

DOUGHCON_LABELS: dict[int, str] = {
    1: "IMMINENT",
    2: "HIGH",
    3: "ELEVATED",
    4: "NORMAL",
    5: "PEACETIME",
}

# Regex patterns tried in order against page HTML or message text.
# Ordered from most specific (structural) to least specific (generic).
_DOUGHCON_PATTERNS: list[str] = [
    # Explicit label followed by digit
    r'DOUGHCON\s+(?:LEVEL\s+)?([1-5])\b',
    # Digit followed by known label names
    r'\b([1-5])\s*[-–:]\s*(?:Imminent|High|Elevated|Normal|Peacetime)\b',
    # HTML attribute/class value patterns (e.g. data-level="3", class="level-3")
    r'(?:data-level|doughcon-level|level)["\s=\-]+([1-5])\b',
    # JS variable patterns (e.g. doughcon = 3, level: 3)
    r'(?:doughcon|level)\s*[=:]\s*([1-5])\b',
    # Broadest fallback: any digit adjacent to "DOUGHCON" within 30 chars
    r'DOUGHCON.{0,30}?([1-5])\b',
]


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=config.RETRY_BACKOFF_SECONDS,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class PizzINTMonitor:
    def __init__(self) -> None:
        self._bot_token  = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._channel_id = os.getenv("PIZZINT_CHANNEL_ID", "")
        self._session    = _make_session()

        # Persisted state
        self._level: int          = 5      # default: Peacetime
        self._updated_at: int     = 0
        self._source: str         = "none"
        self._last_update_id: int = 0      # Telegram getUpdates cursor

        self._last_refresh: float = 0.0
        self._load_state()

        if self._channel_id:
            logger.info(
                "PizzINT: Telegram Option A enabled (channel %s)",
                self._channel_id,
            )
        else:
            logger.info("PizzINT: using web scrape option (pizzint.watch)")

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def score(self) -> float:
        """Current DOUGHCON score (0.0–1.0). Use this in composite scoring."""
        return DOUGHCON_SCORES.get(self._level, 0.0)

    @property
    def level(self) -> int:
        """Current DOUGHCON level (1–5)."""
        return self._level

    @property
    def label(self) -> str:
        """Human-readable DOUGHCON label."""
        return DOUGHCON_LABELS.get(self._level, "UNKNOWN")

    @property
    def updated_at(self) -> int:
        """Unix timestamp of last confirmed level update."""
        return self._updated_at

    def refresh(self) -> float:
        """
        Poll PizzINT for the current DOUGHCON level. Rate-limited to once
        per POLL_INTERVAL_SECONDS; returns cached score between polls.

        On fetch failure, retains the last known level (fail-safe: a transient
        network error should not drop the score to 0.0 / peacetime).

        Returns the current score (0.0–1.0).
        """
        now = time.time()
        if (now - self._last_refresh) < config.POLL_INTERVAL_SECONDS:
            return self.score  # still fresh from this cycle

        self._last_refresh = now
        new_level = self._fetch()

        if new_level is not None and new_level != self._level:
            logger.warning(
                "DOUGHCON changed: %d (%s) → %d (%s)",
                self._level, DOUGHCON_LABELS.get(self._level, "?"),
                new_level, DOUGHCON_LABELS.get(new_level, "?"),
            )
            self._level      = new_level
            self._updated_at = int(now)
            self._save_state()
        elif new_level is not None:
            logger.debug("DOUGHCON: %d (%s) — no change", self._level, self.label)

        return self.score

    def status_line(self) -> str:
        """Short status string for alert messages and digest."""
        age_min = int((time.time() - self._updated_at) / 60) if self._updated_at else None
        age_str = f", updated {age_min}m ago" if age_min is not None else ""
        return f"DOUGHCON {self._level} — {self.label} (score {self.score:.1f}{age_str})"

    # ── Fetch dispatch ─────────────────────────────────────────────────────────

    def _fetch(self) -> int | None:
        """Try Option A (Telegram) then Option B (web). Returns level or None."""
        if self._channel_id and self._bot_token:
            level = self._fetch_from_telegram()
            if level is not None:
                self._source = "telegram"
                return level

        level = self._fetch_from_web()
        if level is not None:
            self._source = "web"
        return level

    # ── Option A: Telegram Bot API ─────────────────────────────────────────────

    def _fetch_from_telegram(self) -> int | None:
        """
        Poll Bot API getUpdates for messages in the configured channel/group.
        Returns the most recent DOUGHCON level found, or None if no new
        DOUGHCON messages since the last poll.
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        params: dict[str, Any] = {
            "offset":           self._last_update_id + 1,
            "limit":            100,
            "timeout":          0,
            "allowed_updates":  json.dumps(["message", "channel_post"]),
        }
        try:
            resp = self._session.get(url, params=params, timeout=15)
            data = resp.json()
        except Exception as exc:
            logger.warning("PizzINT Telegram getUpdates failed: %s", exc)
            return None

        if not data.get("ok"):
            logger.warning("PizzINT Telegram error: %s", data.get("description", "unknown"))
            return None

        found_level: int | None = None
        for update in data.get("result", []):
            uid = update.get("update_id", 0)
            if uid > self._last_update_id:
                self._last_update_id = uid

            msg  = update.get("message") or update.get("channel_post") or {}
            text = msg.get("text") or msg.get("caption") or ""
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Filter to configured channel if set (empty = accept any chat)
            if self._channel_id and chat_id != self._channel_id:
                continue

            level = _parse_doughcon(text)
            if level is not None:
                found_level = level  # keep the last (most recent) match

        if self._last_update_id > 0:
            self._save_state()  # persist updated cursor

        return found_level

    # ── Option B: Web scraping ─────────────────────────────────────────────────

    def _fetch_from_web(self) -> int | None:
        """
        Scrape pizzint.watch for current DOUGHCON level.
        Tries multiple regex patterns against the raw HTML.
        """
        try:
            resp = self._session.get(
                PIZZINT_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.warning("pizzint.watch fetch failed: %s", exc)
            return None

        level = _parse_doughcon(html)
        if level is None:
            logger.warning(
                "PizzINT: scraped pizzint.watch but could not extract DOUGHCON level. "
                "Page structure may have changed — review manually."
            )
            logger.debug("PizzINT HTML snippet: %s", html[:500])
        return level

    # ── State persistence ──────────────────────────────────────────────────────

    def _load_state(self) -> None:
        path = Path(config.PIZZINT_STATE_PATH)
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            self._level           = int(state.get("doughcon_level", 5))
            self._updated_at      = int(state.get("updated_at", 0))
            self._source          = state.get("source", "none")
            self._last_update_id  = int(state.get("last_telegram_update_id", 0))
            logger.debug(
                "PizzINT: loaded state — DOUGHCON %d (%s) from %s",
                self._level, self.label, self._source,
            )
        except Exception as exc:
            logger.warning("PizzINT: could not load state file: %s", exc)

    def _save_state(self) -> None:
        path = Path(config.PIZZINT_STATE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "doughcon_level":           self._level,
            "updated_at":               self._updated_at,
            "source":                   self._source,
            "last_telegram_update_id":  self._last_update_id,
        }, indent=2))


# ── Shared parsing helper ──────────────────────────────────────────────────────

def _parse_doughcon(text: str) -> int | None:
    """
    Extract a DOUGHCON level (1–5) from arbitrary text or HTML.
    Tries patterns from most to least specific; returns first match.
    """
    for pattern in _DOUGHCON_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            level = int(m.group(1))
            if 1 <= level <= 5:
                return level
    return None
