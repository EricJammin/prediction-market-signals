"""
Configuration for the Polymarket Alert Aggregator.
All tunable constants live here — no magic numbers in other modules.
"""

from __future__ import annotations

# ── API endpoints ──────────────────────────────────────────────────────────────
DATA_API_BASE  = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# ── Polling ────────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS  = 600   # 10 minutes between polls
BACKFILL_HOURS         = 168   # 7 days of history seeded on startup
WATCHLIST_REFRESH_SECS = 3600  # refresh active market list every 1 hour

# ── Signal C detection ─────────────────────────────────────────────────────────
SURGE_WINDOW_SECONDS      = 3600   # 1-hour volume bucket
SURGE_LOOKBACK_HOURS      = 168    # 7-day rolling baseline window
SURGE_MIN_BASELINE_USDC   = 500.0  # ignore dormant / illiquid markets
SURGE_MULTIPLIER_LOW      = 3.0    # 3–5× → score 0.5
SURGE_MULTIPLIER_HIGH     = 5.0    # >5× → score 1.0
# Minimum prior hourly buckets required before Signal C can fire.
# Prevents cold-start false positives on new or recently-added markets.
# 24 hours = 1 day minimum; 72 hours (3 days) recommended by BUILD_SPEC.
# Set lower for fast iteration; raise to 72 when deploying long-running monitor.
SIGNAL_C_MIN_BASELINE_HOURS = 24
# Minimum age of current-hour bucket before running surge detection.
# Prevents false positives from the first few trades of an anomalous hour.
SURGE_MIN_BUCKET_AGE_SECONDS = 900  # 15 minutes

# ── Price gate ─────────────────────────────────────────────────────────────────
# Only alert when YES price is in the actionable range.
# Below floor = lottery ticket. Above ceiling = already priced in.
SIGNAL_C_MIN_PRICE = 0.05
SIGNAL_C_MAX_PRICE = 0.60

# ── Composite scoring ──────────────────────────────────────────────────────────
# Each source contributes 0.0 / 0.5 / 1.0 to composite.
# High tier fires on composite >= threshold OR on the signal_c+news combo.
HIGH_TIER_COMPOSITE       = 2.5   # total score across all sources
MEDIUM_TIER_COMPOSITE     = 1.5
HIGH_TIER_SIGNAL_C_MIN    = 0.5   # any detectable surge...
HIGH_TIER_NEWS_UNEXPLAINED = 1.0  # ...with no news → always HIGH

# ── Alert deduplication ────────────────────────────────────────────────────────
ALERT_COOLDOWN_SECONDS = 21_600  # 6 hours before re-alerting same market

# ── News checker ──────────────────────────────────────────────────────────────
NEWS_LOOKBACK_HOURS = 4   # articles published within this window count as "recent"
# Google News RSS — free, no API key
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
# Score: 0 recent articles → 1.0 (unexplained), 1 → 0.5 (ambiguous), 2+ → 0.0 (explained)
NEWS_STRONG_THRESHOLD = 2   # articles needed to consider surge "explained by news"

# ── Market watchlist filters ───────────────────────────────────────────────────
# Low threshold so new markets are picked up from inception — avoiding the
# cold-start problem (Venezuela had $59/hr baseline and zero coverage).
# Seed markets bypass this filter entirely.
MIN_MARKET_VOLUME_USDC = 5_000.0    # skip only truly dormant markets
GAMMA_WATCHLIST_CATEGORIES = [      # Gamma API category strings to monitor
    "politics",
    "geopolitics",
    "world",
    "us-politics",
    "military",
]

# ── Signal A: burner wallet detection ─────────────────────────────────────────
# Lower single-trade floor than backtest ($10K) to catch split accumulation.
# Concentration threshold lowered from backtest (80%) per BUILD_SPEC.md (70%).
SIGNAL_A_MIN_SINGLE_TRADE_USDC = 5_000    # evaluate wallet on any trade >= this
SIGNAL_A_SIZE_THRESHOLD_USDC   = 15_000   # cumulative BUY must reach this for size criterion
SIGNAL_A_MIN_CRITERIA          = 4        # fire if >= 4 of 5 criteria pass
SIGNAL_A_BURNER_AGE_DAYS       = 14       # on-chain wallet age <= this → "fresh"
SIGNAL_A_CONCENTRATION_MIN     = 0.70     # >= 70% of wallet's total buy in one (market, side)
SIGNAL_A_ENTRY_PRICE_MIN       = 0.10     # informational zone floor
SIGNAL_A_ENTRY_PRICE_MAX       = 0.50     # informational zone ceiling
SIGNAL_A_WASH_TRADE_MAX_SELL   = 0.20     # sells > 20% of buys → wash trader flag

# ── Wallet age cache ───────────────────────────────────────────────────────────
WALLET_AGE_CACHE_PATH = "data/wallet_ages.json"

# ── HTTP ───────────────────────────────────────────────────────────────────────
REQUEST_DELAY_SECONDS  = 0.3   # between paginated requests
MAX_RETRIES            = 3
RETRY_BACKOFF_SECONDS  = 2.0
MIN_TRADE_SIZE_USDC    = 10.0

# ── State ──────────────────────────────────────────────────────────────────────
DB_PATH = "data/monitor.db"
PIZZINT_STATE_PATH = "data/pizzint_state.json"
