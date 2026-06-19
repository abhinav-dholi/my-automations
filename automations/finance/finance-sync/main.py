"""finance-sync — ingest SimpleFIN data into the local datastore.

Idempotent: re-running never duplicates. Applies deterministic rule labels
to new transactions (agents refine later). Local-only; data never leaves box.
"""
from __future__ import annotations

import time

import yaml

import account_classify
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


def _implied_apy(conn, account_id: str, balance: float) -> float | None:
    """Annualized yield from ingested interest credits, or None if no interest
    history. Assumes monthly posting (the norm for savings): mean payment × 12 ÷
    balance. Lets us tell a true HYSA (~3–5%) from a brick-and-mortar savings
    account (~0%) without trusting the account's name."""
    if not balance or balance <= 0:
        return None
    rows = conn.execute(
        "SELECT amount FROM transactions WHERE account_id=? AND amount>0 "
        "AND lower(description) LIKE '%interest%'", (account_id,),
    ).fetchall()
    pays = [r["amount"] for r in rows]
    if not pays:
        return None
    return (sum(pays) / len(pays)) * 12.0 / balance


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
                snapshot = accounts        # current-state list for post-ingest classification
            for acct in accounts:
                if i == 0:
                    store.upsert_account(
                        conn, id=acct.id, name=acct.name,
                        type=infer_type(acct.name, bool(acct.holdings), acct.balance, overrides),
                        institution=acct.institution, currency=acct.currency, source="simplefin",
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
        # Inbound p2p reimbursements are excluded so a friend's repayment isn't
        # mis-paired with a same-amount internal debit (it's real cash to you).
        all_txns = conn.execute(
            "SELECT id, account_id, amount, posted, description FROM transactions"
        ).fetchall()
        reimb_ids = {r["id"] for r in all_txns
                     if finance_rules.is_reimbursement(r["description"], r["amount"])}
        manual = {r["txn_id"] for r in conn.execute(
            "SELECT txn_id FROM transaction_labels WHERE label_source='manual'")}
        acct_type = {r["id"]: r["type"] for r in conn.execute("SELECT id, type FROM accounts")}
        pairs = transfers.detect_internal_transfers(all_txns, exclude_ids=reimb_ids, return_pairs=True)
        n_transfers = 0
        for debit, credit in pairs:
            # A transfer that lands on a credit card is a bill PAYMENT (clearer than
            # the generic Transfer); both are excluded from spend identically.
            cat = "Payment" if (acct_type.get(debit["account_id"]) == "credit"
                                or acct_type.get(credit["account_id"]) == "credit") else "Transfer"
            for leg in (debit, credit):
                if leg["id"] in manual:
                    continue
                store.set_label(conn, txn_id=leg["id"], category=cat,
                                label_source="rule", confidence=1.0, model="transfer-match")
                n_transfers += 1

        # Classify subtypes (brokerage/retirement/equity / HYSA-vs-regular) from
        # name + institution + measured interest yield, so accounts self-label.
        n_subtyped = 0
        for acct in snapshot:
            base = infer_type(acct.name, bool(acct.holdings), acct.balance, overrides)
            apy = _implied_apy(conn, acct.id, acct.balance) if base == "savings" else None
            sub = account_classify.subtype(
                name=acct.name, base_type=base,
                org_domain=acct.org_domain, implied_apy=apy,
            )
            store.set_account_subtype(conn, acct.id, sub)
            if sub:
                n_subtyped += 1

        store.record_sync(
            conn, source="simplefin", started=started, status="ok",
            n_accounts=n_accounts, n_transactions=n_new_txn,
            note=f"history={HISTORY_DAYS}d transfers={n_transfers} subtyped={n_subtyped}",
        )

    print(f"[finance-sync] {n_accounts} accounts, {n_new_txn} new transactions, "
          f"{n_transfers} transfer legs detected, {n_subtyped} subtyped.")


if __name__ == "__main__":
    main()
