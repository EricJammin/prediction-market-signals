"""
Signal C + GDELT News Backtest Validation

For each Signal C surge in our Venezuela and Iran markets, queries the GDELT
DOC API v2 to determine whether public news existed at the time of the surge.

Classification:
  UNEXPLAINED  (0 articles) → potential insider trading signal
  AMBIGUOUS    (1 article)  → possibly news-driven
  NEWS-DRIVEN  (2+ articles) → surge plausibly explained by public news

Key validation question: do UNEXPLAINED surges produce better forward returns
than NEWS-DRIVEN surges? If yes, the news filter earns its place in the monitor.

NOTE: GDELT DOC API covers approximately the last 3 months. Venezuela surges
(Nov-Dec 2025) are at the edge; some may return empty results due to API limit
rather than absence of news. Iran surges (Feb 2026) are safely within range.

Usage:
    cd polymarket_backtest/
    python3 gdelt_backtest.py
"""

from __future__ import annotations

import json
import time
import urllib.parse
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

# Reuse surge detection from the existing analysis script
from signal_c_analysis import (
    MARKETS_TO_ANALYZE,
    SurgeEvent,
    compute_returns,
    detect_surges,
    load_trades,
)

# ── GDELT constants ────────────────────────────────────────────────────────────

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWS_LOOKBACK_SECONDS = 4 * 3600   # 4 hours before surge (matches live monitor)
NEWS_FORWARD_SECONDS  = 3600       # 1 hour after surge start (end of the bucket)
GDELT_MAX_RECORDS = 25             # enough to classify; don't need hundreds
GDELT_REQUEST_DELAY = 8.0          # GDELT enforces 1 req/5s; 8s gives headroom
GDELT_MAX_RETRIES = 4
GDELT_RETRY_BACKOFF = 12.0         # 12s / 24s / 48s / 96s on successive retries

CACHE_PATH = Path("data/gdelt_cache.json")

# Articles needed to classify a surge as "explained by news"
NEWS_DRIVEN_THRESHOLD = 2   # matches NEWS_STRONG_THRESHOLD in monitor config
AMBIGUOUS_THRESHOLD = 1

# ── Market-specific keyword sets ───────────────────────────────────────────────
# Each set is tried in order; first non-empty result wins.
# Use targeted terms — broad terms (e.g. just "Venezuela") return noise.

MARKET_KEYWORDS: dict[str, list[str]] = {
    # Venezuela Invasion by Dec 31, 2025
    # NOTE: Nov-Dec 2025 is likely outside GDELT DOC API's ~3-month window.
    # These queries are included for completeness but may return empty results.
    "0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68": [
        'Venezuela (invasion OR military OR troops OR "armed forces")',
        'Venezuela Maduro (sanctions OR ultimatum OR military)',
        'Venezuela "United States" (military OR troops)',
    ],
    # Iran Strike on Israel by Feb 28, 2026
    "0xb3ebf217cf2f393a66030c072b04b893268506923e01b23f1bcf3504c3d319c2": [
        'Iran Israel (strike OR attack OR missiles OR military)',
        'Iran (nuclear OR strike OR attack) Israel',
        'Iran military (Israel OR "United States")',
    ],
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GDELTArticle:
    title: str
    url: str
    domain: str
    seendate: str  # raw from API: YYYYMMDDThhmmssZ


@dataclass
class NewsResult:
    n_articles: int
    classification: str           # UNEXPLAINED / AMBIGUOUS / NEWS-DRIVEN
    articles: list[GDELTArticle]
    query_window_start: int       # unix ts
    query_window_end: int         # unix ts
    from_cache: bool = False
    api_error: bool = False       # True if request failed (treat as unknown)
    api_error_msg: str = ""


@dataclass
class AnnotatedSurge:
    surge: SurgeEvent
    news: NewsResult


# ── Cache ──────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _cache_key(query: str, window_start: int, window_end: int) -> str:
    return f"{query}|{window_start}|{window_end}"


# ── GDELT client ───────────────────────────────────────────────────────────────

def _ts_to_gdelt(ts: int) -> str:
    """Convert unix timestamp to GDELT datetime format: YYYYMMDDHHmmss"""
    dt = datetime.datetime.utcfromtimestamp(ts)
    return dt.strftime("%Y%m%d%H%M%S")


def _parse_seendate(seendate: str) -> Optional[int]:
    """Parse GDELT seendate (YYYYMMDDThhmmssZ) to unix timestamp."""
    try:
        dt = datetime.datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
        return int(dt.timestamp())
    except ValueError:
        return None


def _fetch_gdelt(
    query: str,
    window_start: int,
    window_end: int,
    session: requests.Session,
) -> Optional[list[dict]]:
    """
    Query GDELT DOC API v2 for articles matching `query` in [window_start, window_end].
    Returns list of article dicts, or None on failure.
    """
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": GDELT_MAX_RECORDS,
        "startdatetime": _ts_to_gdelt(window_start),
        "enddatetime": _ts_to_gdelt(window_end),
        "format": "json",
        "sort": "DateDesc",
    }

    url = GDELT_DOC_API + "?" + urllib.parse.urlencode(params)

    for attempt in range(GDELT_MAX_RETRIES):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                body = resp.text.strip()
                if not body:
                    # Empty body = date range outside API window (3-month limit)
                    return []
                if not body.startswith("{"):
                    # GDELT returns plain-text errors with HTTP 200 (e.g., "phrase too short")
                    print(f"    [GDELT] API message: {body[:80]}")
                    return None
                data = resp.json()
                return data.get("articles") or []
            elif resp.status_code == 429:
                wait = GDELT_RETRY_BACKOFF * (2 ** attempt)
                print(f"    [GDELT] rate limited — waiting {wait:.0f}s")
                time.sleep(wait)
            else:
                print(f"    [GDELT] HTTP {resp.status_code} for query: {query[:60]}")
                return None
        except Exception as exc:
            if attempt < GDELT_MAX_RETRIES - 1:
                time.sleep(GDELT_RETRY_BACKOFF)
            else:
                print(f"    [GDELT] request failed: {exc}")
                return None

    return None


def classify(n: int) -> str:
    if n == 0:
        return "UNEXPLAINED"
    if n < NEWS_DRIVEN_THRESHOLD:
        return "AMBIGUOUS  "
    return "NEWS-DRIVEN"


def check_news(
    condition_id: str,
    surge: SurgeEvent,
    cache: dict,
    session: requests.Session,
) -> NewsResult:
    """
    Query GDELT for news in the 4-hour window before (and 1 hour into) the surge.
    Tries market-specific keyword sets in order; uses first non-empty result.
    """
    window_start = surge.hour_ts - NEWS_LOOKBACK_SECONDS
    window_end   = surge.hour_ts + NEWS_FORWARD_SECONDS

    keyword_sets = MARKET_KEYWORDS.get(condition_id, [])
    if not keyword_sets:
        return NewsResult(
            n_articles=0,
            classification="UNEXPLAINED",
            articles=[],
            query_window_start=window_start,
            query_window_end=window_end,
            api_error=True,
            api_error_msg="No keyword set defined for this market",
        )

    for query in keyword_sets:
        key = _cache_key(query, window_start, window_end)
        if key in cache:
            raw_articles = cache[key]
            from_cache = True
        else:
            time.sleep(GDELT_REQUEST_DELAY)
            raw_articles = _fetch_gdelt(query, window_start, window_end, session)
            if raw_articles is None:
                return NewsResult(
                    n_articles=0,
                    classification=classify(0),
                    articles=[],
                    query_window_start=window_start,
                    query_window_end=window_end,
                    api_error=True,
                    api_error_msg=f"GDELT request failed for query: {query[:60]}",
                )
            cache[key] = raw_articles
            _save_cache(cache)
            from_cache = False

        articles = [
            GDELTArticle(
                title=a.get("title", ""),
                url=a.get("url", ""),
                domain=a.get("domain", ""),
                seendate=a.get("seendate", ""),
            )
            for a in (raw_articles or [])
        ]

        if articles:
            # Got results with this keyword set — use it
            return NewsResult(
                n_articles=len(articles),
                classification=classify(len(articles)),
                articles=articles,
                query_window_start=window_start,
                query_window_end=window_end,
                from_cache=from_cache,
            )

    # All keyword sets returned 0 articles
    return NewsResult(
        n_articles=0,
        classification=classify(0),
        articles=[],
        query_window_start=window_start,
        query_window_end=window_end,
        from_cache=from_cache,
    )


# ── Output formatting ──────────────────────────────────────────────────────────

def print_market_results(market: dict, annotated: list[AnnotatedSurge]) -> None:
    w = 120
    print("=" * w)
    print(f"GDELT VALIDATION: {market['name']}")
    print(f"  Market resolution: {market['resolution']}  |  Real event: {'YES' if market['real_event_happened'] else 'NO'} ({market['real_event_date']})")
    print("=" * w)

    header = (
        f"{'Date/Time (UTC)':<20} {'Ratio':>7} {'YES@surge':>10} "
        f"{'Ret(mkt)':>9} {'Ret(real)':>9}  {'Classification':<14}  Articles / Top Headline"
    )
    print(header)
    print("-" * w)

    for ann in annotated:
        s = ann.surge
        n = ann.news
        dt_str = s.datetime_utc.strftime("%Y-%m-%d %H:%M")
        yes_str = f"{s.yes_price_at_surge:.3f}" if s.yes_price_at_surge else "  N/A"
        mkt_ret = f"{s.return_if_bought_yes:+.0%}" if s.return_if_bought_yes is not None else "  N/A"
        real_ret = f"{s.real_world_return:+.0%}" if s.real_world_return is not None else "  N/A"

        if n.api_error:
            news_str = f"[ERR: {n.api_error_msg[:40]}]"
        elif n.n_articles == 0:
            news_str = "0 articles"
        else:
            top = n.articles[0].title[:55] if n.articles else ""
            news_str = f"{n.n_articles} articles  \"{top}\""

        cache_flag = " (cached)" if n.from_cache else ""
        print(
            f"{dt_str:<20} {s.surge_ratio:>7.1f}x {yes_str:>10} "
            f"{mkt_ret:>9} {real_ret:>9}  {n.classification}  {news_str}{cache_flag}"
        )

    print()

    # ── Summary stats ──────────────────────────────────────────────────────────
    valid = [a for a in annotated if not a.news.api_error]
    if not valid:
        print("  No valid GDELT results.\n")
        return

    unexplained = [a for a in valid if a.news.classification.strip() == "UNEXPLAINED"]
    ambiguous   = [a for a in valid if a.news.classification.strip() == "AMBIGUOUS"]
    news_driven = [a for a in valid if a.news.classification.strip() == "NEWS-DRIVEN"]

    print(f"  Surge classification ({len(valid)} surges with GDELT data):")
    print(f"    UNEXPLAINED  (0 articles): {len(unexplained):>3}  ({len(unexplained)/len(valid):.0%})")
    print(f"    AMBIGUOUS    (1 article):  {len(ambiguous):>3}  ({len(ambiguous)/len(valid):.0%})")
    print(f"    NEWS-DRIVEN  (2+):         {len(news_driven):>3}  ({len(news_driven)/len(valid):.0%})")

    # EV by classification (real-world return, for markets where event happened)
    def avg_real_ret(group: list[AnnotatedSurge]) -> Optional[float]:
        rets = [a.surge.real_world_return for a in group if a.surge.real_world_return is not None]
        return sum(rets) / len(rets) if rets else None

    def avg_mkt_ret(group: list[AnnotatedSurge]) -> Optional[float]:
        rets = [a.surge.return_if_bought_yes for a in group if a.surge.return_if_bought_yes is not None]
        return sum(rets) / len(rets) if rets else None

    print(f"\n  Average return if bought YES at surge (market resolution):")
    for label, group in [("UNEXPLAINED", unexplained), ("AMBIGUOUS", ambiguous), ("NEWS-DRIVEN", news_driven)]:
        r = avg_mkt_ret(group)
        print(f"    {label:<13}: {f'{r:+.1%}' if r is not None else 'N/A':>8}  (n={len(group)})")

    print(f"\n  Average return if bought YES at surge (real-world outcome):")
    for label, group in [("UNEXPLAINED", unexplained), ("AMBIGUOUS", ambiguous), ("NEWS-DRIVEN", news_driven)]:
        r = avg_real_ret(group)
        print(f"    {label:<13}: {f'{r:+.1%}' if r is not None else 'N/A':>8}  (n={len(group)})")

    # Price at surge by classification
    print(f"\n  Average YES price at surge:")
    for label, group in [("UNEXPLAINED", unexplained), ("AMBIGUOUS", ambiguous), ("NEWS-DRIVEN", news_driven)]:
        prices = [a.surge.yes_price_at_surge for a in group if a.surge.yes_price_at_surge]
        avg = sum(prices) / len(prices) if prices else None
        print(f"    {label:<13}: {f'{avg:.3f}' if avg else 'N/A':>8}")

    print()


def print_combined_summary(all_annotated: list[tuple[dict, list[AnnotatedSurge]]]) -> None:
    """Cross-market summary of UNEXPLAINED vs NEWS-DRIVEN forward returns."""
    w = 80
    print("=" * w)
    print("COMBINED SUMMARY — All Markets")
    print("=" * w)

    all_valid: list[AnnotatedSurge] = []
    for _, annotated in all_annotated:
        all_valid.extend(a for a in annotated if not a.news.api_error)

    if not all_valid:
        print("No valid data.\n")
        return

    unexplained = [a for a in all_valid if a.news.classification.strip() == "UNEXPLAINED"]
    news_driven = [a for a in all_valid if a.news.classification.strip() == "NEWS-DRIVEN"]

    print(f"Total surges with GDELT data: {len(all_valid)}")
    print(f"  UNEXPLAINED: {len(unexplained)}  ({len(unexplained)/len(all_valid):.0%})")
    print(f"  NEWS-DRIVEN: {len(news_driven)}  ({len(news_driven)/len(all_valid):.0%})")

    def _avg(group, attr):
        vals = [getattr(a.surge, attr) for a in group if getattr(a.surge, attr) is not None]
        return sum(vals) / len(vals) if vals else None

    print(f"\n  Real-world return (buy YES at surge):")
    print(f"    UNEXPLAINED: {_avg(unexplained, 'real_world_return'):+.1%}" if _avg(unexplained, 'real_world_return') is not None else "    UNEXPLAINED: N/A")
    print(f"    NEWS-DRIVEN: {_avg(news_driven, 'real_world_return'):+.1%}" if _avg(news_driven, 'real_world_return') is not None else "    NEWS-DRIVEN: N/A")

    print(f"\n  Signal C + news filter effectiveness:")
    n_unexplained_profitable = sum(
        1 for a in unexplained
        if a.surge.real_world_return is not None and a.surge.real_world_return > 0
    )
    n_news_profitable = sum(
        1 for a in news_driven
        if a.surge.real_world_return is not None and a.surge.real_world_return > 0
    )
    if unexplained:
        valid_unexp = [a for a in unexplained if a.surge.real_world_return is not None]
        print(f"    UNEXPLAINED profitable: {n_unexplained_profitable}/{len(valid_unexp)}"
              f"  ({n_unexplained_profitable/len(valid_unexp):.0%})" if valid_unexp else "    UNEXPLAINED profitable: N/A")
    if news_driven:
        valid_news = [a for a in news_driven if a.surge.real_world_return is not None]
        print(f"    NEWS-DRIVEN profitable: {n_news_profitable}/{len(valid_news)}"
              f"  ({n_news_profitable/len(valid_news):.0%})" if valid_news else "    NEWS-DRIVEN profitable: N/A")

    print(f"\n  Conclusion:")
    u_ret = _avg(unexplained, 'real_world_return')
    n_ret = _avg(news_driven, 'real_world_return')
    if u_ret is not None and n_ret is not None:
        delta = u_ret - n_ret
        verdict = "VALIDATES news filter" if delta > 0 else "does NOT validate news filter"
        print(f"    UNEXPLAINED surges outperform NEWS-DRIVEN by {delta:+.1%} → {verdict}")
    else:
        print("    Insufficient data for cross-category comparison.")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\nSignal C + GDELT News Backtest Validation")
    print("==========================================")
    print(f"News lookback window: {NEWS_LOOKBACK_SECONDS // 3600}h before surge + 1h into surge")
    print(f"Articles for NEWS-DRIVEN: {NEWS_DRIVEN_THRESHOLD}+  |  AMBIGUOUS: {AMBIGUOUS_THRESHOLD}")
    print(f"Cache: {CACHE_PATH}")
    print()

    cache = _load_cache()
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-signal-research/1.0"

    all_annotated: list[tuple[dict, list[AnnotatedSurge]]] = []

    for market in MARKETS_TO_ANALYZE:
        cid = market["condition_id"]
        print(f"Loading trades: {market['name']}…")
        try:
            df = load_trades(cid)
        except FileNotFoundError:
            print(f"  No trade data — skipping.\n")
            continue

        surges = detect_surges(df)
        for s in surges:
            compute_returns(s, market["resolution"], market["real_event_happened"])

        print(f"  {len(surges)} surges detected. Querying GDELT…")

        annotated: list[AnnotatedSurge] = []
        for i, surge in enumerate(surges):
            dt_str = surge.datetime_utc.strftime("%Y-%m-%d %H:%M")
            print(f"  [{i+1:>3}/{len(surges)}] {dt_str}  ratio={surge.surge_ratio:.1f}x", end="", flush=True)
            news = check_news(cid, surge, cache, session)
            if news.from_cache:
                print(f"  → {news.classification.strip()} ({news.n_articles} articles, cached)")
            elif news.api_error:
                print(f"  → ERROR: {news.api_error_msg[:50]}")
            else:
                print(f"  → {news.classification.strip()} ({news.n_articles} articles)")
            annotated.append(AnnotatedSurge(surge=surge, news=news))

        all_annotated.append((market, annotated))
        print()

    print()
    for market, annotated in all_annotated:
        print_market_results(market, annotated)

    if len(all_annotated) > 1:
        print_combined_summary(all_annotated)


if __name__ == "__main__":
    main()
