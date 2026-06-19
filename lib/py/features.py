"""Deterministic feature builder — the privacy boundary (§15.3).

Reads the local finance DB and emits AGGREGATES ONLY: ratios, monthly totals,
allocation percentages, tickers. No account numbers, no raw transaction
descriptions, no balances tied to identifiers. This is the only finance data
Claude ever sees, so raw data never leaves the box.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import defaultdict

import yaml

import config
import finance_rules
import store


def _load_profile() -> dict:
    """User profile (risk, goals, target allocation, 401k %). Real file wins;
    committed example is the fallback."""
    real = config.DATA_DIR / "profile.yaml"
    example = config.REPO_ROOT / "profile.example.yaml"
    path = real if real.exists() else example
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}


def _clean_acct(name: str) -> str:
    # Drop trailing "(1234)" account-number fragments before anything leaves the box.
    return re.sub(r"\s*\(\d+\)\s*$", "", name or "").strip()


def _acct_role(name: str, type_: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ["401", "ira", "roth", "retirement", "403b"]):
        return "retirement (tax-advantaged, illiquid until ~59.5)"
    if "hsa" in n or "health savings" in n:
        return "HSA (tax-advantaged; medical, or long-term if invested)"
    if "equity award" in n or "rsu" in n or "espp" in n:
        return "equity award / employer stock"
    if type_ == "investment":
        return "taxable brokerage"
    if type_ in ("checking", "savings"):
        return "cash"
    if type_ == "credit":
        return "credit card (debt)"
    if type_ == "loan":
        return "loan"
    return type_

LIQUID_TYPES = {"checking", "savings"}
ANALYZE_DAYS = 400          # pull this much history for monthly aggregation
# Emergency fund covers ESSENTIAL recurring expenses only — not discretionary
# (Travel/Shopping/Dining) or one-off costs (e.g. a recent relocation).
ESSENTIAL_CATEGORIES = {"Rent", "Utilities", "Groceries", "Health", "Transport"}


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
    if profile is None:                 # auto-load so every caller gets it
        profile = _load_profile()
    since = int(time.time()) - lookback_days * 86400
    since_iso = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(since))
    with store.connect() as conn:
        txns = store.transactions_since(conn, since)
        balances = store.latest_balances(conn)
        holdings = store.latest_holdings(conn)
        sw_net = store.splitwise_net(conn)
        sw_share = store.splitwise_share_since(conn, since_iso)
        flags = store.txn_flags(conn)        # manual one-time / excluded tags

    # Aggregate per calendar month. Two parallel sets: actuals (for the per-month
    # series / month-to-date) and one-time portions (subtracted out for the
    # typical-month run-rate). 'excluded' txns are ignored entirely.
    cur_month = _ym(int(time.time()))
    m_income: dict[str, float] = {}
    m_spend: dict[str, float] = {}
    m_spend_cat: dict[str, dict[str, float]] = {}
    m_reimb: dict[str, float] = {}        # inbound p2p reimbursements (cash back for fronted spend)
    m_p2p_out: dict[str, float] = {}      # outbound p2p to people (netted against reimbursements)
    ot_income: dict[str, float] = {}
    ot_spend: dict[str, float] = {}
    ot_spend_cat: dict[str, dict[str, float]] = {}
    ot_reimb: dict[str, float] = {}
    ot_p2p_out: dict[str, float] = {}
    income_dates: list[int] = []
    excluded_movement = 0.0
    for t in txns:
        fl = flags.get(t["id"], {})
        if fl.get("excluded"):            # duplicate/error → not real, drop everywhere
            continue
        ot = fl.get("one_time", False)    # real but non-recurring → keep in actuals only
        cat, amt, ym = t["category"], t["amount"], _ym(t["posted"])
        if amt < 0 and finance_rules.is_p2p_outflow(t["description"], amt):
            m_p2p_out[ym] = m_p2p_out.get(ym, 0.0) + abs(amt)
            if ot:
                ot_p2p_out[ym] = ot_p2p_out.get(ym, 0.0) + abs(amt)
        if cat == "Income":
            if amt > 0:
                m_income[ym] = m_income.get(ym, 0.0) + amt
                income_dates.append(t["posted"])
                if ot:
                    ot_income[ym] = ot_income.get(ym, 0.0) + amt
        elif cat == "Reimbursement":
            if amt > 0:
                m_reimb[ym] = m_reimb.get(ym, 0.0) + amt
                if ot:
                    ot_reimb[ym] = ot_reimb.get(ym, 0.0) + amt
        elif cat in finance_rules.NON_SPEND:
            excluded_movement += abs(amt)
        elif amt < 0:
            m_spend[ym] = m_spend.get(ym, 0.0) + abs(amt)
            m_spend_cat.setdefault(ym, {})[cat] = m_spend_cat.setdefault(ym, {}).get(cat, 0.0) + abs(amt)
            if ot:
                ot_spend[ym] = ot_spend.get(ym, 0.0) + abs(amt)
                ot_spend_cat.setdefault(ym, {})[cat] = ot_spend_cat.setdefault(ym, {}).get(cat, 0.0) + abs(amt)

    # Recurring (run-rate) views = actuals minus one-time portions.
    def _rec(actual: dict, onetime: dict) -> dict:
        return {m: actual.get(m, 0.0) - onetime.get(m, 0.0) for m in actual}
    r_income = _rec(m_income, ot_income)
    r_spend = _rec(m_spend, ot_spend)
    r_reimb = _rec(m_reimb, ot_reimb)
    r_p2p_out = _rec(m_p2p_out, ot_p2p_out)
    r_spend_cat = {m: {c: v - ot_spend_cat.get(m, {}).get(c, 0.0) for c, v in cats.items()}
                   for m, cats in m_spend_cat.items()}

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

    # Pay cadence (biweekly pay = 26/yr, not 24) — from gaps between paychecks.
    pay_dates = sorted(income_dates)
    gaps = [(b - a) / 86400 for a, b in zip(pay_dates, pay_dates[1:])]
    med_gap = statistics.median(gaps) if gaps else None
    if med_gap is None:
        pay_cadence = "unknown"
    elif med_gap <= 9:
        pay_cadence = "weekly"
    elif med_gap <= 18:
        pay_cadence = "biweekly (26/yr)"
    elif med_gap <= 24:
        pay_cadence = "semi-monthly"
    else:
        pay_cadence = "monthly"

    # CASH-HONEST cashflow. Run-rate monthly figures = complete-window totals /
    # #complete months. Median spend kept as the robust "typical month".
    n = max(1, len(complete))
    tot_income = sum(r_income.get(m, 0.0) for m in complete)   # recurring (excl. one-time)
    tot_spend = sum(r_spend.get(m, 0.0) for m in complete)
    monthly_income = round(tot_income / n, 2)        # recurring take-home (payroll)
    monthly_spend = round(tot_spend / n, 2)          # recurring consumption (excl. one-time)
    typical_month_spend = _median(r_spend)
    avg_monthly_spend = _mean(r_spend)

    acct_type = {b["id"]: b["type"] for b in balances}
    acct_name = {b["id"]: (b["name"] or "").lower() for b in balances}

    def _is_retirement(aid: str) -> bool:
        n2 = acct_name.get(aid, "")
        return any(k in n2 for k in ["401", "ira", "roth", "retirement", "403b", "hsa", "health savings"])

    def _is_savings_dest(aid: str) -> bool:
        t = acct_type.get(aid)
        if t == "savings":
            return True
        if t == "investment" and not _is_retirement(aid):   # taxable brokerage / equity
            return True
        return False

    # NET reimbursements = cash people paid you back (inbound p2p) MINUS money you
    # sent to people (outbound p2p). Netting makes a pass-through (got $X from A,
    # sent $X to B) a wash, and counts a genuine repayment for spend you fronted.
    gross_reimb = sum(r_reimb.get(m, 0.0) for m in complete) / n
    p2p_sent = sum(r_p2p_out.get(m, 0.0) for m in complete) / n
    monthly_reimbursements = round(gross_reimb, 2)
    monthly_p2p_sent = round(p2p_sent, 2)
    net_reimbursements = round(gross_reimb - p2p_sent, 2)

    # THE headline number: operating cash surplus = recurring take-home MINUS
    # consumption, plus NET reimbursements. The real monthly cash you keep from
    # salary. We do NOT infer phantom "other income"; lumpy RSU/bonus/refund
    # inflows are reported separately and never folded into recurring income.
    operating_cash_surplus = round(monthly_income - monthly_spend + net_reimbursements, 2)
    operating_surplus_rate = round(operating_cash_surplus / monthly_income, 3) if monthly_income else 0.0

    # Pre-tax 401(k)/HSA contributions (Investment moves in retirement accounts):
    # wealth-building but NOT spendable cash and NOT in take-home above.
    monthly_pretax_retirement = round(-sum(
        t["amount"] for t in txns
        if _ym(t["posted"]) in complete and t["category"] == "Investment"
        and t["amount"] < 0 and _is_retirement(t["account_id"])
        and not flags.get(t["id"], {}).get("excluded")
    ) / n, 2)

    # Cash MOVED into savings/taxable accounts (transfers + deposits). This is
    # relocation of money — it can be funded by one-time inflows or existing cash,
    # so it is NOT recurring saving and must never be read as income.
    monthly_moved_to_savings = round(sum(
        t["amount"] for t in txns
        if _ym(t["posted"]) in complete and t["category"] in finance_rules.NON_SPEND
        and t["category"] != "Income" and t["amount"] > 0 and _is_savings_dest(t["account_id"])
        and not flags.get(t["id"], {}).get("excluded")
    ) / n, 2)

    # One-time / lumpy cash inflows (tax refunds, bonuses) — non-payroll credits to
    # liquid accounts. Reported as a window TOTAL (not a monthly average) so a
    # one-off refund never looks like recurring monthly money.
    one_time_inflows_window = round(sum(
        t["amount"] for t in txns
        if _ym(t["posted"]) in complete and t["category"] == "Credit"
        and t["amount"] > 0 and acct_type.get(t["account_id"]) in LIQUID_TYPES
    ), 2)

    # Back-compat alias: savings_rate now means the honest operating rate.
    savings_rate = operating_surplus_rate

    # Per-month series (for the dashboard trend) + category monthly average.
    monthly_series = {m: {"income": round(m_income.get(m, 0.0), 2),
                          "spend": round(m_spend.get(m, 0.0), 2)} for m in months}
    spend_by_cat: dict[str, float] = {}
    n = max(1, len(complete))
    for m in complete:
        for c, v in r_spend_cat.get(m, {}).items():     # recurring (one-time excluded)
            spend_by_cat[c] = spend_by_cat.get(c, 0.0) + v / n  # mean per month

    # Emergency fund covers ESSENTIAL monthly expenses only (rent/utilities/
    # groceries/transport/health) — not discretionary or one-off relocation costs.
    essential_monthly = round(sum(v for c, v in spend_by_cat.items()
                                  if c in ESSENTIAL_CATEGORIES), 2)
    liquid = sum(b["balance"] for b in balances if b["type"] in LIQUID_TYPES)
    emergency_months = round(liquid / essential_monthly, 1) if essential_monthly else None
    # Investable surplus = liquid cash BEYOND the emergency reserve. Money still
    # needed for the buffer is NOT investable; the to-savings flow builds it.
    emerg_target_months = float((profile or {}).get("emergency_fund_months_target", 6) or 6)
    emergency_target = round(emerg_target_months * essential_monthly, 2)
    investable_surplus = round(max(0.0, liquid - emergency_target), 2)

    # Per-account value. For investment accounts use max(cash balance, holdings
    # value): handles brokerages whose balance already includes holdings AND
    # equity-award accounts that report $0 cash but hold vested stock.
    hold_by_acct: dict[str, float] = defaultdict(float)
    for h in holdings:
        hold_by_acct[h["account_id"]] += h["market_value"] or 0.0

    def _acct_value(b) -> float:
        if b["type"] == "investment":
            return max(b["balance"], hold_by_acct.get(b["id"], 0.0))
        return b["balance"]

    # Net-worth buckets. Retirement (401k/HSA) is LOCKED — shown for completeness
    # but excluded from what you can invest now. Investable = cash you control.
    taxable_inv = round(sum(_acct_value(b) for b in balances
                            if b["type"] == "investment" and not _is_retirement(b["id"])), 2)
    retirement_locked = round(sum(_acct_value(b) for b in balances
                                  if b["type"] == "investment" and _is_retirement(b["id"])), 2)
    credit_debt = round(sum(b["balance"] for b in balances if b["type"] == "credit"), 2)
    other_bal = round(sum(b["balance"] for b in balances
                          if b["type"] not in ("checking", "savings", "investment", "credit")), 2)
    # Splitwise net is a real receivable (+ owed to you) / liability (− you owe):
    # money you fronted for others returns to you, so it belongs in net worth.
    splitwise_receivable = round(sum(sw_net.values()), 2) if sw_net else 0.0
    net_worth = round(liquid + taxable_inv + retirement_locked + credit_debt
                      + other_bal + splitwise_receivable, 2)
    controllable = round(liquid + taxable_inv, 2)  # cash + taxable holdings you can rebalance

    # Concentration is over what you CONTROL (cash + taxable holdings), excluding
    # the locked 401(k) TDF. Aggregate taxable holdings by symbol.
    taxable_ids = {b["id"] for b in balances
                   if b["type"] == "investment" and not _is_retirement(b["id"]) and _acct_value(b) > 0}
    agg: dict[str, dict] = defaultdict(lambda: {"mv": 0.0, "cost": 0.0, "name": ""})
    for h in holdings:
        if h["account_id"] in taxable_ids:
            a = agg[h["symbol"]]
            a["mv"] += h["market_value"] or 0.0
            a["cost"] += h["cost_basis"] or 0.0
            a["name"] = h["name"]
    hv = sum(a["mv"] for a in agg.values()) or 0.0
    allocation = [
        {
            "symbol": s, "name": a["name"],
            "pct_of_taxable": round(a["mv"] / hv, 3) if hv else 0.0,
            "pct_of_controllable": round(a["mv"] / controllable, 3) if controllable else 0.0,
            "gain_pct": round((a["mv"] - a["cost"]) / a["cost"], 3) if a["cost"] else None,
        }
        for s, a in sorted(agg.items(), key=lambda kv: -kv[1]["mv"])
    ]
    top_concentration = max((a["pct_of_controllable"] for a in allocation), default=0.0)

    # Current allocation across CONTROLLABLE assets (cash + taxable), excl. 401(k).
    BOND_SYMS = {"BND", "AGG", "BNDX", "BIV", "VCIT", "VGIT", "SCHZ", "GOVT"}
    bond_val = sum(a["mv"] for s, a in agg.items()
                   if s in BOND_SYMS or "bond" in (a["name"] or "").lower())
    equity_val = max(taxable_inv - bond_val, 0.0)
    investable_base = controllable
    alloc_current = {
        "equity": round(equity_val / investable_base, 3) if investable_base else 0.0,
        "bonds": round(bond_val / investable_base, 3) if investable_base else 0.0,
        "cash": round(liquid / investable_base, 3) if investable_base else 0.0,
    }
    alloc_target = (profile or {}).get("target_allocation", {})
    invest_total = controllable  # back-comat name used below

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lookback_days": lookback_days,
        "months_analyzed": len(complete),
        "cashflow": {
            "monthly_income": monthly_income,          # recurring take-home (payroll only)
            "monthly_spend": monthly_spend,            # gross consumption (excl. transfers/savings/investing)
            "monthly_reimbursements": monthly_reimbursements,   # cash people paid you back (inbound p2p)
            "monthly_p2p_sent": monthly_p2p_sent,      # money you sent to people (outbound p2p)
            "net_reimbursements": net_reimbursements,  # inbound - outbound (folded into surplus)
            "operating_cash_surplus": operating_cash_surplus,   # income - spend + net reimbursements = real cash kept/mo
            "operating_surplus_rate": operating_surplus_rate,   # surplus / take-home
            "savings_rate": savings_rate,              # == operating_surplus_rate (honest, cash-based)
            "monthly_pretax_retirement": monthly_pretax_retirement,  # 401k/HSA pre-tax (wealth, not cash)
            "monthly_moved_to_savings": monthly_moved_to_savings,    # gross transfers INTO savings — relocation, NOT income
            "one_time_inflows_window": one_time_inflows_window,      # refunds/bonuses TOTAL over the window (lumpy)
            "typical_month_spend": typical_month_spend,
            "avg_monthly_spend": avg_monthly_spend,
            "complete_months": len(complete),
            "pay_cadence": pay_cadence,
            "excluded_movement_total": round(excluded_movement, 2),
            "note": (f"Cash-based, after-tax, over {len(complete)} complete month(s) ({pay_cadence} pay). "
                     f"Take-home ~${monthly_income:,.0f}/mo − spend ~${monthly_spend:,.0f}/mo + net "
                     f"reimbursements ~${net_reimbursements:,.0f}/mo (people paid you back "
                     f"${monthly_reimbursements:,.0f}, you sent ${monthly_p2p_sent:,.0f}) = OPERATING CASH "
                     f"SURPLUS ~${operating_cash_surplus:,.0f}/mo ({operating_surplus_rate*100:.0f}% of take-home). "
                     f"Separately (NOT recurring cash): ~${monthly_pretax_retirement:,.0f}/mo pre-tax 401(k); "
                     f"${one_time_inflows_window:,.0f} one-time inflows (refunds/bonuses) over the window. Money "
                     f"moved into savings (~${monthly_moved_to_savings:,.0f}/mo) is relocation, NOT income."),
        },
        "month_to_date": {                             # current (incomplete) month, live
            "month": cur_month,
            "day": int(time.strftime("%d")),
            "income": round(m_income.get(cur_month, 0.0), 2),
            "spend": round(m_spend.get(cur_month, 0.0), 2),
            "net": round(m_income.get(cur_month, 0.0) - m_spend.get(cur_month, 0.0), 2),
        },
        "monthly_series": monthly_series,              # {YYYY-MM: {income, spend}}
        "spend_by_category_monthly": {
            k: round(v, 2)
            for k, v in sorted(spend_by_cat.items(), key=lambda kv: -kv[1])
        },
        "resilience": {
            "liquid_cash": round(liquid, 2),
            "essential_monthly": essential_monthly,    # rent/utilities/groceries/transport/health
            "emergency_fund_months": emergency_months,  # months of ESSENTIALS covered by liquid
            "emergency_fund_target_months": emerg_target_months,
            "emergency_fund_target": emergency_target,
            "emergency_fund_funded": emergency_months is not None and emergency_months >= emerg_target_months,
            "note": "Emergency fund sizes ESSENTIAL expenses only — not discretionary/one-off spend.",
        },
        "net_worth": net_worth,
        "net_worth_breakdown": {
            "liquid_cash": round(liquid, 2),
            "taxable_investments": taxable_inv,
            "retirement_locked": retirement_locked,   # 401k/HSA — not investable now
            "credit_debt": credit_debt,
            "splitwise_receivable": splitwise_receivable,   # + owed to you / − you owe
        },
        "investable_surplus": investable_surplus,      # liquid cash BEYOND the emergency reserve (deployable)
        "investable_cash": investable_surplus,         # alias; NOT the savings flow
        "portfolio": {
            "controllable_total": controllable,        # cash + taxable holdings
            "taxable_value": taxable_inv,
            "retirement_value": retirement_locked,
            "holdings": allocation,                    # taxable holdings only (what you'd rebalance)
            "top_concentration_pct": round(top_concentration, 3),
        },
        "allocation": {
            # % across CONTROLLABLE assets (cash + taxable). Excludes locked 401(k).
            "investable_base": investable_base,
            "current": alloc_current,
            "target": alloc_target,
        },
        "accounts": [
            {"name": _clean_acct(b["name"]), "type": b["type"],
             "role": _acct_role(b["name"], b["type"]), "balance": round(b["balance"], 2)}
            for b in balances
        ],
        "splitwise": {
            # + == owed to you, - == you owe. Settle receivables before counting
            # as spendable; surfaced separately, NOT folded into liquid cash.
            "net_balance_by_currency": sw_net,
            "your_expense_share_in_window": sw_share,
        },
        "profile": profile or {},
    }
