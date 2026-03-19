"""
Entry point for the Polymarket Whale Detection Backtest (Round 2).

Usage:
  python main.py                     # full run, all markets
  python main.py --dry-run           # 3 markets only, for quick iteration
  python main.py --skip-fetch        # use cached data, skip API calls
  python main.py --market <slug>     # single market by slug or condition_id
  python main.py --search "fed rate" # search Gamma API and exit
  python main.py --list-markets      # print all configured markets and exit

Pipeline:
  1. DataCollector    — fetch/cache market metadata + trade history
  2. WalletProfiler   — build cross-market wallet activity profiles
  3. SignalDetector   — Signal A (burner account) + Signal C (volume surge)
  4. Backtester       — simulate entry at 5min / 30min / 2hr delays
  5. ReportGenerator  — print summary and save data/report.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config
from data_collector import DataCollector
from markets import MARKETS, DRY_RUN_COUNT
from wallet_profiler import WalletProfiler
from polygonscan_client import PolygonscanClient
from signal_detector import SignalDetector
from backtester import Backtester
from report import ReportGenerator


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Whale Detection Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=f"Process only the first {DRY_RUN_COUNT} markets (quick iteration)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip API calls; use only locally cached JSON files",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-fetch all data even if cached files exist",
    )
    parser.add_argument(
        "--market",
        type=str,
        default=None,
        help="Process a single market (slug or condition_id)",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        help="Search the Gamma API for markets matching a keyword and exit",
    )
    parser.add_argument(
        "--list-markets",
        action="store_true",
        help="Print all configured markets and exit",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    return parser.parse_args()


def _select_markets(args: argparse.Namespace) -> list[dict]:
    markets = list(MARKETS)

    if args.market:
        matched = [
            m for m in markets
            if m.get("slug") == args.market or m.get("condition_id") == args.market
        ]
        if not matched:
            print(f"ERROR: No market found matching '{args.market}'", file=sys.stderr)
            print("Use --list-markets to see configured markets.", file=sys.stderr)
            sys.exit(1)
        return matched

    if args.dry_run:
        markets = markets[:DRY_RUN_COUNT]
        logging.getLogger(__name__).info(
            "Dry-run mode: processing %d markets", len(markets)
        )

    return markets


def main() -> None:
    args = _parse_args()
    _setup_logging(args.verbose)
    log = logging.getLogger(__name__)

    # ── Utility modes (exit early) ─────────────────────────────────────────

    if args.list_markets:
        print(f"{'#':<4} {'Slug':<65} {'Category':<20} {'Validation'}")
        print("-" * 100)
        for i, m in enumerate(MARKETS, 1):
            print(
                f"{i:<4} {m.get('slug',''):<65} "
                f"{m.get('category',''):<20} "
                f"{'YES' if m.get('validation_market') else ''}"
            )
        sys.exit(0)

    if args.search:
        collector = DataCollector()
        results = collector.search_markets(args.search)
        if not results:
            print(f"No markets found for query: '{args.search}'")
        else:
            print(f"Found {len(results)} markets:")
            for m in results:
                res = "RESOLVED" if m.get("resolved") else "open"
                print(f"  {m['condition_id']}  {m['slug'][:60]}  [{res}]")
        sys.exit(0)

    # ── Main pipeline ──────────────────────────────────────────────────────

    markets = _select_markets(args)
    log.info("Starting backtest on %d markets", len(markets))

    # 1. Fetch / cache data
    collector = DataCollector(force_refresh=args.force_refresh)

    if not args.skip_fetch:
        log.info("Phase 1: Fetching market data and trade history...")
        for i, market in enumerate(markets, 1):
            log.info("[%d/%d] %s", i, len(markets), market.get("slug") or market.get("condition_id"))
            markets[i - 1] = collector.fetch_market(market)
    else:
        log.info("Phase 1: Skipping fetch (--skip-fetch)")

    # Filter out markets that couldn't be resolved
    resolved_markets = [m for m in markets if m.get("condition_id")]
    skipped = len(markets) - len(resolved_markets)
    if skipped:
        log.warning("%d market(s) could not be resolved and will be skipped", skipped)
    if not resolved_markets:
        log.error("No markets with valid condition_ids — nothing to analyse. "
                  "Verify slugs in markets.py or run --search to find correct slugs.")
        sys.exit(1)

    # 2. Load normalised data
    log.info("Phase 2: Loading and normalising trade data...")
    all_trades, market_metadata = collector.load_all_data(resolved_markets)

    if all_trades.empty:
        log.error("No trade data loaded. Check cached files in %s/", config.RAW_TRADES_DIR)
        sys.exit(1)

    log.info(
        "Loaded %d trades across %d markets (%d unique wallets)",
        len(all_trades),
        all_trades["market_id"].nunique(),
        all_trades["wallet"].nunique(),
    )

    # 3. Profile wallets
    log.info("Phase 3: Building wallet profiles...")
    profiler = WalletProfiler(all_trades)
    wallet_profiles = profiler.build_profiles()

    # 4. Detect signals
    log.info("Phase 4: Detecting signals...")
    poly = PolygonscanClient()
    detector = SignalDetector(
        all_trades=all_trades,
        wallet_profiles=wallet_profiles,
        market_metadata=market_metadata,
        polygonscan=poly,
    )
    signals_a = detector.detect_signal_a()
    signals_c = detector.detect_signal_c()

    if not signals_a:
        log.warning("No Signal A detections. Try lowering thresholds in config.py.")
        sys.exit(0)

    # 5. Backtest (Signal A only — wallet-level signals with resolvable EV)
    log.info("Phase 5: Running backtest simulation...")
    backtester = Backtester(signals_a, all_trades, market_metadata)
    results = backtester.run()

    # 6. Report
    log.info("Phase 6: Generating report...")
    reporter = ReportGenerator(
        results=results,
        signals_a=signals_a,
        signals_c=signals_c,
        market_metadata=market_metadata,
        backtester=backtester,
    )
    reporter.generate()


if __name__ == "__main__":
    main()
