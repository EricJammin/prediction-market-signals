"""
MarketWatchlist: discovers and maintains the list of active markets to monitor.

Sources:
  1. Seed markets — hardcoded list of known condition_ids (our Iran/Venezuela set)
  2. Dynamic discovery — Gamma API active market scan, filtered by category and volume

The watchlist is refreshed from the Gamma API once per hour (not every poll).
All market metadata is stored in poll_state via StateDB.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from state import StateDB

logger = logging.getLogger(__name__)

# ── Seed markets ───────────────────────────────────────────────────────────────
# Always monitored regardless of volume filter or category.
# Fields:
#   condition_id     — Polymarket condition ID (authoritative)
#   slug             — Gamma API slug for metadata enrichment
#   category         — "military" | "geopolitics" | "policy" | "election"
#   pizzint_relevant — True only for US military action markets (PizzINT only
#                      tracks US force posture; irrelevant for non-US conflicts)
#   keywords         — search terms for news cross-reference (Signal C filter)
SEED_MARKETS = [
    # ── Iran / US military operations ──────────────────────────────────────
    {
        "condition_id": "0x61ce3773237a948584e422de72265f937034af418a8b703e3a860ea62e59ff36",
        "slug": "will-the-iranian-regime-fall-by-march-31",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran regime fall", "Iran government collapse", "Iranian regime"],
    },
    {
        "condition_id": "0xe443dab97ad8b7f58558cc7a6a3932d156031e962a451e0461e9a4578d78fe84",
        "slug": "will-the-iranian-regime-fall-by-april-30",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran regime fall", "Iran government collapse", "Iranian regime"],
    },
    {
        "condition_id": "0x24026080b17f4e88729eab0ac2929ee37c13bfbb4a159179ec63deb4a242d9c9",
        "slug": "will-france-uk-or-germany-strike-iran-by-march-31",
        "category": "military",
        "pizzint_relevant": False,  # European military action, not US-specific
        "keywords": ["France strike Iran", "UK strike Iran", "Germany strike Iran", "Europe Iran military"],
    },
    {
        "condition_id": "0x5db999fad322cea2914535aae5517060c3f80ad6d8c0231cde2124a434d16846",
        "slug": "will-the-us-invade-iran-before-2027",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["US invade Iran", "US Iran invasion", "US Iran ground troops"],
    },
    {
        "condition_id": "0x773abaa5fe55e5cde51a261f444b7921652a4e059ead6b3be9fe56499c2d4609",
        "slug": "us-x-iran-ceasefire-by-april-15",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["US Iran ceasefire", "Iran ceasefire", "Iran peace deal"],
    },
    {
        "condition_id": "0xa70fc3695a65833b91b45df6db6015096f3e1471b70352ca411b4209010e7633",
        "slug": "us-iran-nuclear-deal-by-june-30",
        "category": "policy",
        "pizzint_relevant": False,
        "keywords": ["US Iran nuclear deal", "Iran nuclear agreement", "Iran JCPOA"],
    },
    {
        "condition_id": "0xd73f60114a0e7169a55082daef1228cb27fa50c939eea22cb0589f6bac6ce5d3",
        "slug": "iran-x-israelus-conflict-ends-by-may-15",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran US conflict", "Iran Israel conflict", "Iran war ends", "Iran ceasefire"],
    },
    {
        "condition_id": "0x5c2e6aef8af5931e9bfa3750364626d754531d2fada2885d45c356b175962a25",
        "slug": "iran-x-israelus-conflict-ends-by-april-15",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran US conflict", "Iran Israel conflict", "Iran war ends", "Iran ceasefire"],
    },
    {
        "condition_id": "0x136f5a0c27a62cf9a2e40a4f48425e43d61b9571a53a2529372c0065f3218a73",
        "slug": "iran-x-israelus-conflict-ends-by-june-30",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran US conflict", "Iran Israel conflict", "Iran war ends"],
    },
    {
        "condition_id": "0xfa59099fbda1e0f0058ed3cbd57e939fe90ab6d9b57d53bd488bcadf75c191d4",
        "slug": "trump-announces-end-of-military-operations-against-iran",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Trump Iran military end", "US Iran operations end", "Iran war end"],
    },
    {
        "condition_id": "0xbeb97cb6d42528a620b66e829b00d6d9e609a34665c109b5c95d581f21b5392f",
        "slug": "us-israel-strike-fordow-nuclear-facility-by-march-31",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Fordow nuclear strike", "Iran nuclear facility strike", "Fordow bomb"],
    },
    {
        "condition_id": "0xefc69f5f48827e331957acbcc2339eb3b15e27e32453b8e6f29b5de67474c986",
        "slug": "will-the-iranian-regime-survive-us-military-strikes",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["Iran regime survive", "Iranian government survive", "Iran military strikes"],
    },
    # ── Iran leadership succession ──────────────────────────────────────────
    {
        "condition_id": "0xd25b2fc3a916b317d71b398c5d0f81ad33fe1da6b56d6c9f717332d31584504e",
        "slug": "will-reza-pahlavi-enter-iran-by-march-31",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Reza Pahlavi Iran", "Pahlavi return Iran", "Iran new leader"],
    },
    {
        "condition_id": "0xb412664463bbfe21be44b1963291205ab332afd4f7f6e0d027aec1ba7a9e6793",
        "slug": "iran-leadership-change-by-april-30",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Iran leadership change", "Iran new government", "Iran regime change"],
    },
    {
        "condition_id": "0x25fb28382075f418a944a781a9f8840e2f541152eea0d9798d1cabfa1466adbb",
        "slug": "will-mojtaba-khamenei-be-head-of-state-in-iran-end-of-2026",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Mojtaba Khamenei", "Iran supreme leader", "Iran head of state"],
    },
    # ── China / Taiwan ──────────────────────────────────────────────────────
    {
        "condition_id": "0x2701e5a5b751418c5c5bf0faaafdea60ac9fc893eb75fd88e902cd97458d375b",
        "slug": "will-china-invade-taiwan-by-march-31-2026",
        "category": "military",
        "pizzint_relevant": False,  # Chinese military action, not US
        "keywords": ["China invade Taiwan", "Taiwan invasion", "China Taiwan military"],
    },
    {
        "condition_id": "0xb215decbedd846168842f6e207f09bd5f50ce51d2191f238887d976ec21b6f66",
        "slug": "will-china-blockade-taiwan-by-june-30",
        "category": "military",
        "pizzint_relevant": False,
        "keywords": ["China Taiwan blockade", "Taiwan blockade", "China Taiwan conflict"],
    },
    # ── Israel / Lebanon / Hamas ────────────────────────────────────────────
    {
        "condition_id": "0x24fb7c2d95c93a68018e6c4a90d88043bb67d32fd1454924cef8ebdd550228f3",
        "slug": "will-israel-launch-a-major-ground-offensive-in-lebanon-by-march-31",
        "category": "military",
        "pizzint_relevant": False,
        "keywords": ["Israel Lebanon offensive", "Israel Lebanon ground", "IDF Lebanon"],
    },
    {
        "condition_id": "0xcc8881721c1d263ee34fbe821d6b2611bb99c6b04a348469ac3353a200921418",
        "slug": "israel-x-hamas-ceasefire-phase-ii-by-march-31",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Israel Hamas ceasefire", "Gaza ceasefire phase 2", "Hamas deal"],
    },
    # ── Russia / Ukraine / NATO ─────────────────────────────────────────────
    {
        "condition_id": "0x1d54eb5eac2cee8f595f3097c65da7d07f8ab5dee63d7c0c6883eb70e1e9af30",
        "slug": "russia-x-ukraine-ceasefire-by-march-31-2026",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Russia Ukraine ceasefire", "Ukraine peace deal", "Russia Ukraine peace"],
    },
    {
        "condition_id": "0x7434b22007745d99095c102119fdb6b975d34869212e9dda4c6c5c48db0683a7",
        "slug": "russia-x-ukraine-ceasefire-by-april-30-2026",
        "category": "geopolitics",
        "pizzint_relevant": False,
        "keywords": ["Russia Ukraine ceasefire", "Ukraine peace deal", "Russia Ukraine peace"],
    },
    {
        "condition_id": "0xd3320fd4b325ce3d629fbaffd90bebd0a4f45042d91bf207c9866704d630bdb9",
        "slug": "nato-x-russia-military-clash-by-march-31-2026",
        "category": "military",
        "pizzint_relevant": False,
        "keywords": ["NATO Russia clash", "NATO Russia conflict", "Russia NATO military"],
    },
    # ── US military (other) ─────────────────────────────────────────────────
    {
        "condition_id": "0x3de0f3d7d7efb40cde68e814d40a0b232832083653c8e78260eb999baa967de0",
        "slug": "us-strike-on-cuba-by-december-31",
        "category": "military",
        "pizzint_relevant": True,
        "keywords": ["US strike Cuba", "US Cuba military", "Cuba bombing"],
    },
]


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=config.RETRY_BACKOFF_SECONDS,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _normalize_gamma_market(raw: dict) -> dict:
    """Flatten a Gamma API market response into a consistent internal dict."""
    tokens_raw = raw.get("clobTokenIds") or raw.get("tokens") or "[]"
    try:
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except (json.JSONDecodeError, TypeError):
        tokens = []

    prices_raw = raw.get("outcomePrices") or "[]"
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except (json.JSONDecodeError, TypeError):
        prices = []

    if prices and prices[0] == "1":
        resolution = "YES"
    elif len(prices) > 1 and prices[1] == "1":
        resolution = "NO"
    else:
        resolution = None

    return {
        "condition_id":   raw.get("conditionId") or raw.get("condition_id") or "",
        "slug":           raw.get("slug") or "",
        "question":       raw.get("question") or raw.get("title") or "",
        "category":       raw.get("category") or "",
        "resolution_date": raw.get("endDateIso") or raw.get("endDate") or "",
        "resolved":       bool(raw.get("closed") and resolution is not None),
        "resolution":     resolution,
        "yes_token_id":   tokens[0] if len(tokens) > 0 else "",
        "no_token_id":    tokens[1] if len(tokens) > 1 else "",
        "volume_usdc":    _safe_float(raw.get("volume") or raw.get("volumeNum") or 0),
    }


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


class MarketWatchlist:
    def __init__(self, db: StateDB) -> None:
        self._db = db
        self._session = _make_session()
        self._last_refresh_ts: float = 0.0

    def refresh(self, force: bool = False) -> list[dict]:
        """
        Fetch active markets from Gamma API + seed list, upsert into DB.
        Rate-limited to once per WATCHLIST_REFRESH_SECS unless force=True.
        Returns the current watchlist as a list of market dicts.
        """
        now = time.time()
        if not force and (now - self._last_refresh_ts) < config.WATCHLIST_REFRESH_SECS:
            return self.get_active()

        logger.info("Refreshing market watchlist...")
        discovered = self._discover_from_gamma()
        seeded = self._load_seeds()

        # Merge: seed markets take priority (they may not appear in Gamma results)
        all_markets: dict[str, dict] = {}
        for m in discovered:
            if m["condition_id"]:
                all_markets[m["condition_id"]] = m
        for m in seeded:
            if m["condition_id"]:
                all_markets[m["condition_id"]] = m  # seed overwrites if present

        for market in all_markets.values():
            if not market.get("resolved"):
                self._db.upsert_market_meta(
                    market_id=market["condition_id"],
                    question=market.get("question", ""),
                    category=market.get("category", ""),
                    resolution_date=market.get("resolution_date", ""),
                    volume_usdc=market.get("volume_usdc", 0.0),
                    slug=market.get("slug", ""),
                    yes_token_id=market.get("yes_token_id", ""),
                    no_token_id=market.get("no_token_id", ""),
                    pizzint_relevant=market.get("pizzint_relevant", False),
                )

        self._last_refresh_ts = now
        active = self.get_active()
        logger.info("Watchlist: %d active markets", len(active))
        return active

    def get_active(self) -> list[dict]:
        """Return current watchlist from DB without hitting the API."""
        return self._db.get_all_watched_markets()

    # ── Private ────────────────────────────────────────────────────────────────

    def _discover_from_gamma(self) -> list[dict]:
        """Fetch all active unresolved markets from Gamma API, filtered by category/volume."""
        markets: list[dict] = []
        page_size = 100
        offset = 0

        while True:
            try:
                resp = self._session.get(
                    f"{config.GAMMA_API_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": page_size,
                        "offset": offset,
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=30,
                )
                resp.raise_for_status()
                raw_list = resp.json()
            except Exception as exc:
                logger.warning("Gamma API error at offset %d: %s", offset, exc)
                break

            if not isinstance(raw_list, list) or not raw_list:
                break

            for raw in raw_list:
                m = _normalize_gamma_market(raw)
                if m["resolved"]:
                    continue
                cat = m["category"].lower()
                if not any(c in cat for c in config.GAMMA_WATCHLIST_CATEGORIES):
                    continue
                if m["volume_usdc"] < config.MIN_MARKET_VOLUME_USDC:
                    continue
                if not m["condition_id"]:
                    continue
                markets.append(m)

            if len(raw_list) < page_size:
                break  # last page
            offset += page_size
            time.sleep(config.REQUEST_DELAY_SECONDS)

        logger.info("Gamma API discovery: %d qualifying markets", len(markets))
        return markets

    def _load_seeds(self) -> list[dict]:
        """
        Attempt to resolve seed markets from Gamma API by slug.
        Falls back to minimal metadata if API lookup fails.
        """
        resolved: list[dict] = []
        for seed in SEED_MARKETS:
            try:
                resp = self._session.get(
                    f"{config.GAMMA_API_BASE}/markets",
                    params={"slug": seed["slug"]},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=15,
                )
                resp.raise_for_status()
                raw_list = resp.json()
                if isinstance(raw_list, list):
                    for raw in raw_list:
                        if raw.get("slug") == seed["slug"]:
                            m = _normalize_gamma_market(raw)
                            # Trust hardcoded fields over API response
                            m["condition_id"] = seed["condition_id"]
                            m["keywords"] = seed.get("keywords", [])
                            m["category"] = seed.get("category", m.get("category", "geopolitics"))
                            m["pizzint_relevant"] = seed.get("pizzint_relevant", False)
                            resolved.append(m)
                            break
                    else:
                        resolved.append(self._minimal_seed(seed))
                else:
                    resolved.append(self._minimal_seed(seed))
                time.sleep(config.REQUEST_DELAY_SECONDS)
            except Exception as exc:
                logger.warning("Failed to resolve seed %s: %s", seed["slug"], exc)
                resolved.append(self._minimal_seed(seed))

        return resolved

    @staticmethod
    def _minimal_seed(seed: dict) -> dict:
        """Return a minimal market dict for a seed when API lookup fails."""
        return {
            "condition_id":    seed["condition_id"],
            "slug":            seed["slug"],
            "question":        seed["slug"].replace("-", " ").title(),
            "category":        seed.get("category", "geopolitics"),
            "resolution_date": "",
            "resolved":        False,
            "resolution":      None,
            "yes_token_id":    "",
            "no_token_id":     "",
            "volume_usdc":     0.0,
            "keywords":        seed.get("keywords", []),
            "pizzint_relevant": seed.get("pizzint_relevant", False),
        }
