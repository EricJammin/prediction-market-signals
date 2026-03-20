"""
SQLite persistence layer for the Polymarket monitor.

Tables:
  hourly_volumes  — rolling per-market hourly USDC volume (7-day window)
  price_history   — latest YES/NO prices per market
  alerts_log      — fired alert history for deduplication
  poll_state      — per-market polling cursor and metadata
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import config


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_volumes (
    market_id   TEXT    NOT NULL,
    hour_ts     INTEGER NOT NULL,
    volume_usdc REAL    NOT NULL DEFAULT 0.0,
    PRIMARY KEY (market_id, hour_ts)
);

CREATE TABLE IF NOT EXISTS price_history (
    market_id   TEXT    PRIMARY KEY,
    yes_price   REAL,
    no_price    REAL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT    NOT NULL,
    hour_ts         INTEGER NOT NULL,
    surge_ratio     REAL    NOT NULL,
    signal_score    REAL    NOT NULL,
    tier            TEXT    NOT NULL,
    fired_at        INTEGER NOT NULL,
    telegram_ok     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_market_fired
    ON alerts_log (market_id, fired_at DESC);

CREATE TABLE IF NOT EXISTS wallet_positions (
    wallet           TEXT    NOT NULL,
    market_id        TEXT    NOT NULL,
    side             TEXT    NOT NULL,  -- YES or NO
    buy_usdc         REAL    NOT NULL DEFAULT 0.0,
    sell_usdc        REAL    NOT NULL DEFAULT 0.0,
    first_buy_price  REAL,
    first_trade_ts   INTEGER,
    signal_fired     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, market_id, side)
);
CREATE INDEX IF NOT EXISTS idx_wallet_positions_wallet
    ON wallet_positions (wallet);

CREATE TABLE IF NOT EXISTS poll_state (
    market_id        TEXT    PRIMARY KEY,
    last_trade_ts    INTEGER,
    question         TEXT,
    category         TEXT,
    resolution_date  TEXT,
    volume_usdc      REAL,
    slug             TEXT,
    yes_token_id     TEXT,
    no_token_id      TEXT,
    pizzint_relevant INTEGER NOT NULL DEFAULT 0,
    added_at         INTEGER NOT NULL
);
"""


class StateDB:
    def __init__(self, db_path: str = config.DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        """Create all tables if they do not exist. Safe to call on every startup."""
        self._conn.executescript(_SCHEMA)
        # Migrations for columns added after initial deployment
        for sql in [
            "ALTER TABLE poll_state ADD COLUMN pizzint_relevant INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                self._conn.execute(sql)
                self._conn.commit()
            except Exception:
                pass  # column already exists

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── hourly_volumes ─────────────────────────────────────────────────────────

    def upsert_hourly_volume(self, market_id: str, hour_ts: int, volume_delta: float) -> None:
        """Add volume_delta to (market_id, hour_ts) bucket, creating the row if needed."""
        self._conn.execute(
            """
            INSERT INTO hourly_volumes (market_id, hour_ts, volume_usdc)
            VALUES (?, ?, ?)
            ON CONFLICT(market_id, hour_ts)
            DO UPDATE SET volume_usdc = volume_usdc + excluded.volume_usdc
            """,
            (market_id, hour_ts, volume_delta),
        )
        self._conn.commit()

    def get_hourly_volumes(self, market_id: str, since_ts: int) -> list[tuple[int, float]]:
        """Return [(hour_ts, volume_usdc), ...] ASC for one market since since_ts."""
        rows = self._conn.execute(
            "SELECT hour_ts, volume_usdc FROM hourly_volumes "
            "WHERE market_id = ? AND hour_ts >= ? ORDER BY hour_ts ASC",
            (market_id, since_ts),
        ).fetchall()
        return [(r["hour_ts"], r["volume_usdc"]) for r in rows]

    def prune_old_volumes(self, cutoff_ts: int) -> int:
        """Delete hourly_volumes older than cutoff_ts. Returns rows deleted."""
        cur = self._conn.execute(
            "DELETE FROM hourly_volumes WHERE hour_ts < ?", (cutoff_ts,)
        )
        self._conn.commit()
        return cur.rowcount

    # ── price_history ──────────────────────────────────────────────────────────

    def upsert_price(
        self,
        market_id: str,
        yes_price: float | None,
        no_price: float | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO price_history (market_id, yes_price, no_price, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                yes_price  = excluded.yes_price,
                no_price   = excluded.no_price,
                updated_at = excluded.updated_at
            """,
            (market_id, yes_price, no_price, int(time.time())),
        )
        self._conn.commit()

    def get_price(self, market_id: str) -> tuple[float | None, float | None]:
        """Returns (yes_price, no_price). Both None if market not in table."""
        row = self._conn.execute(
            "SELECT yes_price, no_price FROM price_history WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        if row is None:
            return None, None
        return row["yes_price"], row["no_price"]

    # ── alerts_log ─────────────────────────────────────────────────────────────

    def last_alert_at(self, market_id: str) -> int | None:
        """Unix timestamp of the most recent alert for this market, or None."""
        row = self._conn.execute(
            "SELECT MAX(fired_at) AS t FROM alerts_log WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def record_alert(
        self,
        market_id: str,
        tier: str,
        composite_score: float,
        surge_ratio: float,
    ) -> None:
        """Record a Signal C composite alert for cooldown tracking."""
        self._conn.execute(
            """
            INSERT INTO alerts_log
                (market_id, hour_ts, surge_ratio, signal_score, tier, fired_at, telegram_ok)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (market_id, 0, surge_ratio, composite_score, tier, int(time.time()), 0),
        )
        self._conn.commit()

    # ── poll_state ─────────────────────────────────────────────────────────────

    def get_last_trade_ts(self, market_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT last_trade_ts FROM poll_state WHERE market_id = ?", (market_id,)
        ).fetchone()
        return row["last_trade_ts"] if row else None

    def set_last_trade_ts(self, market_id: str, ts: int) -> None:
        self._conn.execute(
            "UPDATE poll_state SET last_trade_ts = ? WHERE market_id = ?", (ts, market_id)
        )
        self._conn.commit()

    def upsert_market_meta(
        self,
        market_id: str,
        question: str,
        category: str,
        resolution_date: str,
        volume_usdc: float,
        slug: str,
        yes_token_id: str = "",
        no_token_id: str = "",
        pizzint_relevant: bool = False,
    ) -> None:
        """Insert or update market metadata. Sets added_at only on first insert."""
        self._conn.execute(
            """
            INSERT INTO poll_state
                (market_id, question, category, resolution_date, volume_usdc,
                 slug, yes_token_id, no_token_id, pizzint_relevant, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                question         = excluded.question,
                category         = excluded.category,
                resolution_date  = excluded.resolution_date,
                volume_usdc      = excluded.volume_usdc,
                slug             = excluded.slug,
                yes_token_id     = excluded.yes_token_id,
                no_token_id      = excluded.no_token_id,
                pizzint_relevant = excluded.pizzint_relevant
            """,
            (
                market_id, question, category, resolution_date, volume_usdc,
                slug, yes_token_id, no_token_id, int(pizzint_relevant), int(time.time()),
            ),
        )
        self._conn.commit()

    def get_recent_alerts(self, since_ts: int) -> list[dict]:
        """Return alerts_log rows fired since since_ts, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM alerts_log WHERE fired_at >= ? ORDER BY fired_at DESC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_market_meta(self, market_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM poll_state WHERE market_id = ?", (market_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_watched_markets(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM poll_state").fetchall()
        return [dict(r) for r in rows]

    # ── wallet_positions (Signal A state) ──────────────────────────────────────

    def update_wallet_position(
        self,
        wallet: str,
        market_id: str,
        side: str,
        buy_delta: float,
        sell_delta: float,
        first_buy_price: float | None,
        first_trade_ts: int | None,
    ) -> None:
        """
        Create or update a wallet's cumulative position in (market, side).
        first_buy_price and first_trade_ts are only stored on the first BUY
        (COALESCE keeps the original value on subsequent updates).
        """
        self._conn.execute(
            """
            INSERT INTO wallet_positions
                (wallet, market_id, side, buy_usdc, sell_usdc, first_buy_price, first_trade_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, market_id, side) DO UPDATE SET
                buy_usdc        = buy_usdc  + excluded.buy_usdc,
                sell_usdc       = sell_usdc + excluded.sell_usdc,
                first_buy_price = COALESCE(wallet_positions.first_buy_price,
                                           excluded.first_buy_price),
                first_trade_ts  = COALESCE(wallet_positions.first_trade_ts,
                                           excluded.first_trade_ts)
            """,
            (wallet, market_id, side,
             buy_delta, sell_delta, first_buy_price, first_trade_ts),
        )
        self._conn.commit()

    def get_wallet_position(
        self, wallet: str, market_id: str, side: str
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM wallet_positions WHERE wallet = ? AND market_id = ? AND side = ?",
            (wallet, market_id, side),
        ).fetchone()
        return dict(row) if row else None

    def get_wallet_total_buy_usdc(self, wallet: str) -> float:
        """Sum of buy_usdc across all (market, side) pairs for this wallet."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(buy_usdc), 0.0) AS total FROM wallet_positions "
            "WHERE wallet = ?",
            (wallet,),
        ).fetchone()
        return float(row["total"]) if row else 0.0

    def was_signal_a_fired(self, wallet: str, market_id: str, side: str) -> bool:
        row = self._conn.execute(
            "SELECT signal_fired FROM wallet_positions "
            "WHERE wallet = ? AND market_id = ? AND side = ?",
            (wallet, market_id, side),
        ).fetchone()
        return bool(row and row["signal_fired"])

    def mark_wallet_signal_a_fired(
        self, wallet: str, market_id: str, side: str
    ) -> None:
        self._conn.execute(
            "UPDATE wallet_positions SET signal_fired = 1 "
            "WHERE wallet = ? AND market_id = ? AND side = ?",
            (wallet, market_id, side),
        )
        self._conn.commit()
