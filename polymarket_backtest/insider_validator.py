"""
Insider Trading Validation Script

Validates that Signal A and Signal C would have detected documented insider
trading cases on Polymarket before the events resolved.

Known cases:
  1. Venezuela $32K account — "Maduro in US custody by Jan 31, 2026"
     Turned $32,537 into $436,000. Only 4 positions, all Venezuela.
     Key bets placed Jan 2 (~4h before capture announced Jan 3).

  2. Magamyman — Khamenei/Iran markets
     $553K+ profit. First trade 71 minutes before Khamenei death announced.
     Also $431K on "US strikes Iran by Feb 28".

  3. Six fresh Iran accounts — "US strikes Iran by Feb 28, 2026"
     Collectively $1.2M. All accounts created Feb 2026. All funded within
     24h of Feb 28 strikes. Traced by Bubblemaps: nothingeverhappens911,
     Skoobidoobnj, Planktonbets + 3 more.

  4. ricosuave666 — Israel strike markets
     100% win rate on 4 Israel security events over 7 months. $155K+ profit.
     Israeli military reservist + civilian indicted Feb 2026.
     Wallet: 0x0afc7ce56285bde1fbe3a75efaffdfc86d6530b2

Data strategy:
  - Market metadata: Gamma API slug lookup (NOT search, which is broken)
  - Market trades: data-api /trades (hard-capped at 4000 — newest first)
  - Wallet trades: data-api /trades?user={wallet} (per-wallet, ~complete for
    insiders who had few positions)
  - Wallet lookup by username: scan existing trade data for name/pseudonym match

Run from polymarket_backtest/ directory:
    python3 insider_validator.py
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import datetime
import statistics

import requests
import pandas as pd
from dotenv import load_dotenv

import config
from polygonscan_client import PolygonscanClient
from signal_c_analysis import detect_surges, compute_returns, SurgeEvent

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("insider_validator")

# ── Directories ────────────────────────────────────────────────────────────────
FULL_TRADES_DIR = Path("data/insider_trades")
FULL_MARKETS_DIR = Path("data/insider_markets")
FULL_TRADES_DIR.mkdir(parents=True, exist_ok=True)
FULL_MARKETS_DIR.mkdir(parents=True, exist_ok=True)

# ── Target markets ────────────────────────────────────────────────────────────
TARGET_MARKETS = [
    {
        "slug": "maduro-in-us-custody-by-january-31",
        "name": "Maduro in US custody by Jan 31, 2026",
        "resolution": "YES",
        "event_date": "2026-01-03",
        "insider_keys": ["venezuela_32k"],
    },
    {
        "slug": "khamenei-out-as-supreme-leader-of-iran-by-march-31",
        "name": "Khamenei out as Supreme Leader by March 31, 2026",
        "resolution": "YES",
        "event_date": "2026-02-16",  # Khamenei died Feb 16 2026; market resolved by March 31
        "insider_keys": ["magamyman"],
    },
    {
        "slug": "us-strikes-iran-by-february-28-2026",
        "name": "US strikes Iran by Feb 28, 2026",
        "resolution": "YES",
        "event_date": "2026-02-28",
        "insider_keys": ["magamyman", "nothingeverhappens911", "skoobidoobnj", "planktonbets"],
        "condition_id_override": "0x3488f31e6449f9803f99a8b5dd232c7ad883637f1c86e6953305a2ef19c77f20",
    },
    {
        "slug": "us-military-action-against-iran-by-saturday",
        "name": "US military action against Iran by Saturday? (Jun 2025)",
        "resolution": "YES",
        "event_date": "2025-06-14",
        "insider_keys": ["skoobidoobnj"],
        "condition_id_override": "0x6c6cff89b135f5684f5a25a98fd0335814d1bc411d08bfcf0be400db76ce511a",
    },
    {
        "slug": "israel-strikes-iran-by-january-31-2026-894-994-453",
        "name": "Israel strikes Iran by January 31, 2026",
        "resolution": "YES",
        "event_date": "2026-01-31",
        "insider_keys": ["ricosuave666"],
        "condition_id_override": "0x23fd2b26c4e095465ba0d2ebce8d5eda57009ddc59aad8b68ab19ca968b41eed",
    },
    {
        "slug": "israel-military-action-against-iran-by-friday-477",
        "name": "Israel military action against Iran by Friday? (Jun 2025)",
        "resolution": "YES",
        "event_date": "2025-06-13",
        "insider_keys": ["ricosuave666"],
        "condition_id_override": "0x7f39808829da93cfd189807f13f6d86a0e604835e6f9482d8094fac46b3abaac",
    },
]

# Additional slug variants to try if primary slug fails
SLUG_VARIANTS = {
    "maduro-in-us-custody-by-january-31": [
        "maduro-in-us-custody",
        "will-maduro-be-in-us-custody-by-january-31",
        "maduro-captured-january-2026",
        "maduro-removed-january-2026",
        "will-nicolas-maduro-be-in-us-custody",
    ],
    "us-strikes-iran-by-february-28-2026": [
        "us-strikes-iran-by-february-28",
        "us-strikes-iran-before-march-1",
        "will-the-us-strike-iran-by-february-28",
    ],
    "israel-strikes-iran-by-january-31": [
        "israel-strikes-iran-before-february",
        "will-israel-strike-iran-by-january-31",
        "israel-iran-strike-january-2026",
    ],
}

# ── Known insider profiles ────────────────────────────────────────────────────
@dataclass
class InsiderProfile:
    key: str
    username: Optional[str]
    wallet: Optional[str]          # known address if published
    profit_usdc: float
    description: str
    expected_signal: str           # "A", "B", or "A+C"
    first_trade_approx: str        # approximate date of key trade (YYYY-MM-DD)
    market_keys: list[str]         # which markets they traded

KNOWN_INSIDERS: dict[str, InsiderProfile] = {
    "ricosuave666": InsiderProfile(
        key="ricosuave666",
        username="ricosuave666",
        wallet="0x0afc7ce56285bde1fbe3a75efaffdfc86d6530b2",
        profit_usdc=155_699,
        description="Israeli reservist, 4 security events, 100% win rate, indicted Feb 2026",
        expected_signal="B",
        first_trade_approx="2025-06-01",
        market_keys=["israel-strikes-iran-by-january-31"],
    ),
    "magamyman": InsiderProfile(
        key="magamyman",
        username="Magamyman",
        wallet="0x4dfd481c16d9995b809780fd8a9808e8689f6e4a",
        profit_usdc=553_000,
        description="Khamenei/Iran markets, first trade 71min before Khamenei death (Feb 16 2026)",
        expected_signal="A",
        first_trade_approx="2026-02-16",
        market_keys=["khamenei-out-as-supreme-leader-of-iran-by-march-31",
                     "us-strikes-iran-by-february-28-2026"],
    ),
    "venezuela_32k": InsiderProfile(
        key="venezuela_32k",
        username=None,
        wallet=None,  # partial: 0x31a56e...; will search
        profit_usdc=436_000,
        description="Turned $32,537 → $436K on Maduro capture, bet placed ~4h before capture",
        expected_signal="A",
        first_trade_approx="2026-01-02",
        market_keys=["maduro-in-us-custody-by-january-31"],
    ),
    "nothingeverhappens911": InsiderProfile(
        key="nothingeverhappens911",
        username="nothingeverhappens911",
        wallet="0xa4eb52229991c074bc560f825bf2776d77acd010",
        profit_usdc=560_000,
        description="Fresh account, bought YES shares at ~10.8¢ on US strikes Iran Feb 28",
        expected_signal="A",
        first_trade_approx="2026-02-28",
        market_keys=["us-strikes-iran-by-february-28-2026"],
    ),
    "planktonbets": InsiderProfile(
        key="planktonbets",
        username="Planktonbets",
        wallet=None,
        profit_usdc=173_907,
        description="Fresh account, part of six-account Iran cluster, 7 profitable predictions",
        expected_signal="A",
        first_trade_approx="2026-02-28",
        market_keys=["us-strikes-iran-by-february-28-2026"],
    ),
    "skoobidoobnj": InsiderProfile(
        key="skoobidoobnj",
        username="Skoobidoobnj",
        wallet="0xfe6eee00d36717359578ddb4d6e091d56bc9074e",
        profit_usdc=None,
        description="Iran cluster, $195K total YES bets across US/Israel strike markets (Jun 2025–Feb 2026)",
        expected_signal="A",
        first_trade_approx="2025-06-12",
        market_keys=["us-strikes-iran-by-february-28-2026", "us-military-action-against-iran-by-saturday"],
    ),
}

# ── Signal A thresholds (adjusted for insider detection) ─────────────────────
SIGNAL_A_MIN_PRICE      = 0.05    # looser floor — insiders sometimes enter very low
SIGNAL_A_MAX_PRICE      = 0.50
SIGNAL_A_SIZE_THRESHOLD = 15_000  # $15K single trade OR cumulative
SIGNAL_A_CUMULATIVE     = 25_000  # $25K cumulative in one market/side
SIGNAL_A_CONCENTRATION  = 0.70    # 70% of wallet activity in one market
SIGNAL_A_PRICE_DELTA    = 0.08    # bought at rising prices (8¢ spread)
WALLET_AGE_DAYS_FRESH   = 30      # ≤30 days from first on-chain tx


# ── HTTP client ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    return s


# ── Market discovery ──────────────────────────────────────────────────────────

def find_market_by_slug(slug: str, session: requests.Session) -> Optional[dict]:
    """Attempt to find a Polymarket market by exact slug."""
    url = f"{config.GAMMA_API_BASE}/markets"
    try:
        r = session.get(url, params={"slug": slug}, timeout=20)
        r.raise_for_status()
        markets = r.json() if isinstance(r.json(), list) else []
        for m in markets:
            if m.get("slug") == slug:
                return _normalize_gamma(m)
    except Exception as e:
        logger.warning("Slug lookup failed for %s: %s", slug, e)
    return None


def find_market(market_def: dict, session: requests.Session) -> Optional[dict]:
    """
    Try primary slug, then variants. Returns normalized market dict or None.
    If condition_id_override is set, skip API lookup and return minimal market dict.
    Caches result to data/insider_markets/{condition_id}.json.
    """
    if "condition_id_override" in market_def:
        cid = market_def["condition_id_override"]
        logger.info("Using condition_id_override for %s: %s", market_def["slug"], cid[:20])
        return {
            "condition_id": cid,
            "slug": market_def["slug"],
            "question": market_def["name"],
            "resolution": market_def.get("resolution"),
            "yes_token_id": "",
            "no_token_id": "",
            "volume_usdc": 0,
        }

    slug = market_def["slug"]
    slugs_to_try = [slug] + SLUG_VARIANTS.get(slug, [])

    for s in slugs_to_try:
        result = find_market_by_slug(s, session)
        if result:
            logger.info("Found market %s via slug=%s", result["condition_id"][:20], s)
            cache_path = FULL_MARKETS_DIR / f"{result['condition_id']}.json"
            if not cache_path.exists():
                cache_path.write_text(json.dumps(result, indent=2))
            return result
        time.sleep(0.3)

    logger.warning("Could not find market: %s (tried %d slugs)", slug, len(slugs_to_try))
    return None


def _normalize_gamma(raw: dict) -> dict:
    tokens_raw = raw.get("clobTokenIds") or raw.get("tokens") or "[]"
    try:
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except Exception:
        tokens = []
    prices_raw = raw.get("outcomePrices") or "[]"
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except Exception:
        prices = []
    if prices and prices[0] == "1":
        resolution = "YES"
    elif len(prices) > 1 and prices[1] == "1":
        resolution = "NO"
    else:
        resolution = None
    return {
        "condition_id": raw.get("conditionId") or raw.get("condition_id") or "",
        "slug": raw.get("slug") or "",
        "question": raw.get("question") or raw.get("title") or "",
        "resolution": resolution,
        "yes_token_id": tokens[0] if len(tokens) > 0 else "",
        "no_token_id": tokens[1] if len(tokens) > 1 else "",
        "volume_usdc": float(raw.get("volume") or raw.get("volumeNum") or 0),
    }


# ── Trade fetching ─────────────────────────────────────────────────────────────

def fetch_market_trades(condition_id: str, session: requests.Session, force: bool = False) -> list[dict]:
    """
    Fetch up to 4000 trades for a market (data-api hard cap).
    Newest-first. Cached to data/insider_trades/market_{condition_id}.json.
    """
    cache_path = FULL_TRADES_DIR / f"market_{condition_id}.json"
    if cache_path.exists() and not force:
        logger.info("Using cached market trades for %s", condition_id[:20])
        return json.loads(cache_path.read_text())

    url = f"{config.DATA_API_BASE}/trades"
    all_trades: list[dict] = []
    seen: set[str] = set()

    for offset in [0, 1000, 2000, 3000]:
        try:
            r = session.get(url, params={"market": condition_id, "limit": 1000, "offset": offset}, timeout=30)
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            logger.warning("Trade fetch failed offset=%d: %s", offset, e)
            break
        if not page:
            break
        new = 0
        for t in page:
            h = t.get("transactionHash") or t.get("transaction_hash") or ""
            if h not in seen:
                seen.add(h)
                all_trades.append(t)
                new += 1
        logger.info("  market offset=%d: %d new trades", offset, new)
        if len(page) < 1000:
            break
        time.sleep(config.REQUEST_DELAY_SECONDS)

    logger.info("Fetched %d market trades for %s", len(all_trades), condition_id[:20])
    cache_path.write_text(json.dumps(all_trades, indent=2))
    return all_trades


def fetch_wallet_trades(wallet: str, session: requests.Session, force: bool = False) -> list[dict]:
    """
    Fetch all trades for a specific wallet across all markets.
    Insiders with few positions will have complete history here.
    Cached to data/insider_trades/wallet_{wallet}.json.
    """
    cache_path = FULL_TRADES_DIR / f"wallet_{wallet.lower()}.json"
    if cache_path.exists() and not force:
        logger.info("Using cached wallet trades for %s", wallet[:20])
        return json.loads(cache_path.read_text())

    url = f"{config.DATA_API_BASE}/trades"
    all_trades: list[dict] = []
    seen: set[str] = set()

    for offset in [0, 1000, 2000, 3000]:
        try:
            r = session.get(url, params={"user": wallet.lower(), "limit": 1000, "offset": offset}, timeout=30)
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            logger.warning("Wallet trade fetch failed offset=%d: %s", offset, e)
            break
        if not page or not isinstance(page, list):
            break
        new = 0
        for t in page:
            h = t.get("transactionHash") or t.get("transaction_hash") or ""
            if h not in seen:
                seen.add(h)
                all_trades.append(t)
                new += 1
        logger.info("  wallet %s offset=%d: %d new trades", wallet[:12], offset, new)
        if len(page) < 1000:
            break
        time.sleep(config.REQUEST_DELAY_SECONDS)

    logger.info("Fetched %d wallet trades for %s", len(all_trades), wallet[:20])
    cache_path.write_text(json.dumps(all_trades, indent=2))
    return all_trades


def search_wallet_by_username(
    username: str,
    market_trades: list[dict],
) -> Optional[str]:
    """
    Search cached trade records for a wallet matching the given username/pseudonym.
    Returns the proxyWallet address, or None if not found.
    """
    username_lower = username.lower()
    for t in market_trades:
        name = (t.get("name") or "").lower()
        pseudo = (t.get("pseudonym") or "").lower()
        if username_lower in name or username_lower in pseudo:
            return (t.get("proxyWallet") or "").lower()
    return None


# ── Signal A criterion evaluation ─────────────────────────────────────────────

@dataclass
class CriterionResult:
    name: str
    passed: bool
    value: str       # human-readable observed value
    threshold: str   # what threshold was required


@dataclass
class SignalAResult:
    wallet: str
    market_id: str
    insider_key: str
    criteria: list[CriterionResult]
    n_passed: int
    signal_fired: bool
    entry_price: Optional[float]
    potential_return: Optional[float]   # if YES bought at entry_price, resolved YES
    notes: list[str] = field(default_factory=list)


def evaluate_signal_a(
    insider: InsiderProfile,
    wallet: str,
    market_id: str,
    market_trades: list[dict],
    wallet_trades: list[dict],
    yes_token_id: str,
    no_token_id: str,
    resolution: Optional[str],
    poly_client: Optional[PolygonscanClient],
) -> SignalAResult:
    """
    Run Signal A's 5 criteria against a specific (wallet, market) pair.
    Uses wallet_trades for per-wallet stats, market_trades for price context.
    """
    criteria: list[CriterionResult] = []
    notes: list[str] = []

    # ── Filter to this wallet's trades in this market ─────────────────────────
    wt_in_market = [
        t for t in wallet_trades
        if (t.get("conditionId") or t.get("market_id") or "") == market_id
    ]
    if not wt_in_market:
        # Fall back to scanning market trades for this wallet
        wt_in_market = [
            t for t in market_trades
            if (t.get("proxyWallet") or "").lower() == wallet.lower()
        ]

    if not wt_in_market:
        return SignalAResult(
            wallet=wallet, market_id=market_id, insider_key=insider.key,
            criteria=[], n_passed=0, signal_fired=False,
            entry_price=None, potential_return=None,
            notes=["No trades found for this wallet in this market"],
        )

    # ── Parse trades ──────────────────────────────────────────────────────────
    trades_parsed = []
    for t in wt_in_market:
        ts = int(float(t.get("timestamp", 0)))
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
        side_raw = (t.get("side") or "").upper()   # BUY/SELL from taker perspective
        outcome = (t.get("outcome") or "").strip().upper()
        asset = t.get("asset") or ""
        outcome_upper = outcome.upper()
        if yes_token_id and asset == yes_token_id:
            token_side = "YES"
        elif no_token_id and asset == no_token_id:
            token_side = "NO"
        elif outcome_upper in ("YES", "TRUE"):
            token_side = "YES"
        elif outcome_upper in ("NO", "FALSE"):
            token_side = "NO"
        else:
            token_side = None
        trades_parsed.append({
            "ts": ts, "price": price, "size": size,
            "side_raw": side_raw, "token_side": token_side,
        })

    if not trades_parsed:
        return SignalAResult(
            wallet=wallet, market_id=market_id, insider_key=insider.key,
            criteria=[], n_passed=0, signal_fired=False,
            entry_price=None, potential_return=None,
            notes=["Could not parse trades (missing token side)"],
        )

    trades_parsed.sort(key=lambda x: x["ts"])
    first_trade_ts = trades_parsed[0]["ts"]
    first_dt = datetime.datetime.utcfromtimestamp(first_trade_ts)

    # ── Criterion 1: Wallet age ───────────────────────────────────────────────
    age_days: Optional[float] = None
    if poly_client and wallet:
        try:
            first_tx_ts = poly_client.first_transaction_timestamp(wallet)
            if first_tx_ts:
                age_days = (first_trade_ts - first_tx_ts) / 86400
        except Exception as e:
            notes.append(f"Polygonscan lookup failed: {e}")

    if age_days is None:
        # Fall back: assume new if this wallet doesn't appear in older data
        c_wallet_age = CriterionResult(
            name="wallet_age", passed=False,
            value="unknown (Polygonscan lookup failed)",
            threshold=f"≤{WALLET_AGE_DAYS_FRESH} days from first on-chain tx"
        )
    else:
        passed = age_days <= WALLET_AGE_DAYS_FRESH
        c_wallet_age = CriterionResult(
            name="wallet_age", passed=passed,
            value=f"{age_days:.0f} days old at first trade",
            threshold=f"≤{WALLET_AGE_DAYS_FRESH} days"
        )
    criteria.append(c_wallet_age)

    # ── Criterion 2: Size ─────────────────────────────────────────────────────
    yes_buys = [t for t in trades_parsed if t["token_side"] == "YES" and t["side_raw"] == "BUY"]
    no_buys  = [t for t in trades_parsed if t["token_side"] == "NO"  and t["side_raw"] == "BUY"]
    yes_total = sum(t["size"] for t in yes_buys)
    no_total  = sum(t["size"] for t in no_buys)
    dominant_side = "YES" if yes_total >= no_total else "NO"
    dominant_total = max(yes_total, no_total)
    max_single = max((t["size"] for t in trades_parsed), default=0)
    size_ok = max_single >= SIGNAL_A_SIZE_THRESHOLD or dominant_total >= SIGNAL_A_CUMULATIVE
    criteria.append(CriterionResult(
        name="size", passed=size_ok,
        value=f"max_single=${max_single:,.0f} | cumulative_{dominant_side}=${dominant_total:,.0f}",
        threshold=f"single≥${SIGNAL_A_SIZE_THRESHOLD:,} OR cum≥${SIGNAL_A_CUMULATIVE:,}"
    ))

    # ── Criterion 3: Concentration ────────────────────────────────────────────
    # Fraction of wallet's total Polymarket volume in THIS market
    wallet_total_volume = sum(float(t.get("size") or 0) for t in wallet_trades)
    market_volume_this_wallet = sum(t["size"] for t in trades_parsed)
    concentration = market_volume_this_wallet / wallet_total_volume if wallet_total_volume > 0 else 0
    conc_ok = concentration >= SIGNAL_A_CONCENTRATION
    criteria.append(CriterionResult(
        name="concentration", passed=conc_ok,
        value=f"{concentration:.0%} of wallet volume in this market",
        threshold=f"≥{SIGNAL_A_CONCENTRATION:.0%}"
    ))

    # ── Criterion 4: Price insensitivity ─────────────────────────────────────
    # Did the wallet keep buying YES even as price rose ≥ SIGNAL_A_PRICE_DELTA?
    dominant_buys = yes_buys if dominant_side == "YES" else no_buys
    if len(dominant_buys) >= 2:
        prices = [t["price"] for t in dominant_buys]
        price_spread = max(prices) - min(prices)
        pi_ok = price_spread >= SIGNAL_A_PRICE_DELTA
        criteria.append(CriterionResult(
            name="price_insensitivity", passed=pi_ok,
            value=f"spread={price_spread:.3f} (min={min(prices):.3f} max={max(prices):.3f})",
            threshold=f"≥{SIGNAL_A_PRICE_DELTA:.2f} spread"
        ))
    else:
        # Single trade — counts as price insensitive (bought regardless)
        entry_price = dominant_buys[0]["price"] if dominant_buys else None
        pi_ok = True
        criteria.append(CriterionResult(
            name="price_insensitivity", passed=True,
            value=f"single trade at {entry_price:.3f} (insensitive by definition)",
            threshold=f"≥{SIGNAL_A_PRICE_DELTA:.2f} spread"
        ))

    # ── Criterion 5: Net buyer ────────────────────────────────────────────────
    sells = sum(t["size"] for t in trades_parsed if t["side_raw"] == "SELL")
    buys  = sum(t["size"] for t in trades_parsed if t["side_raw"] == "BUY")
    net_buyer_ok = buys > sells
    criteria.append(CriterionResult(
        name="net_buyer", passed=net_buyer_ok,
        value=f"bought=${buys:,.0f} sold=${sells:,.0f} net=${buys-sells:,.0f}",
        threshold="buys > sells"
    ))

    # ── Price floor criterion ─────────────────────────────────────────────────
    # Check if dominant buys are in actionable price range
    if dominant_buys:
        entry_prices = [t["price"] for t in dominant_buys]
        avg_entry = sum(entry_prices) / len(entry_prices)
        if not (SIGNAL_A_MIN_PRICE <= avg_entry <= SIGNAL_A_MAX_PRICE):
            notes.append(
                f"Price floor check: avg entry {avg_entry:.3f} outside "
                f"[{SIGNAL_A_MIN_PRICE}, {SIGNAL_A_MAX_PRICE}] — "
                f"{'below floor (lottery)' if avg_entry < SIGNAL_A_MIN_PRICE else 'above ceiling (priced in)'}"
            )
    else:
        avg_entry = None

    # ── Return calculation ────────────────────────────────────────────────────
    entry_price = avg_entry if avg_entry else None
    potential_return = None
    if entry_price and resolution == "YES" and dominant_side == "YES" and entry_price > 0:
        potential_return = (1.0 - entry_price) / entry_price
    elif entry_price and resolution == "NO" and dominant_side == "NO" and entry_price > 0:
        potential_return = (1.0 - entry_price) / entry_price

    n_passed = sum(1 for c in criteria if c.passed)
    signal_fired = n_passed >= 4  # Signal A requires 4 of 5

    return SignalAResult(
        wallet=wallet, market_id=market_id, insider_key=insider.key,
        criteria=criteria, n_passed=n_passed, signal_fired=signal_fired,
        entry_price=entry_price, potential_return=potential_return,
        notes=notes,
    )


# ── Signal C validation for Venezuela Jan 2-3 ────────────────────────────────

def validate_signal_c_venezuela(
    condition_id: str,
    yes_token_id: str,
    no_token_id: str,
    resolution: str,
    market_trades: list[dict],
) -> None:
    """
    Run Signal C surge detection focused on Jan 1-4, 2026 (the Venezuela
    Maduro capture window). Reports whether the pre-announcement surge was
    detectable and whether it was news-driven.
    """
    print("\n" + "=" * 80)
    print("SIGNAL C VALIDATION: Venezuela Maduro Capture (Jan 2-3, 2026)")
    print("=" * 80)

    # Parse trades to DataFrame (same logic as signal_c_analysis.load_trades)
    rows = []
    for t in market_trades:
        ts = int(float(t.get("timestamp", 0)))
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
        outcome = (t.get("outcome") or "").strip().upper()
        asset = t.get("asset") or ""
        if yes_token_id and asset == yes_token_id:
            side = "YES"
        elif no_token_id and asset == no_token_id:
            side = "NO"
        elif outcome in ("YES", "TRUE"):
            side = "YES"
        elif outcome in ("NO", "FALSE"):
            side = "NO"
        else:
            continue
        if size < 10 or price <= 0:
            continue
        rows.append({"timestamp": ts, "side": side, "price": price, "size": size})

    if not rows:
        print("  No trade data available for this market.")
        return

    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    jan1_ts = int(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
    jan4_ts = int(datetime.datetime(2026, 1, 4, tzinfo=datetime.timezone.utc).timestamp())
    capture_ts = int(datetime.datetime(2026, 1, 3, 18, 0, tzinfo=datetime.timezone.utc).timestamp())  # ~6pm UTC Jan 3

    # Show data coverage
    if not df.empty:
        oldest = datetime.datetime.utcfromtimestamp(df["timestamp"].min())
        newest = datetime.datetime.utcfromtimestamp(df["timestamp"].max())
        print(f"  Trade data coverage: {oldest.strftime('%Y-%m-%d %H:%M')} → {newest.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  Total trades in dataset: {len(df):,}")

        # How many trades are in the Jan 1-4 window?
        jan_df = df[(df["timestamp"] >= jan1_ts) & (df["timestamp"] <= jan4_ts)]
        print(f"  Trades in Jan 1-4 window: {len(jan_df):,}")

    # Run Signal C surge detection on full dataset
    from signal_c_analysis import MARKETS_TO_ANALYZE
    market_info = {
        "condition_id": condition_id,
        "name": "Maduro in US custody by Jan 31, 2026",
        "resolution": resolution or "YES",
        "real_event_date": "2026-01-03",
        "real_event_happened": True,
        "note": "Maduro captured Jan 3, 2026",
    }

    surges = detect_surges(df)
    for s in surges:
        compute_returns(s, market_info["resolution"], True)

    print(f"\n  Total Signal C surges detected: {len(surges)}")

    # Focus on surges near the capture event
    capture_window_surges = [
        s for s in surges
        if jan1_ts <= s.hour_ts <= jan4_ts
    ]

    if not capture_window_surges:
        print(f"  No surges detected in Jan 1-4 window.")
        if not df[(df["timestamp"] >= jan1_ts) & (df["timestamp"] <= jan4_ts)].empty:
            # Compute hourly volumes manually to explain
            window_df = df[(df["timestamp"] >= jan1_ts) & (df["timestamp"] <= jan4_ts)].copy()
            window_df["hour"] = (window_df["timestamp"] // 3600) * 3600
            hourly = window_df.groupby("hour")["size"].sum()
            print(f"\n  Hourly volumes Jan 1-4 (for Signal C debug):")
            for h, v in hourly.items():
                dt = datetime.datetime.utcfromtimestamp(h)
                flag = " ◄ CAPTURE ANNOUNCEMENT" if h <= capture_ts < h + 3600 else ""
                print(f"    {dt.strftime('%Y-%m-%d %H:%M')}: ${v:,.0f}{flag}")
        else:
            print("  (Jan 1-4 data not in 4000-trade window — market had too many later trades)")
    else:
        print(f"\n  Surges in Jan 1-4 capture window:")
        print(f"  {'Date/Time UTC':<20} {'Ratio':>7} {'Vol':>10} {'YES@surge':>10} {'vs capture'}")
        print("  " + "-" * 65)
        for s in capture_window_surges:
            dt_str = s.datetime_utc.strftime("%Y-%m-%d %H:%M")
            lead = (capture_ts - s.hour_ts) / 3600
            lead_str = f"{lead:.1f}h before" if lead > 0 else f"{abs(lead):.1f}h after"
            yes_str = f"${s.yes_price_at_surge:.3f}" if s.yes_price_at_surge else "N/A"
            print(f"  {dt_str:<20} {s.surge_ratio:>7.1f}x {s.surge_volume:>10,.0f} {yes_str:>10}  {lead_str}")

    # Show all surges if we have them
    if surges and len(capture_window_surges) == 0:
        print(f"\n  All {len(surges)} detected surges (none in Jan 1-4):")
        for s in surges[:5]:
            dt_str = s.datetime_utc.strftime("%Y-%m-%d %H:%M")
            print(f"    {dt_str}  {s.surge_ratio:.1f}x  ${s.surge_volume:,.0f}")
        if len(surges) > 5:
            print(f"    ... and {len(surges)-5} more")


# ── Report formatting ─────────────────────────────────────────────────────────

def print_signal_a_result(result: SignalAResult, insider: InsiderProfile) -> None:
    w = 90
    print(f"\n  {insider.description}")
    print(f"  Wallet: {result.wallet or 'unknown'}")
    print(f"  Expected signal type: {insider.expected_signal}")
    print(f"  {'Criterion':<22} {'Pass':>5}  {'Value'}")
    print("  " + "-" * (w - 2))
    for c in result.criteria:
        tick = "✓" if c.passed else "✗"
        print(f"  {c.name:<22} {tick:>5}  {c.value}")
        print(f"  {'':22} {'':5}  Threshold: {c.threshold}")
    print()
    status = "FIRED ✓" if result.signal_fired else "DID NOT FIRE ✗"
    print(f"  Signal A result: {status}  ({result.n_passed}/5 criteria passed, need 4)")
    if result.entry_price:
        print(f"  Entry price: {result.entry_price:.3f}")
    if result.potential_return is not None:
        print(f"  Potential return if bought at entry: {result.potential_return:+.1%}")
    for note in result.notes:
        print(f"  Note: {note}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\nPolymarket Insider Trading Validator")
    print("=====================================")
    print(f"Signal A thresholds: size≥${SIGNAL_A_SIZE_THRESHOLD:,} | concentration≥{SIGNAL_A_CONCENTRATION:.0%} | price {SIGNAL_A_MIN_PRICE}-{SIGNAL_A_MAX_PRICE}")
    print(f"Wallet age freshness: ≤{WALLET_AGE_DAYS_FRESH} days\n")

    session = _make_session()
    poly_client = PolygonscanClient()

    # ── Step 1: Find markets ──────────────────────────────────────────────────
    print("Step 1: Locating target markets via Gamma API slug lookup")
    print("-" * 60)
    resolved_markets: dict[str, dict] = {}  # slug → market metadata

    for mdef in TARGET_MARKETS:
        slug = mdef["slug"]
        cached = list(FULL_MARKETS_DIR.glob(f"*.json"))
        # Check if we have a cached market for this slug
        found_cached = None
        for cp in cached:
            m = json.loads(cp.read_text())
            if m.get("slug") == slug:
                found_cached = m
                break
        if found_cached:
            print(f"  [{found_cached['condition_id'][:20]}...] {found_cached['question'][:60]} (cached)")
            resolved_markets[slug] = found_cached
        else:
            market = find_market(mdef, session)
            if market:
                print(f"  [{market['condition_id'][:20]}...] {market['question'][:60]}")
                resolved_markets[slug] = market
            else:
                print(f"  NOT FOUND: {slug}")

    if not resolved_markets:
        print("\nCould not find any target markets. Proceeding with wallet-only analysis.\n")

    # ── Step 2: Collect trade data ────────────────────────────────────────────
    print(f"\nStep 2: Collecting trade data ({len(resolved_markets)} markets + {len(KNOWN_INSIDERS)} wallets)")
    print("-" * 60)

    all_market_trades: dict[str, list[dict]] = {}   # condition_id → trades
    slug_to_cid: dict[str, str] = {}

    for slug, market in resolved_markets.items():
        cid = market["condition_id"]
        slug_to_cid[slug] = cid
        print(f"  Fetching market trades: {cid[:20]}...")
        trades = fetch_market_trades(cid, session)
        all_market_trades[cid] = trades
        print(f"    → {len(trades)} trades")
        time.sleep(0.3)

    # Fetch wallet trades for known insiders with confirmed addresses
    wallet_trades: dict[str, list[dict]] = {}  # wallet → trades

    for key, insider in KNOWN_INSIDERS.items():
        if insider.wallet:
            print(f"  Fetching wallet trades: {key} ({insider.wallet[:16]}...)")
            wt = fetch_wallet_trades(insider.wallet, session)
            wallet_trades[insider.wallet.lower()] = wt
            print(f"    → {len(wt)} trades across all markets")
            time.sleep(0.3)

    # ── Step 3: Find unknown wallet addresses by username ─────────────────────
    print("\nStep 3: Searching for insider wallets by username")
    print("-" * 60)

    all_trade_pool: list[dict] = []
    for trades in all_market_trades.values():
        all_trade_pool.extend(trades)

    for key, insider in KNOWN_INSIDERS.items():
        if insider.wallet is None and insider.username:
            found = search_wallet_by_username(insider.username, all_trade_pool)
            if found:
                print(f"  Found {insider.username} → {found}")
                KNOWN_INSIDERS[key].wallet = found
                wt = fetch_wallet_trades(found, session)
                wallet_trades[found.lower()] = wt
                print(f"    → {len(wt)} wallet trades")
            else:
                print(f"  {insider.username}: not found in available trade data (market may be outside 4000-trade window)")

    # ── Step 4: Run Signal A ──────────────────────────────────────────────────
    print("\nStep 4: Signal A Evaluation — Known Insider Wallets")
    print("=" * 80)

    summary_rows = []
    for mdef in TARGET_MARKETS:
        slug = mdef["slug"]
        market = resolved_markets.get(slug)
        if not market:
            continue

        cid = market["condition_id"]
        resolution = mdef["resolution"] or market.get("resolution")
        yes_tok = market.get("yes_token_id", "")
        no_tok  = market.get("no_token_id", "")
        market_trades = all_market_trades.get(cid, [])

        print(f"\n{'='*80}")
        print(f"Market: {mdef['name']}")
        print(f"  condition_id: {cid[:20]}...")
        print(f"  resolution: {resolution}  |  vol: ${market.get('volume_usdc', 0):,.0f}")
        print(f"  {len(market_trades)} trades in dataset (4000-trade cap applies)")
        print(f"{'='*80}")

        for insider_key in mdef["insider_keys"]:
            insider = KNOWN_INSIDERS[insider_key]
            print(f"\n  [{insider_key}] {insider.description}")

            wallet = insider.wallet
            if not wallet:
                print("  → No wallet address — cannot run Signal A (wallet not found in trade data)")
                summary_rows.append({
                    "insider": insider_key, "market": mdef["name"][:40],
                    "signal_a_fired": "N/A (no wallet)", "n_criteria": "N/A",
                    "entry_price": "N/A", "potential_return": "N/A",
                })
                continue

            wt = wallet_trades.get(wallet.lower(), [])

            if insider.expected_signal == "B":
                print(f"  → Expected Signal B (repeat predictor) — Signal A likely won't fire")
                print(f"     Running Signal A anyway for comparison:")

            result = evaluate_signal_a(
                insider=insider,
                wallet=wallet,
                market_id=cid,
                market_trades=market_trades,
                wallet_trades=wt,
                yes_token_id=yes_tok,
                no_token_id=no_tok,
                resolution=resolution,
                poly_client=poly_client,
            )

            print_signal_a_result(result, insider)

            summary_rows.append({
                "insider": insider_key,
                "market": mdef["name"][:40],
                "signal_a_fired": "YES" if result.signal_fired else "NO",
                "n_criteria": f"{result.n_passed}/5",
                "entry_price": f"${result.entry_price:.3f}" if result.entry_price else "N/A",
                "potential_return": f"{result.potential_return:+.1%}" if result.potential_return else "N/A",
            })

    # ── Step 5: Signal C on Venezuela ─────────────────────────────────────────
    venezuela_slug = "maduro-in-us-custody-by-january-31"
    if venezuela_slug in resolved_markets:
        market = resolved_markets[venezuela_slug]
        trades = all_market_trades.get(market["condition_id"], [])
        validate_signal_c_venezuela(
            condition_id=market["condition_id"],
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
            resolution="YES",
            market_trades=trades,
        )

    # ── Step 6: Summary table ─────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("VALIDATION SUMMARY")
    print("=" * 90)
    print(f"{'Insider':<28} {'Market':<38} {'Signal A':>8} {'Criteria':>9} {'Entry':>7} {'Return':>8}")
    print("-" * 90)
    for row in summary_rows:
        print(
            f"{row['insider']:<28} {row['market']:<38} "
            f"{row['signal_a_fired']:>8} {row['n_criteria']:>9} "
            f"{row['entry_price']:>7} {row['potential_return']:>8}"
        )

    print("\nKey questions answered:")
    fired = [r for r in summary_rows if r["signal_a_fired"] == "YES"]
    not_fired = [r for r in summary_rows if r["signal_a_fired"] == "NO"]
    na = [r for r in summary_rows if "N/A" in r["signal_a_fired"]]
    print(f"  Signal A fired on {len(fired)}/{len(summary_rows) - len(na)} validatable cases")
    if not_fired:
        print(f"  Missed cases: {', '.join(r['insider'] for r in not_fired)}")
        print("  → Check which criteria failed above to identify needed threshold adjustments")


if __name__ == "__main__":
    main()
