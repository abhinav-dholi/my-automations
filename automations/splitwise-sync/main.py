"""splitwise-sync — ingest Splitwise balances + your expense shares.

Stores net balance per friend (time series) and your owed/paid share of recent
expenses. Local-only. Idempotent.
"""
from __future__ import annotations

import time

import secrets_store
import splitwise
import store

LOOKBACK_DAYS = 90


def main() -> None:
    api_key = secrets_store.get("SPLITWISE_API_KEY")
    my_id = splitwise.current_user_id(api_key)

    as_of = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    dated_after = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - LOOKBACK_DAYS * 86400)
    )

    balances = splitwise.get_friend_balances(api_key)
    expenses = splitwise.get_expenses(api_key, my_id, dated_after)

    store.init_db()
    with store.connect() as conn:
        for b in balances:
            store.record_splitwise_balance(
                conn, friend_id=b.friend_id, friend_name=b.name,
                currency=b.currency, amount=b.amount, as_of=as_of,
            )
        for e in expenses:
            store.upsert_splitwise_expense(
                conn, id=e.id, date=e.date, description=e.description,
                currency=e.currency, cost=e.cost,
                my_owed_share=e.my_owed_share, my_paid_share=e.my_paid_share,
                group_id=e.group_id,
            )
        net = store.splitwise_net(conn)
        store.record_sync(
            conn, source="splitwise", started=as_of, status="ok",
            n_accounts=len(balances), n_transactions=len(expenses),
            note=f"net={net}",
        )

    print(f"[splitwise-sync] {len(balances)} friend balances, {len(expenses)} expenses. net={net}")


if __name__ == "__main__":
    main()
