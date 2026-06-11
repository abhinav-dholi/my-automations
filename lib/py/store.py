"""Local finance datastore (SQLite). The ONLY module that touches SQL.

Raw data is immutable; labels/enrichment live in separate tables keyed by
source. Balances and holdings are time series (keyed by as_of). DB lives at
data/finance.db — gitignored, local-only, FileVault-encrypted at rest (§11.5).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import config

DB_PATH = config.DATA_DIR / "finance.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    institution TEXT,
    currency TEXT,
    source TEXT,
    first_seen TEXT,
    last_synced TEXT
);
CREATE TABLE IF NOT EXISTS balances (
    account_id TEXT,
    balance REAL,
    available REAL,
    as_of TEXT,
    PRIMARY KEY (account_id, as_of)
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    posted INTEGER,
    amount REAL,
    description TEXT,
    pending INTEGER,
    source TEXT,
    raw_json TEXT,
    ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS transaction_labels (
    txn_id TEXT,
    category TEXT,
    subcategory TEXT,
    tags TEXT,
    label_source TEXT,
    confidence REAL,
    model TEXT,
    labeled_at TEXT,
    PRIMARY KEY (txn_id, label_source)
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id TEXT,
    symbol TEXT,
    name TEXT,
    quantity REAL,
    cost_basis REAL,
    market_value REAL,
    asset_class TEXT,
    as_of TEXT,
    source TEXT,
    PRIMARY KEY (account_id, symbol, as_of)
);
CREATE TABLE IF NOT EXISTS market_prices (
    symbol TEXT,
    date TEXT,
    close REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    started TEXT,
    ended TEXT,
    status TEXT,
    n_accounts INTEGER,
    n_transactions INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT,
    generated_at TEXT,
    model TEXT,
    input_hash TEXT,
    output_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_posted ON transactions (posted);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions (account_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@contextmanager
def connect():
    config.ensure_runtime_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- writes -------------------------------------------------------------

def upsert_account(conn, *, id, name, type, institution, currency, source) -> None:
    conn.execute(
        """INSERT INTO accounts (id,name,type,institution,currency,source,first_seen,last_synced)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, type=excluded.type, institution=excluded.institution,
             currency=excluded.currency, last_synced=excluded.last_synced""",
        (id, name, type, institution, currency, source, _now(), _now()),
    )


def record_balance(conn, *, account_id, balance, available, as_of) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO balances (account_id,balance,available,as_of)
           VALUES (?,?,?,?)""",
        (account_id, balance, available, as_of),
    )


def upsert_transaction(conn, *, id, account_id, posted, amount, description,
                       pending, source, raw) -> bool:
    """Insert a raw transaction. Returns True if newly inserted (idempotent)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO transactions
           (id,account_id,posted,amount,description,pending,source,raw_json,ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (id, account_id, posted, amount, description, int(pending), source,
         json.dumps(raw), _now()),
    )
    return cur.rowcount > 0


def set_label(conn, *, txn_id, category, subcategory=None, tags=None,
              label_source="rule", confidence=1.0, model=None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO transaction_labels
           (txn_id,category,subcategory,tags,label_source,confidence,model,labeled_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (txn_id, category, subcategory, json.dumps(tags or []),
         label_source, confidence, model, _now()),
    )


def upsert_holding(conn, *, account_id, symbol, name, quantity, cost_basis,
                   market_value, asset_class, as_of, source) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO holdings
           (account_id,symbol,name,quantity,cost_basis,market_value,asset_class,as_of,source)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (account_id, symbol, name, quantity, cost_basis, market_value,
         asset_class, as_of, source),
    )


def record_sync(conn, *, source, started, status, n_accounts, n_transactions,
                note="") -> None:
    conn.execute(
        """INSERT INTO sync_runs (source,started,ended,status,n_accounts,n_transactions,note)
           VALUES (?,?,?,?,?,?,?)""",
        (source, started, _now(), status, n_accounts, n_transactions, note),
    )


# --- reads --------------------------------------------------------------

def transactions_since(conn, since_unix: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.*, COALESCE(l.category,'Uncategorized') AS category
           FROM transactions t
           LEFT JOIN transaction_labels l
             ON l.txn_id=t.id AND l.label_source='rule'
           WHERE t.posted >= ?
           ORDER BY t.posted DESC""",
        (since_unix,),
    ).fetchall()


def latest_balances(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.name, a.type, b.balance, b.as_of FROM accounts a
           JOIN balances b ON b.account_id=a.id
           WHERE b.as_of = (SELECT MAX(as_of) FROM balances WHERE account_id=a.id)
           ORDER BY a.name""",
    ).fetchall()


def latest_holdings(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM holdings h
           WHERE h.as_of = (SELECT MAX(as_of) FROM holdings
                            WHERE account_id=h.account_id AND symbol=h.symbol)
           ORDER BY market_value DESC""",
    ).fetchall()
