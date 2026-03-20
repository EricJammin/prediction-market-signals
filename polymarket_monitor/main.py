"""
Polymarket Alert Monitor — main polling loop.

Runs continuously, polling every POLL_INTERVAL_SECONDS (default 10 min).
On startup: seeds 7 days of historical trade data for each watched market.
Each poll: incremental trade fetch → surge detection → composite scoring → Telegram alert.

Usage:
    python main.py              # run monitor
    python main.py --once       # single poll then exit (useful for cron)
    python main.py --backfill   # force full 7-day backfill then exit
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import config
from alert_aggregator import AlertAggregator
from market_watchlist import MarketWatchlist
from news_checker import NewsChecker
from pizzint_monitor import PizzINTMonitor
from signal_a import SignalA
from signal_c import SignalC
from state import StateDB
from telegram_alerter import TelegramAlerter

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor")

# ── Graceful shutdown ──────────────────────────────────────────────────────────

_running = True


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _running
    logger.info("SIGTERM received — shutting down after current poll.")
    _running = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ── Core logic ─────────────────────────────────────────────────────────────────


def backfill_market(
    market: dict,
    signal_c: SignalC,
    db: StateDB,
) -> None:
    """
    Seed 7 days of historical trade data for a single market.
    Uses the standard fetch_trades_since with since_ts = now - BACKFILL_HOURS * 3600.
    Skips if the market already has a last_trade_ts recorded (already seeded).
    """
    market_id = market["condition_id"]
    if db.get_last_trade_ts(market_id) > 0:
        logger.debug("Market %s already has trade history — skipping backfill.", market_id[:16])
        return

    since_ts = int(time.time()) - config.BACKFILL_HOURS * 3600
    logger.info(
        "Backfilling %s (%s)…",
        market_id[:16],
        market.get("question", "")[:50],
    )

    trades = SignalC.fetch_trades_since(market_id, since_ts)
    if trades:
        max_ts = signal_c.ingest_trades(
            market_id,
            trades,
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
        )
        db.set_last_trade_ts(market_id, max_ts)
        logger.info(
            "  → ingested %d trades for %s (through %s)",
            len(trades),
            market_id[:16],
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(max_ts)),
        )
    else:
        # Mark as attempted so we don't retry every poll
        db.set_last_trade_ts(market_id, since_ts)
        logger.info("  → no trades found for %s in backfill window", market_id[:16])


def poll_market(
    market: dict,
    signal_c: SignalC,
    signal_a: SignalA,
    aggregator: AlertAggregator,
    alerter: TelegramAlerter,
    db: StateDB,
) -> bool:
    """
    Incremental poll for a single market:
      1. Fetch trades since last_trade_ts
      2. Ingest into hourly_volumes
      3. Run surge detection
      4. If surge detected, evaluate composite score
      5. If alert threshold met, send Telegram message

    Returns True if an alert was fired.
    """
    market_id = market["condition_id"]
    since_ts = db.get_last_trade_ts(market_id)
    if since_ts == 0:
        # Not yet backfilled; skip surge detection but record current time so
        # subsequent polls have an anchor.
        db.set_last_trade_ts(market_id, int(time.time()) - config.POLL_INTERVAL_SECONDS)
        return False

    trades = SignalC.fetch_trades_since(
        market_id,
        since_ts,
        session=signal_c._session,
    )

    if trades:
        yes_token_id = market.get("yes_token_id", "")
        no_token_id  = market.get("no_token_id", "")
        max_ts = signal_c.ingest_trades(
            market_id, trades, yes_token_id=yes_token_id, no_token_id=no_token_id,
        )
        if max_ts > since_ts:
            db.set_last_trade_ts(market_id, max_ts)

        # Signal A: evaluate each new trade for burner wallet patterns
        signal_a_events = signal_a.ingest_trades(
            market_id, trades, yes_token_id=yes_token_id, no_token_id=no_token_id,
        )
        for event in signal_a_events:
            event.question = market.get("question", "")
            event.slug     = market.get("slug", "")
            logger.warning(
                "SIGNAL A FIRED: %s | wallet=%s | %d/5 | $%,.0f YES @ %.3f",
                market.get("question", "")[:60],
                event.wallet[:16],
                event.n_criteria,
                event.cumulative_buy_usdc,
                event.first_buy_price,
            )
            alerter.send_signal_a_alert(event)

    # Detect surge (returns None if no surge or bucket too young)
    surge = signal_c.detect_surge(market_id)
    if surge is None:
        return False

    logger.info(
        "SURGE detected: %s  ratio=%.1fx  vol=$%,.0f  YES=%.3f",
        market_id[:16],
        surge.surge_ratio,
        surge.surge_volume_usdc,
        surge.yes_price or 0.0,
    )

    # Composite scoring → alert or suppress
    alert = aggregator.evaluate(surge, market)
    if alert is None:
        return False

    # Record before sending so cooldown kicks in even if Telegram fails
    db.record_alert(
        market_id=market_id,
        tier=alert.tier,
        composite_score=alert.composite_score,
        surge_ratio=surge.surge_ratio,
    )

    sent = alerter.send_alert(alert)
    if sent:
        logger.info(
            "ALERT sent: [%s] %s | score=%.1f",
            alert.tier,
            market.get("question", "")[:60],
            alert.composite_score,
        )
    return True


def run_poll_cycle(
    watchlist: MarketWatchlist,
    signal_c: SignalC,
    signal_a: SignalA,
    aggregator: AlertAggregator,
    alerter: TelegramAlerter,
    db: StateDB,
    pizzint: PizzINTMonitor | None = None,
) -> int:
    """
    Single pass over all watched markets. Returns count of Signal C alerts fired.
    Signal A alerts are fired inline within poll_market and counted separately.
    Each market is wrapped in try/except so one bad market doesn't abort the cycle.
    """
    # Refresh PizzINT once per cycle (rate-limited internally)
    if pizzint is not None:
        try:
            pizzint.refresh()
        except Exception as exc:
            logger.warning("PizzINT refresh error: %s", exc)

    markets = watchlist.refresh()  # rate-limited internally; no-op unless 1 hr elapsed
    alerts_fired = 0

    for market in markets:
        market_id = market.get("condition_id", "")
        if not market_id:
            continue
        try:
            fired = poll_market(market, signal_c, signal_a, aggregator, alerter, db)
            if fired:
                alerts_fired += 1
        except Exception as exc:
            logger.error("Error polling market %s: %s", market_id[:16], exc, exc_info=True)

        # Courtesy delay between markets to avoid hammering the API
        time.sleep(config.REQUEST_DELAY_SECONDS)

    logger.info("Poll complete: %d markets checked, %d Signal C alerts fired.", len(markets), alerts_fired)
    return alerts_fired


def run_backfill(
    watchlist: MarketWatchlist,
    signal_c: SignalC,
    db: StateDB,
) -> None:
    """Force-refresh watchlist and backfill all markets."""
    markets = watchlist.refresh(force=True)
    logger.info("Starting backfill for %d markets…", len(markets))
    for market in markets:
        if not market.get("condition_id"):
            continue
        try:
            backfill_market(market, signal_c, db)
        except Exception as exc:
            logger.error(
                "Backfill error for %s: %s",
                market.get("condition_id", "")[:16],
                exc,
                exc_info=True,
            )
        time.sleep(config.REQUEST_DELAY_SECONDS)
    logger.info("Backfill complete.")


# ── Watchlist helper ───────────────────────────────────────────────────────────


def _suggest_watchlist_additions() -> None:
    """
    Query the Gamma API for active geopolitical markets and print any that
    are NOT already in SEED_MARKETS. Output is printed for manual review —
    this command never modifies the watchlist automatically.
    """
    import json as _json
    import requests as _req
    from market_watchlist import SEED_MARKETS

    seeded_ids = {m["condition_id"] for m in SEED_MARKETS}

    print("Querying Gamma API for active geopolitical markets…\n")
    found: list[dict] = []
    page_size = 100

    for offset in range(0, 600, page_size):
        try:
            resp = _req.get(
                f"{config.GAMMA_API_BASE}/markets",
                params={"active": "true", "closed": "false",
                        "limit": page_size, "offset": offset},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            resp.raise_for_status()
            page = resp.json()
        except Exception as exc:
            logger.error("Gamma API error at offset %d: %s", offset, exc)
            break

        if not isinstance(page, list) or not page:
            break

        for raw in page:
            cat = (raw.get("category") or "").lower()
            if not any(c in cat for c in config.GAMMA_WATCHLIST_CATEGORIES):
                continue
            vol = float(raw.get("volume") or raw.get("volumeNum") or 0)
            if vol < config.MIN_MARKET_VOLUME_USDC:
                continue
            cid = raw.get("conditionId") or ""
            if not cid or cid in seeded_ids:
                continue
            if raw.get("closed"):
                continue
            found.append({
                "condition_id": cid,
                "slug":         raw.get("slug", ""),
                "question":     raw.get("question") or raw.get("title", ""),
                "category":     raw.get("category", ""),
                "volume":       vol,
                "end_date":     raw.get("endDateIso") or raw.get("endDate", ""),
            })

        if len(page) < page_size:
            break
        time.sleep(config.REQUEST_DELAY_SECONDS)

    if not found:
        print("No new markets found above the volume threshold.")
        return

    found.sort(key=lambda m: m["volume"], reverse=True)
    print(f"Found {len(found)} active markets not in SEED_MARKETS:\n")
    for m in found:
        print(f"  ${m['volume']:>12,.0f}  [{m['category']:<20}]  {m['question'][:70]}")
        print(f"              slug: {m['slug']}")
        print(f"              id:   {m['condition_id']}")
        print(f"              end:  {m['end_date']}")
        print()

    print("To add any of these, edit SEED_MARKETS in market_watchlist.py.")


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket alert monitor")
    parser.add_argument("--once", action="store_true", help="Single poll then exit")
    parser.add_argument("--backfill", action="store_true", help="Force backfill then exit")
    parser.add_argument(
        "--update-watchlist",
        action="store_true",
        help="Query Gamma API for active geopolitical markets and print suggestions (does not modify watchlist)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    # Load .env from the monitor directory (or its parent)
    load_dotenv(Path(__file__).parent / ".env")
    load_dotenv(Path(__file__).parent.parent / ".env")

    # Ensure data directory exists
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    if args.update_watchlist:
        _suggest_watchlist_additions()
        return

    with StateDB(config.DB_PATH) as db:
        news_checker = NewsChecker()
        signal_c  = SignalC(db)
        signal_a  = SignalA(db)
        watchlist = MarketWatchlist(db)
        aggregator = AlertAggregator(db, news_checker)
        alerter = TelegramAlerter()
        pizzint = PizzINTMonitor()

        if args.backfill:
            run_backfill(watchlist, signal_c, db)
            return

        # Startup: seed history for any markets not yet in DB
        logger.info("Starting Polymarket monitor (poll interval: %ds)…", config.POLL_INTERVAL_SECONDS)
        alerter.send_text("🟢 Polymarket monitor started.")

        markets = watchlist.refresh(force=True)
        logger.info("Watchlist loaded: %d markets", len(markets))

        for market in markets:
            if not market.get("condition_id"):
                continue
            try:
                backfill_market(market, signal_c, db)
            except Exception as exc:
                logger.error(
                    "Startup backfill error for %s: %s",
                    market.get("condition_id", "")[:16],
                    exc,
                )
            time.sleep(config.REQUEST_DELAY_SECONDS)

        logger.info("Startup backfill complete. Entering poll loop…")

        if args.once:
            run_poll_cycle(watchlist, signal_c, signal_a, aggregator, alerter, db, pizzint)
            return

        # Continuous loop
        while _running:
            poll_start = time.time()
            try:
                run_poll_cycle(watchlist, signal_c, signal_a, aggregator, alerter, db, pizzint)
                # Prune stale volume data (keep last 7 days)
                db.prune_old_volumes(
                    cutoff_ts=int(time.time()) - config.SURGE_LOOKBACK_HOURS * 3600
                )
            except Exception as exc:
                logger.error("Unexpected poll cycle error: %s", exc, exc_info=True)

            elapsed = time.time() - poll_start
            sleep_for = max(0, config.POLL_INTERVAL_SECONDS - elapsed)
            logger.debug("Poll took %.1fs; sleeping %.1fs", elapsed, sleep_for)

            # Sleep in short increments so SIGTERM wakes us promptly
            deadline = time.time() + sleep_for
            while _running and time.time() < deadline:
                time.sleep(min(5, deadline - time.time()))

    logger.info("Monitor stopped.")


if __name__ == "__main__":
    main()
