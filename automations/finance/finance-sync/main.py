"""finance-sync — ingest SimpleFIN data into the local datastore.

Idempotent: re-running never duplicates. Applies deterministic rule labels
to new transactions (agents refine later). Local-only; data never leaves box.
"""
from __future__ import annotations

import time

import yaml

import config
import finance_rules
import secrets_store
import simplefin
import store
import transfers

# SimpleFIN caps each request at ~45 days, so backfill history in chunks to
# build enough months for meaningful averages. Idempotent upserts dedupe overlap.
CHUNK_DAYS = 45
HISTORY_DAYS = 270   # ~9 months of history per sync

CREDIT_HINTS = ["credit", "card", "visa", "mastercard", "amex", "freedom",
                "sapphire", "quicksilver", "venture", "platinum", "rewards",
                "unlimited", "discover", "blue cash"]
INVEST_HINTS = ["invest", "brokerage", "individual", "401k", "ira", "roth",
                "equity award", "rsu", "schwab", "fidelity", "vanguard", "robinhood"]


def _overrides() -> dict:
    """Optional data/account_types.yaml: {name substring: type} for exact control."""
    p = config.DATA_DIR / "account_types.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def infer_type(name: str, has_holdings: bool, balance: float, overrides: dict) -> str:
    n = name.lower()
    for sub, t in overrides.items():           # user-defined wins
        if sub.lower() in n:
            return t
    if has_holdings or any(k in n for k in INVEST_HINTS):
        return "investment"
    if "saving" in n:
        return "savings"
    if any(k in n for k in ["loan", "mortgage", "student loan"]):
        return "loan"
    if any(k in n for k in CREDIT_HINTS):
        return "credit"
    if any(k in n for k in ["check", "chk", "debit", "college", "everyday"]):
        return "checking"
    # Fallback by balance sign: cards carry a balance you owe (negative).
    return "credit" if balance < 0 else "checking"


def main() -> None:
    access_url = secrets_store.get("SIMPLEFIN_ACCESS_URL")
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    now = int(time.time())
    as_of = started
    overrides = _overrides()

    store.init_db()
    n_new_txn = 0
    n_accounts = 0

    with store.connect() as conn:
        learned = store.learned_rules(conn)   # merchant key -> category (user-taught)
        # Walk back in CHUNK_DAYS windows. i==0 is the current snapshot and
        # records accounts/balances/holdings; every window ingests transactions.
        for i in range(0, HISTORY_DAYS, CHUNK_DAYS):
            w_end = now - i * 86400
            w_start = w_end - CHUNK_DAYS * 86400
            accounts = simplefin.get_accounts(access_url, w_start, w_end)
            if i == 0:
                n_accounts = len(accounts)
            for acct in accounts:
                if i == 0:
                    store.upsert_account(
                        conn, id=acct.id, name=acct.name,
                        type=infer_type(acct.name, bool(acct.holdings), acct.balance, overrides),
                        institution="", currency=acct.currency, source="simplefin",
                    )
                    store.record_balance(
                        conn, account_id=acct.id, balance=acct.balance,
                        available=acct.balance, as_of=as_of,
                    )
                    for h in acct.holdings:
                        store.upsert_holding(
                            conn, account_id=acct.id, symbol=h.symbol, name=h.name,
                            quantity=h.quantity, cost_basis=h.cost_basis,
                            market_value=h.market_value, asset_class="", as_of=as_of,
                            source="simplefin",
                        )
                for t in acct.transactions:
                    is_new = store.upsert_transaction(
                        conn, id=t.id, account_id=acct.id, posted=t.posted,
                        amount=t.amount, description=t.description,
                        pending=t.pending, source="simplefin",
                        raw={"description": t.description, "amount": t.amount},
                    )
                    if is_new:
                        n_new_txn += 1
                    cat = learned.get(store.merchant_key(t.description)) \
                        or finance_rules.categorize(t.description, t.amount)
                    store.set_label(conn, txn_id=t.id, category=cat,
                                    label_source="rule", confidence=1.0)

        # Detect internal transfers across accounts and relabel both legs as
        # Transfer (excluded from income/spend). Don't override manual labels.
        all_txns = conn.execute(
            "SELECT id, account_id, amount, posted FROM transactions"
        ).fetchall()
        manual = {r["txn_id"] for r in conn.execute(
            "SELECT txn_id FROM transaction_labels WHERE label_source='manual'")}
        transfer_ids = transfers.detect_internal_transfers(all_txns)
        n_transfers = 0
        for tid in transfer_ids:
            if tid in manual:
                continue
            store.set_label(conn, txn_id=tid, category="Transfer",
                            label_source="rule", confidence=1.0, model="transfer-match")
            n_transfers += 1

        store.record_sync(
            conn, source="simplefin", started=started, status="ok",
            n_accounts=n_accounts, n_transactions=n_new_txn,
            note=f"history={HISTORY_DAYS}d transfers={n_transfers}",
        )

    print(f"[finance-sync] {n_accounts} accounts, {n_new_txn} new transactions, "
          f"{n_transfers} transfer legs detected.")


if __name__ == "__main__":
    main()
