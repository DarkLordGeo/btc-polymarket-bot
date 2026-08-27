"""
SQLite persistence — research-grade version.

Three tables matter for analysis:

  decisions        One row per strategy per decision tick (every ~3s, for
                    every strategy, whether or not it traded). This is the
                    full observation + prediction snapshot: enough to
                    reconstruct "why did strategy X do Y at time T".

  trades            One row per settled paper trade (a subset of decisions
                    that actually opened a position and later resolved).

  market_outcomes   One row per OBSERVED market (whether or not any strategy
                    traded it), recording how it actually resolved. This is
                    what makes calibration analysis on the full decision log
                    possible — the original version of this file only ever
                    learned the outcome of markets that had an open paper
                    position, silently discarding ground truth for every
                    market a strategy declined to trade.

Schema changes here are additive: `_migrate()` adds any missing column to an
existing on-disk DB rather than requiring you to delete it. Old rows (from
before the strategy concept existed) are backfilled with strategy='C', since
that's what the single-strategy version of this bot actually ran.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import config

# (column, sql_type, default_sql) — default_sql is used both in fresh
# CREATE TABLE and in ALTER TABLE ... ADD COLUMN for migrating old DBs.
DECISIONS_COLUMNS = [
    ("ts", "REAL", "0"),
    ("strategy", "TEXT", "'C'"),
    ("market_slug", "TEXT", "''"),
    ("question", "TEXT", "NULL"),
    ("market_start_ts", "REAL", "NULL"),
    ("market_end_ts", "REAL", "NULL"),
    ("seconds_remaining", "REAL", "NULL"),
    ("btc_price", "REAL", "NULL"),
    ("reference_price", "REAL", "NULL"),
    ("btc_momentum", "REAL", "NULL"),
    ("realized_vol", "REAL", "NULL"),
    ("vol_window_actual_sec", "REAL", "NULL"),
    ("model_prob_up", "REAL", "NULL"),
    ("model_prob_down", "REAL", "NULL"),
    ("market_yes_price", "REAL", "NULL"),
    ("market_no_price", "REAL", "NULL"),
    ("market_implied_prob", "REAL", "NULL"),
    ("market_prob_source", "TEXT", "NULL"),  # "live_orderbook" or "fallback_snapshot" — see engine/strategy.py.Observation
    ("raw_edge", "REAL", "NULL"),
    ("cost_buffer", "REAL", "NULL"),
    ("net_edge", "REAL", "NULL"),
    ("orderbook_imbalance", "REAL", "NULL"),
    ("spread", "REAL", "NULL"),
    ("action", "TEXT", "''"),
    ("position_size", "REAL", "NULL"),
    ("entry_price", "REAL", "NULL"),
    ("traded", "INTEGER", "0"),
    ("reason", "TEXT", "NULL"),
]

TRADES_COLUMNS = [
    ("strategy", "TEXT", "'C'"),
    ("market_slug", "TEXT", "''"),
    ("question", "TEXT", "NULL"),
    ("side", "TEXT", "''"),
    ("entry_price", "REAL", "NULL"),
    ("stake", "REAL", "NULL"),
    ("fee_paid", "REAL", "NULL"),
    ("shares", "REAL", "NULL"),
    ("opened_at", "REAL", "NULL"),
    ("settled_at", "REAL", "NULL"),
    ("won", "INTEGER", "NULL"),
    ("payout", "REAL", "NULL"),
    ("pnl", "REAL", "NULL"),
    ("edge_at_entry", "REAL", "NULL"),
    ("net_edge_at_entry", "REAL", "NULL"),
    ("seconds_remaining_at_entry", "REAL", "NULL"),
    ("orderbook_imbalance_at_entry", "REAL", "NULL"),
    ("reasoning_json", "TEXT", "NULL"),
]

MARKET_OUTCOMES_COLUMNS = [
    ("market_slug", "TEXT", "''"),
    ("question", "TEXT", "NULL"),
    ("start_ts", "REAL", "NULL"),
    ("end_ts", "REAL", "NULL"),
    ("reference_price", "REAL", "NULL"),
    ("resolution_btc_price", "REAL", "NULL"),
    ("resolved_up", "INTEGER", "NULL"),
    ("resolution_source", "TEXT", "NULL"),
    ("resolved_at", "REAL", "NULL"),
]

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS market_outcomes (market_slug TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS supervisor_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    note TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn, table: str, columns: list[tuple[str, str, str]]):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type, default_sql in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type} DEFAULT {default_sql}")


def init_db():
    with _conn() as conn:
        conn.executescript(BASE_SCHEMA)
        _ensure_columns(conn, "decisions", DECISIONS_COLUMNS)
        _ensure_columns(conn, "trades", TRADES_COLUMNS)
        _ensure_columns(conn, "market_outcomes", MARKET_OUTCOMES_COLUMNS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_slug ON decisions(market_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON decisions(strategy)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_slug ON trades(market_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def log_decision(snapshot: dict):
    """
    `snapshot` must contain exactly the DECISIONS_COLUMNS keys (missing keys
    are written as NULL/0 via sqlite3's normal None handling — this
    deliberately does NOT invent a value for a field the caller didn't have).
    """
    cols = [c for c, _, _ in DECISIONS_COLUMNS]
    placeholders = ",".join("?" for _ in cols)
    with _conn() as conn:
        conn.execute(
            f"INSERT INTO decisions ({','.join(cols)}) VALUES ({placeholders})",
            [snapshot.get(c) for c in cols],
        )


def log_settled_trade(strategy: str, trade, *, edge_at_entry: float | None,
                       net_edge_at_entry: float | None, seconds_remaining_at_entry: float | None,
                       orderbook_imbalance_at_entry: float | None = None):
    pos = trade.position
    with _conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (strategy, market_slug, question, side, entry_price, stake, fee_paid, shares,
                opened_at, settled_at, won, payout, pnl, edge_at_entry, net_edge_at_entry,
                seconds_remaining_at_entry, orderbook_imbalance_at_entry, reasoning_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy, pos.market_slug, pos.question, pos.side, pos.entry_price, pos.stake,
                pos.fee_paid, pos.shares, pos.opened_at, trade.settled_at, int(trade.won),
                trade.payout, trade.pnl, edge_at_entry, net_edge_at_entry,
                seconds_remaining_at_entry, orderbook_imbalance_at_entry,
                json.dumps(pos.reasoning_snapshot),
            ),
        )


def upsert_market_outcome(*, market_slug: str, question: str, start_ts: float | None,
                           end_ts: float, reference_price: float | None,
                           resolution_btc_price: float | None, resolved_up: bool | None,
                           resolution_source: str):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO market_outcomes
               (market_slug, question, start_ts, end_ts, reference_price,
                resolution_btc_price, resolved_up, resolution_source, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(market_slug) DO UPDATE SET
                 resolved_up=excluded.resolved_up,
                 resolution_btc_price=excluded.resolution_btc_price,
                 resolution_source=excluded.resolution_source,
                 resolved_at=excluded.resolved_at""",
            (
                market_slug, question, start_ts, end_ts, reference_price,
                resolution_btc_price, None if resolved_up is None else int(resolved_up),
                resolution_source, time.time(),
            ),
        )


def log_supervisor_note(note: str):
    with _conn() as conn:
        conn.execute("INSERT INTO supervisor_notes (ts, note) VALUES (?, ?)", (time.time(), note))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def recent_decisions(limit: int = 20, strategy: str | None = None) -> list[sqlite3.Row]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        if strategy:
            return conn.execute(
                "SELECT * FROM decisions WHERE strategy = ? ORDER BY ts DESC LIMIT ?", (strategy, limit)
            ).fetchall()
        return conn.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


def all_decisions(strategy: str | None = None) -> list[sqlite3.Row]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        if strategy:
            return conn.execute(
                "SELECT * FROM decisions WHERE strategy = ? ORDER BY ts ASC", (strategy,)
            ).fetchall()
        return conn.execute("SELECT * FROM decisions ORDER BY ts ASC").fetchall()


def all_trades(strategy: str | None = None) -> list[sqlite3.Row]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        if strategy:
            return conn.execute(
                "SELECT * FROM trades WHERE strategy = ? ORDER BY opened_at ASC", (strategy,)
            ).fetchall()
        return conn.execute("SELECT * FROM trades ORDER BY opened_at ASC").fetchall()


def all_market_outcomes() -> list[sqlite3.Row]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM market_outcomes ORDER BY end_ts ASC").fetchall()


def get_market_outcome(market_slug: str) -> sqlite3.Row | None:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM market_outcomes WHERE market_slug = ?", (market_slug,)
        ).fetchone()


def decisions_with_outcomes(strategy: str | None = None) -> list[sqlite3.Row]:
    """
    Every decision joined to its market's eventual outcome (NULL if not yet
    known/resolved). This is the table calibration analysis runs against —
    it is NOT limited to decisions that resulted in a trade.
    """
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT d.*, mo.resolved_up AS outcome_resolved_up,
                   mo.resolution_source AS outcome_source
            FROM decisions d
            LEFT JOIN market_outcomes mo ON mo.market_slug = d.market_slug
        """
        params: tuple = ()
        if strategy:
            query += " WHERE d.strategy = ?"
            params = (strategy,)
        query += " ORDER BY d.ts ASC"
        return conn.execute(query, params).fetchall()


def all_supervisor_notes(limit: int = 50) -> list[sqlite3.Row]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM supervisor_notes ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
