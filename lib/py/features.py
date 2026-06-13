"""Deterministic feature builder — the privacy boundary (§15.3).

Reads the local finance DB and emits AGGREGATES ONLY: ratios, monthly totals,
allocation percentages, tickers. No account numbers, no raw transaction
descriptions, no balances tied to identifiers. This is the only finance data
Claude ever sees, so raw data never leaves the box.
"""
from __future__ import annotations

import statistics
import time

import finance_rules
import store

LIQUID_TYPES = {"checking", "savings"}
ANALYZE_DAYS = 400          # pull this much history for monthly aggregation


def _ym(posted: int) -> str:
    return time.strftime("%Y-%m", time.localtime(posted))


def window_summary(days: int) -> dict:
    """RAW totals over exactly the last `days` (not a monthly average).

    For precise date-bounded questions like "spent on travel in the last 45 days".
    """
    now = int(time.time())
    since = now - days * 86400
    with store.connect() as conn:
        txns = store.transactions_between(conn, since, now)
    by_cat: dict[str, float] = {}
    spend = income = movement = 0.0
    for t in txns:
        cat, amt = t["category"], t["amount"]
        if cat == "Income":
            if amt > 0:
                income += amt
        elif cat in finance_rules.NON_SPEND:
            movement += abs(amt)
        elif amt < 0:
            by_cat[cat] = by_cat.get(cat, 0.0) + abs(amt)
            spend += abs(amt)
    return {
        "window_days": days,
        "total_spend": round(spend, 2),
        "total_income": round(income, 2),
        "spend_by_category": {k: round(v, 2) for k, v in sorted(by_cat.items(), key=lambda x: -x[1])},
        "excluded_movement": round(movement, 2),
        "txn_count": len(txns),
    }


def build(profile: dict | None = None, lookback_days: int = ANALYZE_DAYS) -> dict:
    since = int(time.time()) - lookback_days * 86400
    since_iso = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(since))
    with store.connect() as conn:
        txns = store.transactions_since(conn, since)
        balances = store.latest_balances(conn)
        holdings = store.latest_holdings(conn)
        sw_net = store.splitwise_net(conn)
        sw_share = store.splitwise_share_since(conn, since_iso)

    # Aggregate per calendar month so we can average robustly.
    cur_month = _ym(int(time.time()))
    m_income: dict[str, float] = {}
    m_spend: dict[str, float] = {}
    m_spend_cat: dict[str, dict[str, float]] = {}
    excluded_movement = 0.0
    for t in txns:
        cat, amt, ym = t["category"], t["amount"], _ym(t["posted"])
        if cat == "Income":
            if amt > 0:
                m_income[ym] = m_income.get(ym, 0.0) + amt
        elif cat in finance_rules.NON_SPEND:
            excluded_movement += abs(amt)
        elif amt < 0:
            m_spend[ym] = m_spend.get(ym, 0.0) + abs(amt)
            m_spend_cat.setdefault(ym, {})[cat] = m_spend_cat.setdefault(ym, {}).get(cat, 0.0) + abs(amt)

    # Complete months only: drop the current partial month, and drop the
    # earliest month if the data starts mid-month (also partial). Use MEDIAN so
    # a one-off month (e.g. paying something off early) doesn't skew the typical
    # month; also expose mean + the per-month series for transparency.
    months = sorted(set(m_income) | set(m_spend))
    complete = [m for m in months if m != cur_month] or months
    if len(complete) >= 2 and txns:
        earliest = min(complete)
        first_day = min((t["posted"] for t in txns if _ym(t["posted"]) == earliest), default=0)
        if first_day and int(time.strftime("%d", time.localtime(first_day))) > 3:
            complete = [m for m in complete if m != earliest] or complete

    def _median(d):
        vals = [d.get(m, 0.0) for m in complete]
        return round(statistics.median(vals), 2) if vals else 0.0

    def _mean(d):
        vals = [d.get(m, 0.0) for m in complete]
        return round(statistics.fmean(vals), 2) if vals else 0.0

    monthly_income = _median(m_income)
    monthly_spend = _median(m_spend)
    avg_monthly_spend = _mean(m_spend)
    savings_rate = round((monthly_income - monthly_spend) / monthly_income, 3) if monthly_income else 0.0

    # Per-month series (for the dashboard trend) + category monthly average.
    monthly_series = {m: {"income": round(m_income.get(m, 0.0), 2),
                          "spend": round(m_spend.get(m, 0.0), 2)} for m in months}
    spend_by_cat: dict[str, float] = {}
    n = max(1, len(complete))
    for m in complete:
        for c, v in m_spend_cat.get(m, {}).items():
            spend_by_cat[c] = spend_by_cat.get(c, 0.0) + v / n  # mean per month

    liquid = sum(b["balance"] for b in balances if b["type"] in LIQUID_TYPES)
    emergency_months = round(liquid / monthly_spend, 1) if monthly_spend else None

    # Net worth = sum of all account balances. An investment account's balance
    # already reflects its holdings, so do NOT add holdings again (avoids the
    # double-count). Investable portfolio = funded investment-account balances.
    net_worth = round(sum(b["balance"] for b in balances), 2)
    invest_total = round(sum(b["balance"] for b in balances if b["type"] == "investment"), 2)

    # Allocation: only holdings in funded accounts (balance > 0) — excludes
    # unvested/$0-balance accounts (e.g. equity awards) that would overstate.
    funded = {b["id"] for b in balances if b["type"] == "investment" and b["balance"] > 0}
    funded_holdings = [h for h in holdings if h["account_id"] in funded]
    hv = sum(h["market_value"] for h in funded_holdings) or 0.0
    allocation = [
        {
            "symbol": h["symbol"],
            "name": h["name"],   # e.g. "VANGUARD TARGET 2065" — lets agents see what it actually is
            "pct_of_portfolio": round(h["market_value"] / hv, 3) if hv else 0.0,
            "gain_pct": round((h["market_value"] - h["cost_basis"]) / h["cost_basis"], 3)
            if h["cost_basis"] else None,
        }
        for h in funded_holdings
    ]
    top_concentration = max((a["pct_of_portfolio"] for a in allocation), default=0.0)

    # Deterministic current allocation across investable assets (liquid cash +
    # investments), so the analysis is consistent run-to-run. Bonds detected by
    # symbol/name; everything else in investments counts as equity.
    BOND_SYMS = {"BND", "AGG", "BNDX", "BIV", "VCIT", "VGIT", "SCHZ", "GOVT"}
    bond_val = sum(h["market_value"] for h in funded_holdings
                   if h["symbol"] in BOND_SYMS or "bond" in (h["name"] or "").lower())
    equity_val = max(invest_total - bond_val, 0.0)
    investable_base = round(liquid + invest_total, 2)
    alloc_current = {
        "equity": round(equity_val / investable_base, 3) if investable_base else 0.0,
        "bonds": round(bond_val / investable_base, 3) if investable_base else 0.0,
        "cash": round(liquid / investable_base, 3) if investable_base else 0.0,
    }
    alloc_target = (profile or {}).get("target_allocation", {})

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lookback_days": lookback_days,
        "months_analyzed": len(complete),
        "cashflow": {
            "monthly_income": monthly_income,          # median complete month
            "monthly_spend": monthly_spend,            # median complete month (robust to one-offs)
            "avg_monthly_spend": avg_monthly_spend,    # mean, for comparison
            "savings_rate": savings_rate,
            "monthly_investable_estimate": round(monthly_income - monthly_spend, 2),
            "excluded_movement_total": round(excluded_movement, 2),
            "note": ("medians over complete calendar months; spend excludes "
                     "transfers, card payments, and investment moves"),
        },
        "monthly_series": monthly_series,              # {YYYY-MM: {income, spend}}
        "spend_by_category_monthly": {
            k: round(v, 2)
            for k, v in sorted(spend_by_cat.items(), key=lambda kv: -kv[1])
        },
        "resilience": {
            "liquid_cash": round(liquid, 2),
            "emergency_fund_months": emergency_months,
        },
        "portfolio": {
            "invested_total": round(invest_total, 2),
            "holdings": allocation,
            "top_concentration_pct": round(top_concentration, 3),
        },
        "allocation": {
            # % across investable assets (liquid cash + investments). Use these
            # verbatim in analysis — do not redefine the denominator.
            "investable_base": investable_base,
            "current": alloc_current,
            "target": alloc_target,
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
