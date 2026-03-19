"""
NewsChecker: cross-references a Polymarket volume surge against recent news.

Uses Google News RSS (free, no API key, no rate limit documented).
Filters returned articles to those published within NEWS_LOOKBACK_HOURS.

Scoring:
  0 recent articles  → 1.0  (surge is unexplained by public news — most interesting)
  1 recent article   → 0.5  (ambiguous — one source may be noise)
  2+ recent articles → 0.0  (surge is likely news-driven — less interesting)

The score feeds into the composite alert scorer in alert_aggregator.py.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import feedparser

import config

logger = logging.getLogger(__name__)

# Common English stop words to strip from search queries
_STOP_WORDS = frozenset({
    "will", "the", "a", "an", "of", "in", "on", "at", "to", "by", "for",
    "is", "be", "are", "was", "were", "would", "could", "should", "may",
    "might", "do", "does", "did", "have", "has", "had", "this", "that",
    "with", "from", "or", "and", "not", "no", "it", "its",
})


@dataclass
class NewsResult:
    query: str
    score: float                          # 0.0, 0.5, or 1.0
    matched_articles: list[str] = field(default_factory=list)  # titles within lookback
    total_articles: int = 0               # total RSS results (not time-filtered)
    checked_at: int = 0                   # Unix timestamp


class NewsChecker:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, NewsResult]] = {}  # query → (ts, result)
        self._cache_ttl = 600  # 10 minutes — don't re-query the same term within a poll

    def check(self, market_question: str, extra_keywords: list[str] | None = None) -> NewsResult:
        """
        Build a search query from market_question, fetch Google News RSS,
        filter to articles within NEWS_LOOKBACK_HOURS, and return a NewsResult.

        extra_keywords: additional search terms (e.g. from market_watchlist seed data).
        If provided, they are appended to the query.
        """
        query = self._build_query(market_question, extra_keywords or [])

        # Return cached result if fresh
        cached = self._cache.get(query)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        result = self._fetch(query)
        self._cache[query] = (time.time(), result)
        return result

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch(self, query: str) -> NewsResult:
        url = config.GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query))
        cutoff = time.time() - config.NEWS_LOOKBACK_HOURS * 3600

        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            logger.warning("Google News RSS fetch failed for '%s': %s", query, exc)
            # On fetch failure, treat as ambiguous rather than crashing the alert pipeline
            return NewsResult(
                query=query,
                score=0.5,
                matched_articles=[],
                total_articles=0,
                checked_at=int(time.time()),
            )

        recent_titles: list[str] = []
        for entry in feed.entries:
            pub_ts = self._parse_pubdate(entry)
            if pub_ts is not None and pub_ts >= cutoff:
                title = getattr(entry, "title", "")
                recent_titles.append(title)

        n_recent = len(recent_titles)
        if n_recent == 0:
            score = 1.0
        elif n_recent == 1:
            score = 0.5
        else:
            score = 0.0

        logger.debug(
            "News check '%s': %d recent articles (score=%.1f)",
            query, n_recent, score,
        )
        return NewsResult(
            query=query,
            score=score,
            matched_articles=recent_titles[:5],  # keep top 5 for alert display
            total_articles=len(feed.entries),
            checked_at=int(time.time()),
        )

    @staticmethod
    def _build_query(question: str, extra: list[str]) -> str:
        """
        Extract key terms from question for RSS query.
        Strategy: strip stop words and question punctuation, take first 5 meaningful tokens.
        Append any extra keywords.
        """
        # Strip leading question words and punctuation
        text = re.sub(r"[?\"'()]", "", question)
        tokens = text.split()
        key_tokens = [t for t in tokens if t.lower() not in _STOP_WORDS and len(t) > 2]
        base = " ".join(key_tokens[:5])

        if extra:
            # Use first extra keyword as an OR alternative
            base = f"{base} OR {extra[0]}"

        return base.strip()

    @staticmethod
    def _parse_pubdate(entry) -> float | None:
        """
        Parse feedparser entry's publication time to Unix timestamp.
        feedparser gives published_parsed as time.struct_time in UTC.
        """
        pp = getattr(entry, "published_parsed", None)
        if pp is None:
            return None
        try:
            return float(calendar.timegm(pp))
        except Exception:
            return None
