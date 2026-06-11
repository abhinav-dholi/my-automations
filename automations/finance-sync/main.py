"""finance-sync — ingest SimpleFIN data into the local datastore.

Idempotent: re-running never duplicates. Applies deterministic rule labels
to new transactions (agents refine later). Local-only; data never leaves box.
"""
from __future__ import annotations

import time

import finance_rules
import secrets_store
import simplefin
import store

# SimpleFIN recommends <=45 days per request; idempotent upserts dedupe overlap.
LOOKBACK_DAYS = 45


def infer_type(name: str, has_holdings: bool) -> str:
    if has_holdings:
        return "investment"
    n = name.lower()
    if any(k in n for k in ["credit", "card", "visa", "mastercard", "amex"]):
        return "credit"
    if "saving" in n:
        return "savings"
    if any(k in n for k in ["check", "chk", "debit"]):
        return "checking"
    if "loan" in n or "mortgage" in n:
        return "loan"
    return "other"


def main() -> None:
    access_url = secrets_store.get("SIMPLEFIN_ACCESS_URL")
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    end = int(time.time())
    start = end - LOOKBACK_DAYS * 86400
    accounts = simplefin.get_accounts(access_url, start, end)

    store.init_db()
    n_new_txn = 0
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    with store.connect() as conn:
        for acct in accounts:
            store.upsert_account(
                conn, id=acct.id, name=acct.name,
                type=infer_type(acct.name, bool(acct.holdings)),
                institution="", currency=acct.currency, source="simplefin",
            )
            store.record_balance(
                conn, account_id=acct.id, balance=acct.balance,
                available=acct.balance, as_of=as_of,
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
                # (Re)apply rule label — cheap and idempotent.
                store.set_label(
                    conn, txn_id=t.id,
                    category=finance_rules.categorize(t.description, t.amount),
                    label_source="rule", confidence=1.0,
                )
            for h in acct.holdings:
                store.upsert_holding(
                    conn, account_id=acct.id, symbol=h.symbol, name=h.name,
                    quantity=h.quantity, cost_basis=h.cost_basis,
                    market_value=h.market_value, asset_class="", as_of=as_of,
                    source="simplefin",
                )

        store.record_sync(
            conn, source="simplefin", started=started, status="ok",
            n_accounts=len(accounts), n_transactions=n_new_txn,
            note=f"lookback={LOOKBACK_DAYS}d",
        )

    print(f"[finance-sync] {len(accounts)} accounts, {n_new_txn} new transactions ingested.")


if __name__ == "__main__":
    main()
