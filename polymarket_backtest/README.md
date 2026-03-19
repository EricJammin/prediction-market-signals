# Polymarket Whale Detection Backtest

Determines whether detecting large, concentrated bets by fresh wallets on
Polymarket prediction markets would have been profitable if you followed those
signals — and specifically, by the time the whale pattern is detectable, has
the price already moved too much for the trade to have positive expected value?

## Quick Start

```bash
cd polymarket_backtest
pip install -r requirements.txt

# Dry run: 3 markets, uses cached data if available
python main.py --dry-run

# Full run
python main.py

# Skip re-fetching if you already have cached data
python main.py --skip-fetch

# Single market
python main.py --market will-donald-trump-win-the-2024-us-presidential-election

# Find market slugs interactively
python main.py --search "fed rate cut"

# List all configured markets
python main.py --list-markets
```

## Project Structure

```
polymarket_backtest/
  config.py           # API endpoints, thresholds, detection delays
  markets.py          # Target market slugs / condition_ids to analyze
  data_collector.py   # Fetch + cache market metadata and trade history
  wallet_profiler.py  # Cross-market wallet activity profiles
  whale_detector.py   # Composite scoring to flag whale signals
  backtester.py       # Simulate following signals at 5min / 30min / 2hr
  report.py           # Summary statistics and console + JSON report
  main.py             # Entry point (argparse)
  data/               # Cached JSON + generated report
```

## Whale Detection Criteria (score 3+ of 4 to fire a signal)

| # | Criterion | Threshold |
|---|-----------|-----------|
| 1 | **Freshness** | Wallet's first Polymarket trade was within 96h of this bet |
| 2 | **Size** | Single trade > $10K / $25K / $50K _or_ cumulative position > $25K |
| 3 | **Concentration** | > 80% of wallet's total Polymarket volume in this one market |
| 4 | **Price insensitivity** | Kept buying same side as price rose > 10 cents |

Freshness and concentration are the strongest predictors; a signal fires on
(freshness + concentration + any 1 other) even for smaller trade sizes.

## Detection Delays Tested

- **5 min** — automated near-real-time monitoring
- **30 min** — semi-automated check
- **2 hr** — manual review cycle

The critical finding this backtest aims to answer: how much does expected value
degrade as detection delay increases?

## Key Output Metrics

- Hit rate — what % of whale signals correctly predicted resolution
- Avg entry price — how much of the move had already happened
- EV per dollar risked — the central profitability metric
- Entry price vs 24h-prior baseline — did the signal beat just buying late?

Report is printed to stdout and saved to `data/report.json`.

## API Notes

- **Gamma API** (`gamma-api.polymarket.com`): market metadata, resolution
- **CLOB API** (`clob.polymarket.com`): individual trade fills, paginated
- Rate-limited to 0.5s between requests; all raw data is cached in `data/`
- If the CLOB API doesn't have full historical data for older markets, this
  will be visible as a low trade count warning — on-chain data via Polygon
  would be needed for complete history (noted as a future enhancement)

## Adding Markets

1. Find the market on polymarket.com — the slug is the last URL path segment
2. Add an entry to `markets.py` (set `condition_id=None` to auto-resolve)
3. Run `python main.py --search "keyword"` to verify slug existence

## Limitations

- Entry price approximation: we use the last trade price before entry time as
  a proxy for the ask price. The true fill price would be slightly higher.
- No blockchain data: relies on CLOB API history only. Historical coverage
  may be incomplete for markets that closed > 6 months ago.
- No live monitoring: this is a historical backtest only.
