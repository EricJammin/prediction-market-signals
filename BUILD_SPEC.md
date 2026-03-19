# Phase 1 Deployment: Live Polymarket Alert Monitor

## Overview

Deploy a live monitoring system combining Signal A (burner wallet detection), Signal C (volume surge + news filter), and automated PizzINT monitoring. Before going fully live, run a retrospective case study on Venezuela and Iran to validate the composite system would have produced useful alerts.

**Build order is at the bottom of this document (Steps 1-10). Follow sequentially, pausing after each major step for review.**

---

## Part 1: Retrospective Case Study

> Build this first, before going live.

Walk through the Venezuela (Jan 2-3, 2026) and Iran strikes (Feb 2026) timelines hour by hour and reconstruct what each signal layer would have shown. This gives us a concrete picture of the live experience.

### Venezuela Timeline Reconstruction (Jan 1-3, 2026)

Using our complete trade data for the Maduro removal market, produce an hour-by-hour timeline from January 1 00:00 UTC through January 3 18:00 UTC showing:

- **Signal C:** Hourly volume relative to the 7-day baseline. Flag any hours that exceed 3x.
- **News filter:** For each Signal C surge, query GDELT for articles in the 4-hour window. Label as EXPLAINED or UNEXPLAINED. Pay special attention to the late January 2 / early January 3 window before Trump's Truth Social announcement.
- **Signal A:** Did nothingeverhappens911 or any other burner wallets place trades during this window? At what time and price?
- **Market price:** YES price at each hour.
- **PizzINT (manual reconstruction):** PizzINT's dashboard showed elevated activity the night of January 2-3. We can't query their historical data, but document that this was reported. Include a note: "PizzINT reportedly showed elevated DOUGHCON on the night of Jan 2-3 based on public reporting."
- **OSINT context:** Search for any Twitter/news reports of Caribbean military movement or unusual activity in the December 28 - January 2 window. This is manual research — just note what was publicly observable.

Produce a table like:

```
Time (UTC) | YES Price | Volume (vs baseline) | News? | Signal A? | PizzINT? | Composite
```

**The goal:** Show at what point in the timeline a Tier 2 or Tier 3 alert would have fired, what the YES price was at that moment, and what the return would have been (market resolved YES at $1.00).

### Iran Timeline Reconstruction (Feb 2026)

Same format for the Iran strike markets. Focus on:

- When nothingeverhappens911 placed its 5/5 Signal A bet relative to the news breaking
- Whether Signal C detected volume surges before the event
- Whether those surges were news-driven or unexplained

### Output

A markdown document (`CASE_STUDY.md`) with both timelines, a summary of when composite alerts would have fired, the entry prices available at each alert tier, and the hypothetical returns.

---

## Part 2: Live Monitor Deployment

After the case study validates (or doesn't) the composite approach, deploy the live monitor.

### Project Structure

```
polymarket_monitor/
  config.py                # Thresholds, API keys, market watchlist, Telegram config
  main.py                  # Main loop — runs every 10 minutes
  market_watchlist.py      # Active markets to monitor with topic keywords
  signals/
    signal_a.py            # Burner wallet detection (port from backtest)
    signal_c.py            # Volume surge detection (port from backtest)
    news_checker.py        # GDELT or Google News RSS cross-reference
    pizzint_monitor.py     # PizzINT Telegram channel listener
  alerting/
    composite_scorer.py    # Combines all signals into alert tiers
    telegram_alerter.py    # Sends alerts (reuse pattern from stock scanner)
    email_alerter.py       # Daily digest (reuse pattern from stock scanner)
  data/
    market_baselines.json  # Rolling volume baselines per market
    wallet_cache.json      # Cached wallet ages from Polygonscan
    alert_history.json     # Log of all alerts sent (for review and tuning)
  requirements.txt
  .env.example
  README.md
```

---

## Signal A: Burner Wallet Detection (Live)

Every 10 minutes, pull recent trades from the CLOB API for all watched markets. For any trade above $5,000:

- Check wallet age via Polygonscan (cache results, only look up new wallets)
- Check wallet concentration across watched markets
- Check if wallet is a wash trader (round-trip + net-position filter)
- Score against 4/5 criteria:
  1. Freshness: wallet created on-chain <14 days ago
  2. Size: >$15K cumulative position on a single outcome
  3. Concentration: >70% of all wallet activity in one market or related cluster
  4. Entry price: between 0.10 and 0.50 (informational zone)
  5. Not flagged as wash trader

If 4+ criteria met, fire Signal A.

**Important:** Lower the single-trade alert threshold to $5,000 for the live monitor (backtest used $10K). The Venezuela insider split $32K across multiple orders that might have been individually smaller. We want to catch accumulation patterns, not just single large trades. The cumulative threshold stays at $15K.

---

## Signal C: Volume Surge + News Filter (Live)

Maintain a rolling 7-day hourly volume baseline for each watched market (store in `market_baselines.json`, update every cycle). On each cycle:

1. Calculate current-hour volume
2. If volume exceeds 3x the rolling hourly average, flag as a surge
3. Query GDELT (or Google News RSS) for articles matching the market's keywords in the last 4 hours
4. Classify:
   - 0 articles = **UNEXPLAINED** (news_score = 1.0)
   - 1 article = **AMBIGUOUS** (news_score = 0.5)
   - 2+ articles = **NEWS-DRIVEN** (news_score = 0.0)

For new markets that don't have 7 days of baseline yet, use 3 days minimum or skip Signal C until baseline is established.

---

## PizzINT Telegram Monitor (Live)

Use the Telegram Bot API to monitor the PizzINT Telegram channel (`t.me/pizzintwatchers`).

### Option A (recommended)

Use the `python-telegram-bot` library. Add your bot to the PizzINT channel as a member (if the channel allows it). Listen for messages containing DOUGHCON level updates. Parse the DOUGHCON level from message text (they typically post "DOUGHCON level is X" with descriptions).

### Option B (fallback if channel doesn't allow bots)

Poll the `pizzint.watch` website every 10 minutes and scrape the current DOUGHCON level from the page. Use `requests` + `BeautifulSoup`. Less reliable but doesn't require channel access.

### DOUGHCON Score Mapping

| DOUGHCON Level | Description | Score |
|---|---|---|
| 5 | Peacetime | 0.0 |
| 4 | Normal | 0.0 |
| 3 | Elevated | 0.3 |
| 2 | High | 0.7 |
| 1 | Imminent | 1.0 |

PizzINT score only applies to markets tagged as US military action in the watchlist. For non-military markets (elections, policy, regulatory), PizzINT score is always 0.0 and doesn't factor into the composite.

---

## Composite Scoring and Alert Tiers

On each 10-minute cycle, for each watched market, compute:

```python
composite = signal_a_score + signal_c_score + (signal_c_score * news_unexplained_score) + pizzint_score
```

The `signal_c_score * news_unexplained_score` term means an explained surge adds zero while an unexplained surge gets double-counted (both the surge itself and the lack of explanation are informative).

### Alert Tiers

**TIER 3 — ACT** (Telegram instant alert with urgency formatting):
- Signal A fires (4+ criteria) AND Signal C shows unexplained surge
- OR Signal A fires AND PizzINT at DOUGHCON 2+
- OR Signal C unexplained surge AND PizzINT at DOUGHCON 1-2
- Any combination of 3+ signals active on the same market

**TIER 2 — PREPARE** (Telegram alert, standard formatting):
- Signal A fires alone (4+ criteria on any watched market)
- OR Signal C unexplained surge alone (volume 5x+ baseline with zero news articles)
- OR PizzINT at DOUGHCON 1-2 AND any Signal C surge (even if news-driven)

**TIER 1 — WATCH** (included in daily digest email only, no Telegram):
- Signal C surge that is news-driven (volume elevated but articles explain it)
- OR PizzINT at DOUGHCON 3
- OR Signal A fires with only 3/5 criteria

### Telegram Alert Format

```
🔴 TIER 3 — ACT: [Market Name]

Signal A: ✅ FIRED
  Wallet: 0x7a3...f91 (age: 2 days)
  Position: $23,400 YES at avg $0.18
  Concentration: 94% in this market
  Criteria: 5/5

Signal C: ✅ UNEXPLAINED SURGE
  Volume: 8.2x baseline (last 2 hours)
  News check: 0 articles found

PizzINT: ⚠️ DOUGHCON 2

Price: YES $0.22 (was $0.08 24h ago)
Market: [link to Polymarket page]

Action: Multiple independent signals firing. Evaluate immediately.
```

---

## Market Watchlist Management

Create `market_watchlist.py` with a curated list of active geopolitical/policy markets to monitor. Each entry includes:

- Market condition ID (from Gamma API)
- Display name
- Category tag: `military`, `policy`, `election`, `regulatory`
- News keywords for GDELT/RSS matching
- Whether PizzINT is relevant (`True` for US military markets only)

Include a command `python main.py --update-watchlist` that queries the Gamma API for currently active markets in relevant categories and suggests additions. The actual watchlist should be manually curated — auto-adding every market creates noise and API load.

**Start with 15-20 active geopolitical markets.** Include any markets related to: US military action, Iran/Israel, Russia/Ukraine, China/Taiwan, and major US policy decisions.

---

## Daily Digest Email

At a configurable time each day (default 6:00 PM ET), send an email summary including:

- All Tier 1/2/3 alerts from the past 24 hours
- Current PizzINT DOUGHCON level
- Any new markets added to the watchlist
- System health: last successful API poll, any errors
- A table of all watched markets with current YES price and 24h price change

---

## Alert History and Logging

Log every alert to `alert_history.json` with full details: timestamp, market, tier, all signal scores, market price at alert time. This serves two purposes:

1. **Post-hoc analysis** of alert quality (were Tier 3 alerts actually predictive?)
2. **Preventing duplicate alerts** — don't re-alert on the same Signal A wallet within 24 hours, don't re-alert on the same Signal C surge within 4 hours

---

## Configuration (.env)

```
TELEGRAM_BOT_TOKEN=         # From stock scanner setup (reuse same bot or create new one)
TELEGRAM_CHAT_ID=           # Your personal chat ID
POLYGONSCAN_API_KEY=        # From backtest setup
ALERT_EMAIL_FROM=           # Gmail address
ALERT_EMAIL_PASSWORD=       # Gmail app password
ALERT_EMAIL_TO=             # Recipient
GDELT_REQUEST_DELAY=8.0     # Rate limit for GDELT
POLL_INTERVAL_SECONDS=600   # 10 minutes
DIGEST_HOUR_UTC=23          # 6 PM ET = 23 UTC
```

---

## Deployment

Start by running locally: `python main.py`.

**Flags:**
- `--dry-run` — Run one cycle, print what would be alerted, don't send Telegram/email
- `--test-alerts` — Send test Telegram + email with dummy data to verify connections
- `--backfill` — Process last 24h of cached trade data as if monitoring live

Once stable after a few days of local testing, plan for deployment to an always-on server (EC2 t3.micro free tier or DigitalOcean $4-6/month). Start local first.

---

## Testing

- `python main.py --dry-run` — Run one cycle, print what would be alerted
- `python main.py --test-alerts` — Send test Telegram + email with dummy data
- `python main.py --backfill` — Process last 24h of data as if monitoring live
- Unit tests for each signal module with mock data

---

## What NOT to Build Yet

- **Signal B (repeat predictor)** — Needs more resolved markets over time
- **Twitter/OSINT monitoring** — Phase 3
- **Flight tracking integration** — Phase 3
- **Open-source insider tracker integration** — Phase 4
- **Any automated trading** — All alerts are for manual evaluation only

---

## Build Order

Follow these steps sequentially. Pause after each major step for review.

1. **Retrospective case study** (Part 1) — Validate the composite approach on known events. Output: `CASE_STUDY.md`
2. **Market watchlist** with 15-20 active markets
3. **Signal A live implementation** — Port from backtest, lower trade threshold to $5K
4. **Signal C live implementation** — Port from backtest, add rolling baseline
5. **PizzINT monitor** — Try Telegram listener first, fall back to web scraping
6. **News checker** — GDELT with Google News RSS fallback
7. **Composite scorer and alert tier logic**
8. **Telegram and email alerting**
9. **Main loop and deployment**
10. **Testing** with `--dry-run` and `--backfill`
