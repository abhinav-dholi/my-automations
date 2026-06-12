"""Deterministic feature builder — the privacy boundary (§15.3).

Reads the local finance DB and emits AGGREGATES ONLY: ratios, monthly totals,
allocation percentages, tickers. No account numbers, no raw transaction
descriptions, no balances tied to identifiers. This is the only finance data
Claude ever sees, so raw data never leaves the box.
"""
from __future__ import annotations

import time

import finance_rules
import store

LIQUID_TYPES = {"checking", "savings"}


def _months_span(rows) -> float:
    if not rows:
        return 1.0
    oldest = min(r["posted"] for r in rows)
    days = max(1, (int(time.time()) - oldest) / 86400)
    return max(1.0, days / 30.0)


def build(profile: dict | None = None, lookback_days: int = 90) -> dict:
    since = int(time.time()) - lookback_days * 86400
    since_iso = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(since))
    with store.connect() as conn:
        txns = store.transactions_since(conn, since)
        balances = store.latest_balances(conn)
        holdings = store.latest_holdings(conn)
        sw_net = store.splitwise_net(conn)
        sw_share = store.splitwise_share_since(conn, since_iso)

    months = _months_span(txns)

    spend_by_cat: dict[str, float] = {}
    total_spend = total_income = excluded_movement = 0.0
    for t in txns:
        cat = t["category"]
        amt = t["amount"]
        if cat == "Income":
            if amt > 0:
                total_income += amt
        elif cat in finance_rules.NON_SPEND:
            # Transfers / card payments / investment moves — not consumption.
            excluded_movement += abs(amt)
        elif amt < 0:
            spend_by_cat[cat] = spend_by_cat.get(cat, 0.0) + abs(amt)
            total_spend += abs(amt)

    monthly_income = round(total_income / months, 2)
    monthly_spend = round(total_spend / months, 2)
    savings_rate = round((monthly_income - monthly_spend) / monthly_income, 3) if monthly_income else 0.0

    liquid = sum(b["balance"] for b in balances if b["type"] in LIQUID_TYPES)
    emergency_months = round(liquid / monthly_spend, 1) if monthly_spend else None

    invest_total = sum(h["market_value"] for h in holdings) or 0.0
    allocation = [
        {
            "symbol": h["symbol"],
            "pct_of_portfolio": round(h["market_value"] / invest_total, 3) if invest_total else 0.0,
            "gain_pct": round((h["market_value"] - h["cost_basis"]) / h["cost_basis"], 3)
            if h["cost_basis"] else None,
        }
        for h in holdings
    ]
    top_concentration = max((a["pct_of_portfolio"] for a in allocation), default=0.0)

    net_worth = round(sum(b["balance"] for b in balances) + invest_total, 2)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lookback_days": lookback_days,
        "cashflow": {
            "monthly_income": monthly_income,
            "monthly_spend": monthly_spend,
            "savings_rate": savings_rate,
            "monthly_investable_estimate": round(monthly_income - monthly_spend, 2),
            "excluded_movement_total": round(excluded_movement, 2),
            "note": "spend excludes transfers, card payments, and investment moves",
        },
        "spend_by_category_monthly": {
            k: round(v / months, 2)
            for k, v in sorted(spend_by_cat.items(), key=lambda kv: -kv[1])
        },
        "resilience": {
            "liquid_cash": round(liquid, 2),
            "emergency_fund_months": emergency_months,
        },
        "portfolio": {
            "invested_total": round(invest_total, 2),
            "allocation": allocation,
            "top_concentration_pct": round(top_concentration, 3),
        },
        "net_worth": net_worth,
        "splitwise": {
            # + == owed to you, - == you owe. Settle receivables before counting
            # as spendable; surfaced separately, NOT folded into liquid cash.
            "net_balance_by_currency": sw_net,
            "your_expense_share_in_window": sw_share,
        },
        "profile": profile or {},
    }
