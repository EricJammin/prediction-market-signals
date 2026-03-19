# Polymarket Signal C: Live Monitoring System Spec

**Status:** Design only. Not yet built.
**Validated against:** Venezuela invasion market (Oct 2025 – Jan 2026)
**Date:** 2026-03-14

---

## Motivation

Signal A (wallet-level burner detection) requires a larger dataset of resolved markets to produce reliable signals. Signal C (market-level volume surge detection) has already demonstrated genuine predictive value:

**Venezuela case study:**
- First 5x+ surge fired **Nov 1, 2025** — 63 days before the Jan 3, 2026 invasion
- YES token was priced at $0.11–0.14 during the Nov 1–7 cluster (still credible, not a lottery ticket)
- 95 total surge events over the following 9 weeks
- The early surge window (Nov 1–18) offered +600% to +1500% real-event returns if YES was bought
- After Dec 10, YES had fallen below $0.10 — the actionable window had closed

The critical insight: **the first cluster of surges on a previously quiet market is the signal.** The goal of live monitoring is to catch that first cluster in real-time, before the price has moved.

---

## System Design

### 1. Data Collection

**Source:** `https://data-api.polymarket.com/trades?market={condition_id}&limit=1000&offset=0`

**Scope:** All active (unresolved) Polymarket markets
**Market discovery:** `https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100`
Paginate through all active markets, filter to those with `volume > $50K` (below this, surges are almost always noise).

**What to store per market (in-memory or SQLite):**
- Hourly volume buckets (rolling 7-day window)
- Latest YES price
- First time this market entered the monitoring window
- Surge history (timestamps + ratios)

### 2. Polling Frequency

**Recommended: every 10 minutes**

Rationale:
- Signal C detects *hourly* volume anomalies; polling faster than 10 min doesn't improve detection
- Polymarket trade data is near-real-time (no meaningful delay)
- At 10 min intervals: ~144 polls/day per market, ~14,400/day for 100 markets
- The data-api is public and rate-limitless at this scale

**Do NOT poll faster than 5 minutes** — no benefit and risks hitting undocumented rate limits.

### 3. Surge Detection Algorithm

Same as Signal C in `signal_detector.py`, applied in rolling fashion:

```
For each active market, every poll:
  1. Fetch latest trades since last poll
  2. Update hourly volume bucket for current hour
  3. Compute rolling 7-day median hourly volume (baseline)
  4. If current_hour_volume > 5× baseline AND baseline > $500:
       → fire alert
```

**Key parameters (same as backtest):**
- `SURGE_MULTIPLIER = 5.0` — 5× the baseline hourly volume
- `SURGE_MIN_BASELINE = $500` — ignore markets with essentially no trading history
- `SURGE_LOOKBACK_HOURS = 168` — 7-day rolling median

**Alert deduplication:** Once a surge alert fires for a given market-hour, don't re-alert for the same hour. Allow a new alert after 6 hours of no surge activity (cooldown).

### 4. Alert Content (Telegram)

Each alert should answer: *Is this worth trading right now?*

```
🚨 VOLUME SURGE: [Market Question]

Surge:    {ratio:.1f}× baseline  (${surge_vol:,.0f} vs ${baseline:,.0f}/hr)
Price:    YES {yes_price:.3f}  |  NO {no_price:.3f}
Implied:  {yes_pct:.0f}% probability YES
Volume:   ${total_volume:,.0f} total market volume

Resolution: {resolution_date}
Category:   {category}

[Polymarket Link]
[First surge in this market: {first_surge_time} ago / {nth} surge this week]
```

**Critical fields for quick assessment:**
- **Ratio** — how anomalous (5x is threshold, 20x+ is significant, 50x+ is remarkable)
- **YES price** — determines whether there's still value. Below $0.05: lottery ticket, skip. Above $0.60: mostly priced in, diminishing edge. Sweet spot: $0.10–$0.50.
- **Surge count** — first surge on a market vs. 30th surge tells you very different things
- **Resolution date** — time remaining affects position sizing

### 5. False Positive Rate Estimate

From the Venezuela backtest (95 surge events on 1 market over 9 weeks):

**Genuine signal markets** (strong pre-event surge pattern): ~95 surges over 9 weeks = ~10/week on the hot market.

**Noise markets**: Markets with thin but spiky liquidity will fire spuriously. Based on the backtest data:
- `SURGE_MIN_BASELINE = $500` eliminates most dormant markets
- With 5× multiplier: expect ~1–3 false surges per week per active market in the $500K–$5M range
- With ~50 monitored markets in the geopolitical/news category: **~50–150 alerts/week total**
- During a genuine pre-event window: expect 10–30 alerts/week on **a single market**

**Signal quality heuristic for the operator:**
- A market with its **first ever 10x+ surge** on a previously quiet baseline → high priority
- A market with its **20th surge in 2 weeks** and YES price below $0.05 → probably past the actionable window
- A market with surges clustering on weekdays during US market hours → likely institutional activity
- A market with random isolated surges → probably noise/arb

### 6. Minimum Infrastructure

```
┌─────────────────────────────────────────────────────┐
│  Cron job (every 10 min)                            │
│  ┌─────────────────────────────────────────────────┐│
│  │  monitor.py                                     ││
│  │  1. Fetch active markets from Gamma API         ││
│  │  2. For each market, fetch recent trades        ││
│  │  3. Update rolling hourly volume buckets        ││
│  │  4. Run surge detection                         ││
│  │  5. Deduplicate against recent alert log        ││
│  │  6. Fire Telegram alerts for new surges         ││
│  └─────────────────────────────────────────────────┘│
│                                                     │
│  State: SQLite (or JSON files)                      │
│  - hourly_volumes.db: {market_id, hour_ts, volume}  │
│  - alerts_log.db: {market_id, hour_ts, ratio, sent} │
│                                                     │
│  Secrets: .env                                      │
│  - TELEGRAM_BOT_TOKEN                               │
│  - TELEGRAM_CHAT_ID                                 │
└─────────────────────────────────────────────────────┘
```

**Deployment options (cheapest to most robust):**
1. **Mac cron job** — simplest, works fine for personal use, dies if laptop sleeps
2. **Raspberry Pi** — always-on, $50 one-time cost
3. **Railway/Render free tier** — cloud, free for low-traffic cron
4. **AWS Lambda + EventBridge** — most reliable, ~$2/month at this polling rate

For a first version: a simple cron job on a Mac or always-on machine is sufficient.

### 7. What to Build (Ordered by Priority)

1. **`monitor.py`** — core poller: fetch active markets, compute hourly volumes, detect surges, write to SQLite
2. **`alerter.py`** — Telegram integration: format and send alerts, track sent alerts to deduplicate
3. **`state.py`** — SQLite wrapper for hourly_volumes and alert_log tables
4. **Cron entry** — `*/10 * * * * cd /path/to/project && python3 monitor.py >> logs/monitor.log 2>&1`
5. **`backfill.py`** — seed historical volume state for already-active markets on first run (using the existing DataCollector)

**Not needed for v1:**
- Dashboard / web UI
- Signal A integration
- Wallet-level analysis at alert time
- Historical alert performance tracking

### 8. Open Questions Before Building

1. **Market scope:** Monitor all active markets (hundreds) or only geopolitical/news categories? Starting narrow (geopolitical, ~50 markets) reduces noise and is easier to tune.

2. **Telegram vs. other channels:** Telegram is lowest friction. Email or Slack also viable. The key requirement is mobile-accessible so you can act quickly.

3. **Position sizing guidance in alert:** Should the alert include a suggested position size based on Kelly criterion given the surge price? Probably yes for v2, not v1.

4. **Surge on YES vs. NO volume:** Currently Signal C is market-level (total volume). A surge driven entirely by NO buying at a low YES price is different from YES buying. Should we break out surge direction in the alert? Probably yes — add YES_volume/NO_volume ratio to alert content.

5. **Multi-market correlation:** If 5 markets all surge simultaneously (e.g., all Iran-related markets on a single news event), that's stronger than any one signal alone. A correlation detector would be a high-value v2 addition.

---

## Key Lessons from Venezuela Backtest

1. **The actionable window is the FIRST surge cluster, not the sustained activity.** By the time a market has 50 surge events, the YES price has usually moved and you've missed the entry.

2. **YES price at surge time is the gate.** Nov 1–7 surges at $0.10–$0.14 were actionable. Dec 20+ surges at $0.01–$0.03 were not (even though the real event happened, the market deadline made them worthless).

3. **Signal C fires on noise too.** The Venezuela market had 95 surge events over 9 weeks. A live system would have alerted every day. Operator judgment is required — the system surfaces candidates, not buy orders.

4. **The Venezuela case is unusual in its 8-week lead time.** More typical lead time for geopolitical events is probably 1–2 weeks. Design the monitoring system assuming you need to act within 24–48 hours of the first alert.

5. **The Iran strike market had $5M volume but our 4000-trade cap missed its pre-event surges.** The monitoring system needs to store state incrementally — it cannot rely on a snapshot. Incremental polling solves this naturally.
