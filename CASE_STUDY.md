# Retrospective Case Study: Would the Live Monitor Have Fired?

Reconstructs what the composite alert system would have shown in real time for two
documented insider trading events. Uses actual trade data, wallet profiles, and GDELT
news lookups. Produced as Step 1 of BUILD_SPEC.md before live deployment.

---

## Executive Summary

| Event | Signal A | Signal C | PizzINT | Composite Alert | Entry Price | Return |
|-------|----------|----------|---------|-----------------|-------------|--------|
| Venezuela (Jan 3 2026) | ✗ Not fired | ✗ Cold-start | Unknown | **No alert** | — | — |
| Iran Khamenei (Feb 16 2026) | ✗ Not fired | No data | Unknown | **No alert** | — | — |
| Iran US strikes (Feb 28 2026) | **✓ FIRED 00:55 UTC** | No baseline | Unknown | **TIER 2 alert** | $0.19 | **+426%** |

The system would have caught the Feb 28 Iran strikes case via Signal A alone, ~5 hours
before the announcement. The other two events were structurally undetectable with the
current approach.

---

## Case 1: Venezuela — Maduro Capture (Jan 2–3, 2026)

### What happened

Trump announced Maduro was in US custody via Truth Social on the night of January 2, 2026
(approximately 22:00–23:00 ET = Jan 3 03:00–04:00 UTC). A known insider turned $32,537
into $436,000, placing bets on January 2 (~4h before the announcement). Their wallet
address is partially known (`0x31a56e...`) but not confirmed.

### What the system would have seen

**Pre-announcement (Jan 2 00:00 – Jan 3 03:00 UTC):** No data accessible. The public
`data-api.polymarket.com` returns only the 4000 most recent trades for a market, newest
first. By the time this market reached $11M in volume, all pre-announcement trades had
scrolled out of the accessible window.

**Signal A (Jan 2):** The insider wallet is unknown. No `pseudonym` or `name` field
appeared in the trade data that could link to this account. Signal A cannot fire without
a wallet to evaluate.

**Signal C (Jan 3 07:58 UTC onward — earliest data available):**

| Hour (UTC) | Volume | vs Baseline | YES Price | News? | Signal C |
|------------|--------|-------------|-----------|-------|----------|
| Jan 3 07:00 | $64 | — | $0.35 | — | Cold-start |
| Jan 3 08:00 | $32,193 | Cold-start | $0.18 | Yes (announcement) | ✗ No baseline |
| Jan 3 09:00 | $367,609 | — | $0.83 | Yes | ✗ No baseline |
| Jan 3 10:00 | $305,132 | — | $0.99 | Yes | ✗ No baseline |

**Cold-start problem:** The market had near-zero trading activity before the announcement.
The rolling 7-day median hourly volume was far below the `SURGE_MIN_BASELINE = $500`
threshold, so Signal C would not fire regardless. Even with data present, there was no
established baseline to measure a surge against.

**PizzINT:** Public reporting indicates PizzINT's DOUGHCON dashboard showed elevated
activity on the night of January 2–3. This was not automatically captured. Manual
monitoring of PizzINT on the night of Jan 2 would have provided a weak signal.

### Would an alert have fired?

**No.** Three structural failures stacked:
1. Pre-announcement trades not accessible (4000-trade cap, high post-event volume)
2. Signal A cannot fire on an unknown wallet
3. Signal C fails on a cold-start market with no established baseline

**Lesson:** The system as designed cannot catch first-time events on new/low-volume
markets. Venezuela-style events require either: (a) real-time on-chain monitoring of all
new wallet activity, or (b) Signal C cold-start handling (alert on first large volume
regardless of baseline). Both are Phase 3 enhancements.

---

## Case 2: Iran — Khamenei Death (Feb 16, 2026)

### What happened

Khamenei's death was announced approximately 15:33 UTC on February 16, 2026. Magamyman
placed two large bets ~71–100 minutes before the announcement:

| Time (UTC) | Market | Position | Price | Size |
|------------|--------|----------|-------|------|
| Feb 16 13:54 | US strikes Iran by Feb 28 | YES | $0.144 | $114,393 |
| Feb 16 14:22 | Khamenei out as Supreme Leader | YES | $0.140 | $143,393 |

Both bets on two correlated markets within 28 minutes, totaling ~$258K at prices that
implied <15% probability.

### What Signal A would have shown

| Criterion | Result | Value |
|-----------|--------|-------|
| wallet_age | ✗ FAIL | 471–498 days old |
| size | ✓ PASS | $114K–$143K |
| concentration | ✗ FAIL | 3–46% (holds 334 trades across dozens of markets) |
| price_insensitivity | ✓ PASS | Single large trade at 0.140 |
| net_buyer | ✓ PASS | $143K buys, $0 sells |
| **Signal A** | **✗ DID NOT FIRE** | **3/5 criteria** |

**Why Signal A missed:** Magamyman is a repeat predictor (Signal B territory), not a
burner account. Their wallet is 471–498 days old with 334 trades across Israel, Iran,
Fed rates, and other geopolitical markets. Even a $143K concentrated bet represents only
3% of their total Polymarket volume — far below the 70% concentration threshold. The
system correctly classified them as not a fresh burner account.

**What Signal B would have caught:** Magamyman had previously correctly predicted multiple
Iran/Israel events. A Signal B implementation (repeat predictor tracking) would score
them highly based on prior resolution record. Signal B is deferred until we have 25–30
resolved markets for training. This is the primary gap.

### Would an alert have fired?

**No.** Signal A correctly does not fire on established repeat predictors. Signal C
requires pre-announcement data which is inaccessible (market had $63M in volume; 4000
most recent trades were all post-announcement activity). No alert would have fired.

---

## Case 3: Iran — US Strikes Iran (Feb 28, 2026)

### What happened

The United States struck Iran on February 28, 2026. The announcement appears to have
broken around 06:00–06:30 UTC based on the YES price trajectory (jumped from ~$0.22 at
05:33 UTC to $0.70 at 06:33 UTC). Multiple accounts placed bets hours before any public
reporting:

| Time (UTC) | Account | Position | Price | Size | Signal A |
|------------|---------|----------|-------|------|----------|
| Feb 28 00:55 | nothingeverhappens911 | YES | $0.190 | $21,508 | **5/5 FIRES** |
| Feb 28 01:39 | nothingeverhappens911 | YES | $0.250 | $889 | Already alerted |
| Feb 28 05:21 | Magamyman | YES | $0.201 | $117,763 | 3/5, no fire |
| Feb 28 05:33 | Magamyman | YES | $0.223 | $132,242 | 3/5, no fire |
| ~06:00–06:30 | — | **ANNOUNCEMENT** | — | — | — |
| Feb 28 06:33 | Magamyman | YES | $0.700–0.800 | $12,000 | Post-event |

### Signal A evaluation: nothingeverhappens911

| Criterion | Result | Value |
|-----------|--------|-------|
| wallet_age | ✓ PASS | 0 days — wallet created same day |
| size | ✓ PASS | $21,508 single trade (>$15K threshold) |
| concentration | ✓ PASS | 96% of wallet volume in this market |
| price_insensitivity | ✓ PASS | Single trade at $0.19 (by definition insensitive) |
| net_buyer | ✓ PASS | $21,508 buys, $0 sells |
| **Signal A** | **✓ FIRED** | **5/5 criteria** |

**Alert would fire at 00:55 UTC, ~5 hours before the announcement.**

### Signal C on Feb 28 market

Pre-announcement hourly volume data is not accessible — by the time the event resolved,
the market had processed millions in volume and our 4000-trade window covers only the
two-hour post-announcement period (07:12–09:32 UTC, when YES was already at $0.98+).

From Magamyman's trade record, we know the Feb 28 market had elevated accumulation
starting Feb 16 (~$340K over Feb 16–18). This *suggests* above-baseline volume in the
days before the event, but Signal C hourly data for that period is not reconstructable
from available sources.

**Assessment:** Signal C *may* have fired on the Feb 28 market in the days leading up
to the event (Magamyman's $340K accumulation over Feb 16–18 would likely have produced
detectable surges), but this cannot be confirmed without pre-event hourly volume data.

### PizzINT (manual reconstruction)

No public reporting identified specific PizzINT DOUGHCON levels for the Feb 27–28 window.
If DOUGHCON was elevated (2+) the night of Feb 27–28, a composite TIER 3 alert would
have been possible. Without confirmation, this remains unknown.

### Composite alert reconstruction

```
⚠️  TIER 2 — PREPARE: US strikes Iran by February 28, 2026
    Fired: 2026-02-28 00:55 UTC

    Signal A: ✅ FIRED (5/5 criteria)
      Wallet: 0xa4eb...d010  (age: 0 days — created today)
      Position: $21,508 YES at $0.19
      Concentration: 96% of wallet volume in this market
      Criteria: 5/5

    Signal C: ⚠️  No established baseline (insufficient pre-event data)
    PizzINT:  ❓  Unknown

    Price: YES $0.19 at alert time
    Market: polymarket.com/event/us-strikes-iran-by-february-28

    Action: Brand-new wallet making concentrated large bet in conflict market.
            Evaluate immediately.
```

### Return calculation

| Scenario | Entry | Resolution | Return |
|----------|-------|------------|--------|
| Buy at Signal A fire (00:55 UTC) | $0.190 | $1.000 | **+426%** |
| Buy at Magamyman's 05:21 surge | $0.201 | $1.000 | **+398%** |
| Buy at 06:33 (post-announcement) | $0.750 | $1.000 | **+33%** |

Acting on the Signal A alert at 00:55 UTC — 5 hours before the announcement and 5.5
hours before prices reflected the event — would have returned +426% on invested capital.

---

## Synthesis: What Would Have Fired Live

| Signal Layer | Venezuela | Khamenei (Feb 16) | US Strikes (Feb 28) |
|---|---|---|---|
| Signal A (burner) | ✗ Wallet unknown | ✗ Old wallet (Signal B) | **✓ Fires at 00:55 UTC** |
| Signal C (volume surge) | ✗ Cold-start | ✗ No pre-event data | ❓ Possibly (data inaccessible) |
| PizzINT | ❓ Elevated reported | ❓ Unknown | ❓ Unknown |
| **Composite** | **No alert** | **No alert** | **TIER 2 at 00:55 UTC** |

---

## Implications for Live Deployment

### What the system catches well
- **Fresh burner wallets** making concentrated large bets on binary outcome markets.
  nothingeverhappens911 is a textbook case: 5/5 Signal A criteria, 5 hours of lead time.
- **Price-insensitive accumulation** at below-equilibrium prices (0.19 vs eventual $1.00).

### Known gaps

**Gap 1: Repeat predictors (Signal B)**
Magamyman (2× correct, large positions) is the highest-value case we cannot catch.
Signal B requires a database of resolved market predictions per wallet — cannot be built
until we have 25–30 resolved markets to establish a track record. This is the biggest
miss: Signal B would have fired on both Khamenei and Feb 28 for Magamyman.

**Gap 2: Cold-start markets**
Venezuela: market had near-zero volume before the event. Signal C requires an established
rolling baseline. New markets with low prior volume cannot produce Signal C alerts.
Mitigation: monitor markets from inception, build baselines over time.

**Gap 3: Data accessibility**
High-volume markets (>$10M) fill the 4000-trade cap with post-event activity within hours.
Pre-announcement burner wallets roll out of the accessible window. For Signal A live
monitoring, this is mitigated by polling every 10 minutes and caching newly-seen wallets
in real time (rather than trying to reconstruct from history).

**Gap 4: Cluster accounts**
The full $1.2M Iran insider cluster likely involved 4–6 wallets. We only found
nothingeverhappens911 ($21.5K). The other accounts (Planktonbets, etc.) have not been
located. In a live system, each fresh wallet would be evaluated independently on its own
trades — all would likely score 5/5 Signal A individually and fire separate alerts,
creating a reinforcing cluster signal.

### Bottom line

The Feb 28 case validates the core approach. A live system polling every 10 minutes
would have sent a TIER 2 alert at 00:55 UTC on February 28, 2026, with an available
entry price of $0.19 and a final resolution of $1.00 (+426%). The Venezuela and Khamenei
cases represent structural limitations (cold-start, repeat predictor) that require Phase
2 and Phase 3 improvements respectively.

**Proceed to Step 2: Market watchlist with 15–20 active geopolitical markets.**
