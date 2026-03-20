# Polymarket Live Monitor

Polls Polymarket for unusual volume surges on geopolitical prediction markets,
cross-references against news and PizzINT military readiness signals, and fires
Telegram + email alerts when the composite score crosses a tier threshold.

## Quick Start

```bash
cd polymarket_monitor
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
python3 main.py
```

## Commands

```bash
# Start the live monitor (polls every 10 minutes)
python3 main.py

# Dry run — one poll cycle, logs what would fire without sending alerts
python3 main.py --dry-run

# Send a test message to verify Telegram and email are configured
python3 main.py --test-alerts

# Single poll then exit
python3 main.py --once

# Force a backfill of historical surge data then exit
python3 main.py --backfill

# Query Gamma API for new geopolitical markets to add to the watchlist
python3 main.py --update-watchlist

# Verbose logging (useful for debugging PizzINT, news, and signal details)
python3 main.py --log-level DEBUG
```

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Chat ID to send alerts to |
| `ALERT_EMAIL_FROM` | No | Gmail address for digest emails |
| `ALERT_EMAIL_PASSWORD` | No | Gmail App Password (not your account password) |
| `ALERT_EMAIL_TO` | No | Recipient address for digest emails |
| `PIZZINT_CHANNEL_ID` | No | Telegram channel ID for DOUGHCON updates (falls back to API) |

## Project Structure

```
polymarket_monitor/
  main.py               # Entry point and poll loop
  config.py             # Thresholds, intervals, paths
  market_watchlist.py   # Watched markets with keywords and pizzint_relevant flags
  signal_c.py           # Volume surge detection (Signal C)
  signal_a.py           # Burner account detection (Signal A)
  alert_aggregator.py   # Composite scoring and tier classification
  news_checker.py       # GDELT news cross-reference
  pizzint_monitor.py    # PizzINT DOUGHCON level (API + Telegram + web fallbacks)
  telegram_alerter.py   # Telegram alert sender
  email_alerter.py      # Daily digest email sender
  state.py              # SQLite state DB (dedup, surge history)
  data/                 # Runtime state (gitignored)
```

## Alert Tiers

| Tier | Condition |
|---|---|
| **HIGH** | Composite ≥ 2.5, or surge + unexplained news |
| **MEDIUM** | Composite ≥ 1.5 |
| **LOW** | Composite ≥ 0.5 (bare surge, no corroboration) |

Composite score = `signal_c` + `news` + `pizzint` + `insider` (max 4.0).
PizzINT score only contributes for markets tagged `pizzint_relevant=True`
(US military action markets).

## PizzINT Integration

The monitor fetches DOUGHCON military readiness level from
[pizzint.watch](https://pizzint.watch) using three fallback methods:

1. **JSON API** (`/api/dashboard-data`) — primary, no config required
2. **Telegram Bot API** — if `PIZZINT_CHANNEL_ID` is set
3. **Web scrape** — final fallback

DOUGHCON score mapping:

| Level | Label | Score |
|---|---|---|
| 1 | IMMINENT | 1.0 |
| 2 | HIGH | 0.7 |
| 3 | ELEVATED | 0.3 |
| 4 | NORMAL | 0.0 |
| 5 | PEACETIME | 0.0 |

## Adding Markets

Edit `market_watchlist.py` and add an entry with:
- `condition_id` — from the Polymarket URL or Gamma API
- `question` — market question text
- `keywords` — list of news search terms
- `pizzint_relevant` — `True` for US military action markets, `False` otherwise

Run `python3 main.py --update-watchlist` to see auto-suggested additions from
the Gamma API.
