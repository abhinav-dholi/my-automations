"""Local finance datastore (SQLite). The ONLY module that touches SQL.

Raw data is immutable; labels/enrichment live in separate tables keyed by
source. Balances and holdings are time series (keyed by as_of). DB lives at
data/finance.db — gitignored, local-only, FileVault-encrypted at rest (§11.5).
"""
from __future__ import annotations

import json
import re
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
CREATE TABLE IF NOT EXISTS macro_series (
    series TEXT,
    date TEXT,
    value REAL,
    label TEXT,
    PRIMARY KEY (series, date)
);
CREATE TABLE IF NOT EXISTS market_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT,
    source TEXT,
    headline TEXT,
    summary TEXT,
    url TEXT,
    tickers TEXT,
    relevance TEXT
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
CREATE TABLE IF NOT EXISTS balance_overrides (
    account_id TEXT PRIMARY KEY,
    override_balance REAL,   -- the correct balance to use
    stale_balance REAL,      -- the synced value this override corrects
    created_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    as_of TEXT,
    data_json TEXT
);
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT,
    generated_at TEXT,
    model TEXT,
    input_hash TEXT,
    output_json TEXT
);
CREATE TABLE IF NOT EXISTS splitwise_balances (
    friend_id INTEGER,
    friend_name TEXT,
    currency TEXT,
    amount REAL,            -- + == owed to me
    as_of TEXT,
    PRIMARY KEY (friend_id, currency, as_of)
);
CREATE TABLE IF NOT EXISTS learned_categories (
    pattern TEXT PRIMARY KEY,   -- normalized merchant key
    category TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS splitwise_expenses (
    id INTEGER PRIMARY KEY,
    date TEXT,
    description TEXT,
    currency TEXT,
    cost REAL,
    my_owed_share REAL,
    my_paid_share REAL,
    group_id INTEGER,
    ingested_at TEXT
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


def record_splitwise_balance(conn, *, friend_id, friend_name, currency, amount, as_of) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO splitwise_balances
           (friend_id,friend_name,currency,amount,as_of) VALUES (?,?,?,?,?)""",
        (friend_id, friend_name, currency, amount, as_of),
    )


def upsert_splitwise_expense(conn, *, id, date, description, currency, cost,
                             my_owed_share, my_paid_share, group_id) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO splitwise_expenses
           (id,date,description,currency,cost,my_owed_share,my_paid_share,group_id,ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (id, date, description, currency, cost, my_owed_share, my_paid_share,
         group_id, _now()),
    )


# --- reads --------------------------------------------------------------

def upsert_market_price(conn, *, symbol, date, close) -> None:
    conn.execute("INSERT OR REPLACE INTO market_prices (symbol,date,close) VALUES (?,?,?)",
                 (symbol, date, close))


def upsert_macro(conn, *, series, date, value, label="") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO macro_series (series,date,value,label) VALUES (?,?,?,?)",
        (series, date, value, label),
    )


def add_news(conn, *, source, headline, summary, url, tickers, relevance) -> None:
    conn.execute(
        """INSERT INTO market_news (fetched_at,source,headline,summary,url,tickers,relevance)
           VALUES (?,?,?,?,?,?,?)""",
        (_now(), source, headline, summary, url, json.dumps(tickers or []), relevance),
    )


def held_symbols(conn) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT symbol FROM holdings h
           WHERE h.as_of=(SELECT MAX(as_of) FROM holdings WHERE account_id=h.account_id AND symbol=h.symbol)
             AND symbol != ''""",
    ).fetchall()
    return [r["symbol"] for r in rows]


def latest_prices(conn) -> list:
    return conn.execute(
        """SELECT symbol, close, date FROM market_prices p
           WHERE date=(SELECT MAX(date) FROM market_prices WHERE symbol=p.symbol)
           ORDER BY symbol""",
    ).fetchall()


def latest_macro(conn) -> list:
    return conn.execute(
        """SELECT series, value, label, date FROM macro_series m
           WHERE date=(SELECT MAX(date) FROM macro_series WHERE series=m.series)
           ORDER BY series""",
    ).fetchall()


def recent_news(conn, limit: int = 20) -> list:
    return conn.execute(
        "SELECT * FROM market_news ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()


def record_snapshot(conn, *, kind, data) -> None:
    conn.execute("INSERT INTO snapshots (kind,as_of,data_json) VALUES (?,?,?)",
                 (kind, _now(), json.dumps(data, default=str)))


def latest_snapshot(conn, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT as_of, data_json FROM snapshots WHERE kind=? ORDER BY id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if not row:
        return None
    d = json.loads(row["data_json"]); d["_as_of"] = row["as_of"]
    return d


def last_insight(conn, agent: str) -> dict | None:
    row = conn.execute(
        "SELECT generated_at, output_json FROM insights WHERE agent=? ORDER BY id DESC LIMIT 1",
        (agent,),
    ).fetchone()
    if not row:
        return None
    return {"generated_at": row["generated_at"], "output": json.loads(row["output_json"])}


def splitwise_net(conn) -> dict[str, float]:
    """Latest net Splitwise balance per currency (+ == owed to me)."""
    rows = conn.execute(
        """SELECT currency, SUM(amount) AS net FROM splitwise_balances b
           WHERE as_of = (SELECT MAX(as_of) FROM splitwise_balances)
           GROUP BY currency""",
    ).fetchall()
    return {r["currency"]: round(r["net"], 2) for r in rows}


def balance_series(conn) -> list:
    """Total of all account balances per sync timestamp (net worth ex-investments)."""
    return conn.execute(
        "SELECT as_of, SUM(balance) AS total FROM balances GROUP BY as_of ORDER BY as_of"
    ).fetchall()


def holdings_series(conn) -> list:
    """Total holdings market value per sync timestamp."""
    return conn.execute(
        "SELECT as_of, SUM(market_value) AS total FROM holdings GROUP BY as_of ORDER BY as_of"
    ).fetchall()


def splitwise_balances_latest(conn) -> list:
    return conn.execute(
        """SELECT friend_name, currency, amount FROM splitwise_balances
           WHERE as_of=(SELECT MAX(as_of) FROM splitwise_balances)
           ORDER BY amount DESC"""
    ).fetchall()


def splitwise_share_since(conn, since_iso: str) -> float:
    """Sum of my owed share of expenses dated on/after since_iso (my real cost)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(my_owed_share),0) AS s FROM splitwise_expenses WHERE date >= ?",
        (since_iso,),
    ).fetchone()
    return round(row["s"], 2)

# Effective category precedence: manual (user) > ai (Claude) > rule (keywords).
_CATEGORY_SELECT = """
    SELECT t.*,
           COALESCE(lm.category, la.category, lr.category, 'Uncategorized') AS category,
           CASE WHEN lm.category IS NOT NULL THEN 'manual'
                WHEN la.category IS NOT NULL THEN 'ai'
                ELSE 'rule' END AS label_source
    FROM transactions t
    LEFT JOIN transaction_labels lm ON lm.txn_id=t.id AND lm.label_source='manual'
    LEFT JOIN transaction_labels la ON la.txn_id=t.id AND la.label_source='ai'
    LEFT JOIN transaction_labels lr ON lr.txn_id=t.id AND lr.label_source='rule'
"""


def transactions_since(conn, since_unix: int) -> list[sqlite3.Row]:
    return conn.execute(
        _CATEGORY_SELECT + " WHERE t.posted >= ? ORDER BY t.posted DESC",
        (since_unix,),
    ).fetchall()


def set_manual_category(conn, txn_id: str, category: str) -> None:
    """Persist a user's category override (wins over rules, survives re-sync)."""
    set_label(conn, txn_id=txn_id, category=category,
              label_source="manual", confidence=1.0)


def merchant_key(description: str) -> str:
    """Normalized merchant signature: lowercase, letters only, first 2 tokens."""
    toks = re.sub(r"[^a-z ]", " ", (description or "").lower()).split()
    return " ".join(toks[:2])


def learn_category(conn, description: str, category: str) -> int:
    """Remember merchant→category and retroactively re-tag matching txns' rule
    labels (manual labels are untouched). Returns how many txns were re-tagged.
    Future syncs apply this automatically. Improves classification over time.
    """
    key = merchant_key(description)
    if not key:
        return 0
    conn.execute(
        """INSERT OR REPLACE INTO learned_categories (pattern,category,created_at)
           VALUES (?,?,?)""",
        (key, category, _now()),
    )
    # Re-tag existing transactions whose merchant key matches.
    retagged = 0
    for r in conn.execute("SELECT id, description FROM transactions").fetchall():
        if merchant_key(r["description"]) == key:
            set_label(conn, txn_id=r["id"], category=category,
                      label_source="rule", confidence=0.9, model="learned")
            retagged += 1
    return retagged


def learned_rules(conn) -> dict[str, str]:
    return {r["pattern"]: r["category"]
            for r in conn.execute("SELECT pattern, category FROM learned_categories")}


def transactions_between(conn, start_unix: int, end_unix: int) -> list[sqlite3.Row]:
    return conn.execute(
        _CATEGORY_SELECT + " WHERE t.posted >= ? AND t.posted <= ? ORDER BY t.posted DESC",
        (start_unix, end_unix),
    ).fetchall()


def set_balance_override(conn, *, account_id, override_balance, stale_balance, note="") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO balance_overrides
           (account_id,override_balance,stale_balance,created_at,note) VALUES (?,?,?,?,?)""",
        (account_id, override_balance, stale_balance, _now(), note),
    )


def clear_balance_override(conn, account_id: str) -> None:
    conn.execute("DELETE FROM balance_overrides WHERE account_id=?", (account_id,))


def latest_balances(conn) -> list[dict]:
    """Latest balance per account, with manual corrections applied. An override
    is honored only while the SYNCED balance still equals the stale value it
    corrected — once the bank reports anything else, the real value wins
    (auto-heals stale-feed double-counts, e.g. a transfer credited but not yet
    debited at the source)."""
    rows = [dict(r) for r in conn.execute(
        """SELECT a.id, a.name, a.type, b.balance, b.as_of FROM accounts a
           JOIN balances b ON b.account_id=a.id
           WHERE b.as_of = (SELECT MAX(as_of) FROM balances WHERE account_id=a.id)
           ORDER BY a.name""").fetchall()]
    overrides = {r["account_id"]: r for r in conn.execute("SELECT * FROM balance_overrides")}
    obsolete = []
    for r in rows:
        o = overrides.get(r["id"])
        if o is None:
            continue
        if abs(r["balance"] - o["stale_balance"]) < 0.01:   # bank still stale → use correction
            r["synced_balance"] = r["balance"]
            r["balance"] = o["override_balance"]
            r["overridden"] = True
        else:
            obsolete.append(r["id"])                        # bank updated → drop override
    for aid in obsolete:
        clear_balance_override(conn, aid)
    return rows


def latest_holdings(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM holdings h
           WHERE h.as_of = (SELECT MAX(as_of) FROM holdings
                            WHERE account_id=h.account_id AND symbol=h.symbol)
           ORDER BY market_value DESC""",
    ).fetchall()
