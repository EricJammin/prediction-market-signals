"""
Configuration constants for the Polymarket Whale Detection Backtest.
"""

# API endpoints
CLOB_API_BASE  = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE  = "https://data-api.polymarket.com"

# Pagination sentinel — Polymarket uses base64("-1") to signal end of results
CLOB_END_CURSOR = "LTE="

# Whale detection thresholds (in USDC)
SIZE_THRESHOLDS = [10_000, 25_000, 50_000]
CUMULATIVE_THRESHOLD = 25_000  # cumulative position in one outcome per market

# Wallet freshness: first trade within this many hours of the whale bet
FRESHNESS_WINDOW_HOURS = 96  # 4 days

# Concentration: fraction of total activity in a single market
CONCENTRATION_THRESHOLD = 0.80  # 80%

# Price insensitivity: whale kept buying even after price rose this much
PRICE_INSENSITIVITY_DELTA = 0.10  # 10 cents / 10 percentage points

# Signal A entry price floor/ceiling: ignore bets outside this range.
# Below floor = lottery ticket (no informational content).
# Above ceiling = market already expects the event (no edge in following).
SIGNAL_A_MIN_PRICE = 0.10
SIGNAL_A_MAX_PRICE = 0.50

# Minimum criteria score required to fire a signal (out of 4)
MIN_SIGNAL_SCORE = 3

# Detection delays to simulate (seconds after the triggering trade)
DETECTION_DELAYS_SECONDS = [
    5 * 60,       # 5 minutes  — automated near-real-time monitoring
    30 * 60,      # 30 minutes — semi-automated check
    2 * 3600,     # 2 hours    — manual review cycle
]

# API rate limiting
REQUEST_DELAY_SECONDS = 0.5   # sleep between requests
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# Pagination page size
CLOB_PAGE_SIZE = 500
GAMMA_PAGE_SIZE = 100

# Data directories (relative to project root)
DATA_DIR = "data"
RAW_TRADES_DIR = "data/raw_trades"
RAW_MARKETS_DIR = "data/raw_markets"
REPORT_PATH = "data/report.json"

# Minimum trade size to include in analysis (filter dust trades)
MIN_TRADE_SIZE_USDC = 10.0
