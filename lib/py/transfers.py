"""Detect internal transfers across the user's own accounts.

A transfer (checking→savings, credit-card payment, cash to brokerage) appears
as a debit in one account and an equal credit in another within a few days.
Neither is real income or spend, so both legs must be excluded from cashflow.
Keyword rules can't catch these reliably — amount/date pairing can.
"""
from __future__ import annotations

from collections import defaultdict

DAYS_TOLERANCE = 4


def detect_internal_transfers(txns, days_tol: int = DAYS_TOLERANCE,
                              exclude_ids: set | None = None, return_pairs: bool = False):
    """Identify the two legs of each internal transfer.

    txns: rows/dicts with id, account_id, amount (signed), posted (unix sec).
    Matches each debit to an unused equal-magnitude credit in a *different*
    account within the date tolerance. `exclude_ids` are never matched on either
    side (e.g. inbound p2p reimbursements, which look like a stray credit and
    would otherwise be mis-paired with a same-amount internal debit).

    Returns a set of leg ids, or — with return_pairs=True — a list of
    (debit_txn, credit_txn) pairs so callers can inspect the accounts (e.g. to
    label a payment landing on a credit card differently).
    """
    tol = days_tol * 86400
    exclude_ids = exclude_ids or set()
    credits = defaultdict(list)  # rounded magnitude -> [credit txns]
    for t in txns:
        if t["amount"] > 0 and t["id"] not in exclude_ids:
            credits[round(t["amount"], 2)].append(t)

    used: set[str] = set()
    transfer_ids: set[str] = set()
    pairs: list = []
    # Process debits oldest→newest for stable pairing.
    debits = sorted((t for t in txns if t["amount"] < 0 and t["id"] not in exclude_ids),
                    key=lambda t: t["posted"])
    for d in debits:
        mag = round(-d["amount"], 2)
        best = None
        for c in credits.get(mag, []):
            if c["id"] in used or c["account_id"] == d["account_id"]:
                continue
            if abs(c["posted"] - d["posted"]) <= tol:
                if best is None or abs(c["posted"] - d["posted"]) < abs(best["posted"] - d["posted"]):
                    best = c
        if best is not None:
            used.add(best["id"])
            transfer_ids.add(best["id"])
            transfer_ids.add(d["id"])
            pairs.append((d, best))
    return pairs if return_pairs else transfer_ids
