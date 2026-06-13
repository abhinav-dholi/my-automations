"""finance-summary — current-status snapshot to Telegram.

Net worth, account balances by type, portfolio, Splitwise position, and the
typical monthly cashflow. Reads the local DB; sends a digest and writes
data/finance-summary.json for the UI.
"""
from __future__ import annotations

import json
import time

import config
import features as features_mod
import notify
import store

LIQUID = {"checking", "savings"}


def build() -> dict:
    f = features_mod.build()
    with store.connect() as conn:
        balances = [dict(r) for r in store.latest_balances(conn)]
        splitwise = [dict(r) for r in store.splitwise_balances_latest(conn)]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "net_worth": f["net_worth"],
        "cashflow": f["cashflow"],
        "resilience": f["resilience"],
        "portfolio": f["portfolio"],
        "splitwise_net": f["splitwise"]["net_balance_by_currency"],
        "balances": balances,
        "splitwise": splitwise,
    }


def format_text(s: dict) -> str:
    bal = s["balances"]
    liquid = sum(b["balance"] for b in bal if b["type"] in LIQUID)
    credit = sum(b["balance"] for b in bal if b["type"] == "credit")
    invest = s["portfolio"]["invested_total"]
    cf = s["cashflow"]

    lines = [
        "📊 Finance Summary",
        f"Net worth: ${s['net_worth']:,.0f}",
        "",
        f"💵 Liquid cash: ${liquid:,.0f}",
    ]
    for b in sorted([b for b in bal if b["type"] in LIQUID], key=lambda x: -x["balance"]):
        lines.append(f"   {b['name'][:30]}  ${b['balance']:,.0f}")
    if credit:
        lines.append(f"💳 Credit owed: ${abs(credit):,.0f}")

    if invest:
        holds = s["portfolio"].get("holdings") or []
        top = holds[0] if holds else None
        label = (top.get("name") or top.get("symbol")) if top else ""
        conc = f" (top: {label} {top['pct_of_portfolio']*100:.0f}%)" if top else ""
        lines += ["", f"📈 Portfolio: ${invest:,.0f}{conc}"]

    net = sum(s["splitwise_net"].values()) if s["splitwise_net"] else 0
    if s["splitwise"]:
        verb = "owed to you" if net >= 0 else "you owe"
        lines += ["", f"🤝 Splitwise: ${abs(net):,.0f} net {verb}"]
        # Per-friend split: + = they owe you, - = you owe them.
        for b in sorted(s["splitwise"], key=lambda x: -x["amount"]):
            if round(b["amount"], 2) == 0:
                continue
            sign = "＋" if b["amount"] >= 0 else "−"
            who = "owes you" if b["amount"] >= 0 else "you owe"
            lines.append(f"   {sign}${abs(b['amount']):,.0f}  {b['friend_name'][:24]} ({who})")

    em = s["resilience"].get("emergency_fund_months")
    lines += [
        "",
        f"📅 Typical month: income ${cf['monthly_income']:,.0f} / "
        f"spend ${cf['monthly_spend']:,.0f} → save ${cf['monthly_investable_estimate']:,.0f}",
    ]
    if em is not None:
        lines.append(f"🛟 Emergency fund: {em:.1f} months")
    return "\n".join(lines)


def main() -> None:
    store.init_db()
    s = build()
    config.ensure_runtime_dirs()
    (config.DATA_DIR / "finance-summary.json").write_text(json.dumps(s, indent=2, default=str))
    text = format_text(s)
    print(text)
    notify.send(text)


if __name__ == "__main__":
    main()
