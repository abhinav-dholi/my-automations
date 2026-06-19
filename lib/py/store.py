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
    subtype TEXT,
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
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT,               -- groups messages into a conversation
    role TEXT,                  -- 'user' | 'assistant'
    content TEXT,
    charts_json TEXT,           -- assistant chart specs (catalog ids / custom)
    followups_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS txn_flags (
    txn_id TEXT PRIMARY KEY,
    one_time INTEGER DEFAULT 0,   -- real but non-recurring: kept in actuals, out of run-rate
    excluded INTEGER DEFAULT 0,   -- not real (duplicate/error): dropped from all analytics
    note TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_posted ON transactions (posted);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions (account_id);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages (conv_id, id);
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
        _migrate(conn)


def _migrate(conn) -> None:
    """Additive, idempotent schema migrations for existing DBs (SQLite can't add
    a column via CREATE TABLE IF NOT EXISTS)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "subtype" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN subtype TEXT")
    # Re-key learned merchant rules to the current merchant_key (drops leaked
    # processor-noise prefixes like 'aplpay '). Idempotent.
    for pat, cat in list(conn.execute("SELECT pattern, category FROM learned_categories")):
        toks = [t for t in pat.split() if t not in _PROC_NOISE]
        newpat = " ".join(toks[:2])
        if newpat and newpat != pat:
            conn.execute("INSERT OR REPLACE INTO learned_categories (pattern,category,created_at) "
                         "VALUES (?,?,?)", (newpat, cat, _now()))
            conn.execute("DELETE FROM learned_categories WHERE pattern=?", (pat,))


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


def set_txn_flags(conn, txn_id: str, *, one_time=None, excluded=None, note="") -> None:
    """Persist manual transaction tags. 'one_time' keeps the txn in actuals/net
    worth but removes it from typical-month run-rate; 'excluded' drops it from all
    analytics (a duplicate/error). Survives re-syncs."""
    cur = conn.execute("SELECT one_time, excluded, note FROM txn_flags WHERE txn_id=?",
                       (txn_id,)).fetchone()
    ot = int(one_time) if one_time is not None else (cur["one_time"] if cur else 0)
    ex = int(excluded) if excluded is not None else (cur["excluded"] if cur else 0)
    conn.execute(
        "INSERT OR REPLACE INTO txn_flags (txn_id,one_time,excluded,note,updated_at) "
        "VALUES (?,?,?,?,?)", (txn_id, ot, ex, note or (cur["note"] if cur else ""), _now()))


def txn_flags(conn) -> dict:
    """Map txn_id -> {'one_time': bool, 'excluded': bool} for all flagged txns."""
    return {r["txn_id"]: {"one_time": bool(r["one_time"]), "excluded": bool(r["excluded"])}
            for r in conn.execute("SELECT txn_id, one_time, excluded FROM txn_flags")}


def save_chat_message(conn, *, conv_id, role, content, charts=None, followups=None) -> None:
    """Persist one chat turn so the Ask-AI history survives reloads/restarts."""
    conn.execute(
        """INSERT INTO chat_messages (conv_id,role,content,charts_json,followups_json,created_at)
           VALUES (?,?,?,?,?,?)""",
        (conv_id, role, content, json.dumps(charts or []),
         json.dumps(followups or []), _now()),
    )


def load_chat(conn, conv_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role,content,charts_json,followups_json FROM chat_messages "
        "WHERE conv_id=? ORDER BY id", (conv_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"],
             "charts": json.loads(r["charts_json"] or "[]"),
             "followups": json.loads(r["followups_json"] or "[]")} for r in rows]


def list_chats(conn, limit: int = 30) -> list[dict]:
    """Recent conversations, newest first: id, title (first user message), count,
    and start time."""
    rows = conn.execute(
        """SELECT conv_id,
                  MIN(created_at) AS started,
                  SUM(role='user') AS questions,
                  (SELECT content FROM chat_messages c2
                   WHERE c2.conv_id=c.conv_id AND role='user' ORDER BY id LIMIT 1) AS title
           FROM chat_messages c
           GROUP BY conv_id ORDER BY MAX(id) DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_chat(conn, conv_id: str) -> None:
    conn.execute("DELETE FROM chat_messages WHERE conv_id=?", (conv_id,))


def set_account_subtype(conn, account_id: str, subtype: str) -> None:
    """Tag an account with a refinement of its type (e.g. 'hysa' for a high-yield
    savings account). Kept separate from upsert_account because it's derived after
    transactions are ingested (interest history feeds the heuristic)."""
    conn.execute("UPDATE accounts SET subtype=? WHERE id=?", (subtype or "", account_id))


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


_PROC_NOISE = {"aplpay", "applepay", "apple", "gpay", "googlepay", "bt", "tst", "sq",
               "sp", "py", "pp", "paypal", "pos", "purchase", "debit", "ach", "dd",
               "sumup", "toast", "clover", "stripe", "checkcard"}


def merchant_key(description: str) -> str:
    """Normalized merchant signature for matching/dedup. Drops payment-processor
    prefixes (Apple Pay, TST*, SQ*, BT*…) so the REAL merchant survives:
    'AplPay BT*WAYMO MOUNTAIN VIEW' -> 'waymo mountain' (not 'aplpay bt')."""
    d = (description or "").lower()
    if "*" in d:
        d = d.rsplit("*", 1)[-1]            # text after the last * is usually the merchant
    toks = [t for t in re.sub(r"[^a-z ]", " ", d).split() if t not in _PROC_NOISE]
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
        """SELECT a.id, a.name, a.type, a.subtype, a.institution, b.balance, b.as_of
           FROM accounts a
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


def data_freshness(conn) -> dict:
    """Most-recent ingest timestamp per data source, for staleness checks.
    Values are ISO8601 strings (date-only for prices) or None if a source is
    empty. Consumers (e.g. the on-demand bot) refresh only what's actually old
    instead of syncing on every read."""
    def _max(sql: str):
        return conn.execute(sql).fetchone()[0]
    return {
        "bank": _max("SELECT MAX(as_of) FROM balances"),
        "splitwise": _max("SELECT MAX(as_of) FROM splitwise_balances"),
        "prices": _max("SELECT MAX(date) FROM market_prices"),
        "news": _max("SELECT MAX(fetched_at) FROM market_news"),
    }


def latest_holdings(conn) -> list[sqlite3.Row]:
    """Current positions only. A holding is dropped if (a) the feed stopped
    reporting it — its newest snapshot predates the account's last sync by >1 day,
    so the position was closed/vested-out — or (b) it has no shares. Without this,
    vanished positions linger forever and inflate net worth (e.g. an emptied
    equity-award account still showing stale RSU value)."""
    return conn.execute(
        """SELECT h.* FROM holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.as_of = (SELECT MAX(as_of) FROM holdings
                            WHERE account_id=h.account_id AND symbol=h.symbol)
             AND substr(h.as_of,1,10) >= date(substr(a.last_synced,1,10), '-1 day')
             AND COALESCE(h.quantity,0) > 0
           ORDER BY h.market_value DESC""",
    ).fetchall()
