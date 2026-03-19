"""
DataCollector: fetches and caches market metadata and trade history.

Data sources:
  - Gamma API (https://gamma-api.polymarket.com): market metadata, resolution
  - CLOB API  (https://clob.polymarket.com):      individual trade fills (authenticated)

Authentication:
  The CLOB /trades endpoint requires L2 API credentials stored in .env:
    POLY_ADDRESS, POLY_API_KEY, POLY_SECRET, POLY_PASSPHRASE

  Each request is signed with HMAC-SHA256. See _clob_auth_headers().

Raw JSON is cached to data/raw_markets/ and data/raw_trades/ so re-runs
don't hit the API again. Pass force_refresh=True to re-fetch.

Normalized output is a pandas DataFrame with standardised column names.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

load_dotenv()

logger = logging.getLogger(__name__)

# ── CLOB credentials (loaded once at import time) ─────────────────────────────
_POLY_ADDRESS    = os.getenv("POLY_ADDRESS", "")
_POLY_API_KEY    = os.getenv("POLY_API_KEY", "")
_POLY_SECRET     = os.getenv("POLY_SECRET", "")
_POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

_CLOB_AUTH_AVAILABLE = all([_POLY_ADDRESS, _POLY_API_KEY, _POLY_SECRET, _POLY_PASSPHRASE])


def _clob_auth_headers(method: str, path: str, body: str = "") -> dict:
    """
    Generate HMAC-SHA256 signed L2 headers for the Polymarket CLOB API.

    Signing scheme (from py-clob-client source):
      key       = urlsafe_b64decode(secret)
      message   = timestamp + METHOD + path + body
      signature = urlsafe_b64encode( HMAC-SHA256(key, message) )

    Headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE
    """
    timestamp = str(int(time.time()))
    message = timestamp + method.upper() + path
    if body:
        message += body.replace("'", '"')

    secret_padded = _POLY_SECRET + "=" * (-len(_POLY_SECRET) % 4)
    secret_bytes = base64.urlsafe_b64decode(secret_padded)

    sig_bytes = hmac.new(secret_bytes, message.encode("utf-8"), digestmod=hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(sig_bytes).decode("utf-8")

    return {
        "POLY_ADDRESS":    _POLY_ADDRESS,
        "POLY_SIGNATURE":  signature,
        "POLY_TIMESTAMP":  timestamp,
        "POLY_API_KEY":    _POLY_API_KEY,
        "POLY_PASSPHRASE": _POLY_PASSPHRASE,
    }


def _make_session() -> requests.Session:
    """Build a requests Session with automatic retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=config.RETRY_BACKOFF_SECONDS,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class DataCollector:
    def __init__(self, force_refresh: bool = False) -> None:
        self.force_refresh = force_refresh
        self.session = _make_session()
        Path(config.RAW_TRADES_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.RAW_MARKETS_DIR).mkdir(parents=True, exist_ok=True)

        if not _CLOB_AUTH_AVAILABLE:
            logger.warning(
                "CLOB API credentials not found in .env — trade fetching will fail. "
                "Copy .env.example to .env and fill in your Polymarket Builder API key."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_market(self, market: dict) -> dict:
        """
        Resolve a market entry (slug or condition_id) to full metadata + trades.

        Returns the enriched market dict (with condition_id populated if it was
        None, plus resolved YES/NO token IDs).

        If condition_id is already provided, it is trusted directly. Metadata
        lookup is still attempted to fill in token IDs and resolution, but
        a lookup failure does not block trade fetching.
        """
        metadata = self._fetch_metadata(market)
        if metadata is None:
            # Metadata lookup failed — if we have a hardcoded condition_id, proceed
            if market.get("condition_id"):
                logger.warning(
                    "Metadata lookup failed for %s — using hardcoded condition_id",
                    market.get("slug") or market.get("condition_id"),
                )
                metadata = {"condition_id": market["condition_id"]}
            else:
                logger.warning("Could not resolve market: %s", market.get("slug"))
                return market

        # Trust hardcoded condition_id over resolved one to avoid Gamma API quirks
        condition_id = market.get("condition_id") or metadata["condition_id"]
        market["condition_id"] = condition_id
        metadata["condition_id"] = condition_id

        # Cache metadata
        meta_path = Path(config.RAW_MARKETS_DIR) / f"{condition_id}.json"
        if not meta_path.exists() or self.force_refresh:
            meta_path.write_text(json.dumps(metadata, indent=2))
            logger.info("Saved metadata → %s", meta_path)

        # Fetch trades
        trades_path = Path(config.RAW_TRADES_DIR) / f"{condition_id}.json"
        if not trades_path.exists() or self.force_refresh:
            trades = self._fetch_trades(condition_id)
            trades_path.write_text(json.dumps(trades, indent=2))
            logger.info("Saved %d trades → %s", len(trades), trades_path)
        else:
            logger.info("Using cached trades for %s", condition_id)

        return market

    def load_all_data(self, markets: list[dict]) -> tuple[pd.DataFrame, dict[str, dict]]:
        """
        Load cached market metadata and trades for every market in the list.

        Returns:
            all_trades_df: DataFrame of all trades across all markets
            metadata_map:  dict condition_id → metadata dict
        """
        all_trades: list[pd.DataFrame] = []
        metadata_map: dict[str, dict] = {}

        for market in markets:
            condition_id = market.get("condition_id")
            if not condition_id:
                logger.warning("Skipping market with no condition_id: %s", market.get("slug"))
                continue

            meta = self._load_cached_metadata(condition_id)
            if meta:
                metadata_map[condition_id] = meta

            trades_df = self._load_cached_trades(condition_id, meta)
            if trades_df is not None and not trades_df.empty:
                all_trades.append(trades_df)

        if not all_trades:
            return pd.DataFrame(), metadata_map

        combined = pd.concat(all_trades, ignore_index=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        return combined, metadata_map

    def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search the Gamma API for markets matching a keyword query."""
        url = f"{config.GAMMA_API_BASE}/markets"
        params = {"search": query, "limit": limit}
        resp = self._get(url, params)
        if resp is None:
            return []
        results = resp if isinstance(resp, list) else resp.get("markets", [])
        return [self._normalize_gamma_market(m) for m in results]

    # ── Gamma API ─────────────────────────────────────────────────────────────

    def _fetch_metadata(self, market: dict) -> dict | None:
        """Try to resolve metadata by condition_id first, then by slug."""
        condition_id = market.get("condition_id")
        slug = market.get("slug")

        if condition_id:
            meta = self._gamma_by_condition_id(condition_id)
            if meta:
                return meta

        if slug:
            meta = self._gamma_by_slug(slug)
            if meta:
                return meta

        desc = market.get("description") or slug or ""
        if desc:
            results = self.search_markets(desc, limit=5)
            if results:
                logger.info("Resolved '%s' via search → %s", desc, results[0]["condition_id"])
                return results[0]

        return None

    def _gamma_by_condition_id(self, condition_id: str) -> dict | None:
        # Gamma API doesn't reliably filter by condition_id — validate the response
        url = f"{config.GAMMA_API_BASE}/markets"
        resp = self._get(url, {"condition_id": condition_id})
        markets = self._extract_list(resp)
        for m in markets:
            if m.get("conditionId") == condition_id or m.get("condition_id") == condition_id:
                return self._normalize_gamma_market(m)
        return None

    def _gamma_by_slug(self, slug: str) -> dict | None:
        url = f"{config.GAMMA_API_BASE}/markets"
        resp = self._get(url, {"slug": slug})
        markets = self._extract_list(resp)
        for m in markets:
            if m.get("slug") == slug:
                return self._normalize_gamma_market(m)
        return None

    def _normalize_gamma_market(self, raw: dict) -> dict:
        """Flatten a Gamma API market object into a consistent internal dict."""
        # clobTokenIds is a JSON-encoded string in the Gamma API response
        tokens_raw = raw.get("clobTokenIds") or raw.get("tokens") or "[]"
        try:
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            tokens = []

        yes_token_id = tokens[0] if len(tokens) > 0 else ""
        no_token_id  = tokens[1] if len(tokens) > 1 else ""

        # Resolution: outcomePrices is also a JSON-encoded string
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

        resolved = bool(raw.get("closed") and resolution is not None)

        return {
            "condition_id":   raw.get("conditionId") or raw.get("condition_id") or "",
            "slug":           raw.get("slug") or "",
            "question":       raw.get("question") or raw.get("title") or "",
            "category":       raw.get("category") or "",
            "creation_date":  raw.get("startDateIso") or raw.get("createdAt") or "",
            "resolution_date": raw.get("endDateIso") or raw.get("endDate") or "",
            "resolved":       resolved,
            "resolution":     resolution,
            "yes_token_id":   yes_token_id,
            "no_token_id":    no_token_id,
            "volume_usdc":    _safe_float(raw.get("volume") or raw.get("volumeNum") or 0),
        }

    # ── CLOB API ──────────────────────────────────────────────────────────────

    def _fetch_trades(self, condition_id: str) -> list[dict]:
        """
        Fetch all available trades for a market from data-api.polymarket.com.

        The data-api is public (no auth). It returns up to 1000 trades per
        request with offset-based pagination, max offset = 3000. We sweep
        all four pages to get up to ~4000 trades total per market.

        Pages: offset=0, 1000, 2000, 3000 → merged and deduplicated.
        """
        url = f"{config.DATA_API_BASE}/trades"
        page_size = 1000
        max_offset = 3000
        seen_hashes: set[str] = set()
        all_trades: list[dict] = []

        offset = 0
        while offset <= max_offset:
            params = {"market": condition_id, "limit": page_size, "offset": offset}
            resp = self._get(url, params)

            if resp is None or not isinstance(resp, list) or not resp:
                break

            new_count = 0
            for t in resp:
                h = t.get("transaction_hash") or t.get("transactionHash") or ""
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_trades.append(t)
                    new_count += 1

            logger.info("offset=%d: %d trades (%d new) for %s",
                        offset, len(resp), new_count, condition_id[:16])

            # Stop early if page wasn't full — no more data exists
            if len(resp) < page_size:
                break

            offset += page_size
            time.sleep(config.REQUEST_DELAY_SECONDS)

        logger.info("Fetched %d total unique trades for %s", len(all_trades), condition_id)
        return all_trades

    # ── Loading cached data ───────────────────────────────────────────────────

    def _load_cached_metadata(self, condition_id: str) -> dict | None:
        path = Path(config.RAW_MARKETS_DIR) / f"{condition_id}.json"
        if not path.exists():
            logger.warning("No cached metadata for %s", condition_id)
            return None
        return json.loads(path.read_text())

    def _load_cached_trades(self, condition_id: str, metadata: dict | None) -> pd.DataFrame | None:
        path = Path(config.RAW_TRADES_DIR) / f"{condition_id}.json"
        if not path.exists():
            logger.warning("No cached trades for %s", condition_id)
            return None

        raw = json.loads(path.read_text())
        if not raw:
            return pd.DataFrame()

        return self._normalize_trades(raw, condition_id, metadata)

    def _normalize_trades(
        self,
        raw_trades: list[dict],
        condition_id: str,
        metadata: dict | None,
    ) -> pd.DataFrame:
        """
        Normalize raw CLOB trade records to a consistent DataFrame.

        data-api field reference:
          proxyWallet    – wallet address (taker)
          side           – "BUY" / "SELL" from taker perspective (trade direction)
          asset          – token ID (YES or NO share)
          outcome        – "Yes" / "No" text (which token)
          price          – fill price (0–1)
          size           – USDC size
          timestamp      – unix seconds
          transactionHash – on-chain tx

        Output columns:
          side      – YES or NO (which token was traded; derived from outcome/asset)
          direction – BUY or SELL (taker's direction; preserved from raw side field)

        We emit one row per unique (transaction_hash, wallet).
        """
        yes_token_id = (metadata or {}).get("yes_token_id") or ""
        no_token_id  = (metadata or {}).get("no_token_id") or ""

        rows = []
        seen: set[tuple[str, str]] = set()

        for t in raw_trades:
            tx_hash   = t.get("transaction_hash") or t.get("transactionHash") or ""
            timestamp = _parse_timestamp(t)
            price     = _safe_float(t.get("price"))
            size      = _safe_float(
                t.get("size") or t.get("matched_amount") or t.get("amount")
            )

            if size < config.MIN_TRADE_SIZE_USDC:
                continue

            asset_id      = t.get("asset_id") or t.get("assetId") or t.get("asset") or ""
            outcome_field = t.get("outcome") or ""
            side = _resolve_side(asset_id, outcome_field, yes_token_id, no_token_id)

            # Preserve BUY/SELL direction separately from YES/NO token side
            raw_direction = (t.get("side") or "").upper()
            direction = raw_direction if raw_direction in ("BUY", "SELL") else None

            # data-api uses proxyWallet; CLOB uses taker_address / maker_address
            wallet = (
                t.get("proxyWallet")
                or t.get("taker_address")
                or t.get("maker_address")
                or t.get("owner")
                or ""
            ).lower()

            if not wallet or not tx_hash or timestamp is None or side is None:
                continue

            key = (tx_hash, wallet)
            if key in seen:
                continue
            seen.add(key)

            rows.append({
                "market_id":        condition_id,
                "timestamp":        int(timestamp),
                "wallet":           wallet,
                "side":             side,        # YES / NO  (which token)
                "direction":        direction,   # BUY / SELL (taker direction)
                "price":            price,
                "size_usdc":        size,
                "transaction_hash": tx_hash,
            })

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(
        self,
        url: str,
        params: dict,
        extra_headers: dict | None = None,
    ) -> dict | list | None:
        headers = {"User-Agent": "Mozilla/5.0"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error("HTTP %s fetching %s: %s", exc.response.status_code, url, exc)
            if exc.response.status_code == 401:
                logger.error("Auth failed — check POLY_ADDRESS / POLY_SECRET / POLY_PASSPHRASE in .env")
        except requests.RequestException as exc:
            logger.error("Request error fetching %s: %s", url, exc)
        except json.JSONDecodeError as exc:
            logger.error("JSON decode error from %s: %s", url, exc)
        return None

    @staticmethod
    def _extract_list(resp: dict | list | None) -> list:
        if resp is None:
            return []
        if isinstance(resp, list):
            return resp
        for key in ("markets", "data", "results"):
            if key in resp:
                return resp[key]
        return []


# ── Utility functions ─────────────────────────────────────────────────────────

def _parse_timestamp(trade: dict) -> float | None:
    for field in ("timestamp", "match_time", "matchTime", "created_at", "createdAt"):
        val = trade.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _resolve_side(
    asset_id: str,
    outcome_field: str,
    yes_token_id: str,
    no_token_id: str,
) -> str | None:
    if yes_token_id and asset_id == yes_token_id:
        return "YES"
    if no_token_id and asset_id == no_token_id:
        return "NO"
    upper = outcome_field.strip().upper()
    if upper in ("YES", "TRUE"):
        return "YES"
    if upper in ("NO", "FALSE"):
        return "NO"
    return None
