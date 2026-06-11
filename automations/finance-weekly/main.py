"""finance-weekly — weekly spend + balance digest from the local datastore.

Reads what finance-sync ingested (run finance-sync first / on a daily cron).
Sends a digest to Telegram. Finance data stays local; only the digest text
(category totals + balances) goes to your own Telegram chat.
"""
from __future__ import annotations

import json
import time

import config
import notify
import store

WINDOW_DAYS = 7


def build_summary(conn) -> dict:
    since = int(time.time()) - WINDOW_DAYS * 86400
    txns = store.transactions_since(conn, since)

    by_category: dict[str, float] = {}
    total_spend = 0.0
    total_income = 0.0
    for t in txns:
        amount = t["amount"]
        if amount < 0:
            spend = abs(amount)
            by_category[t["category"]] = round(
                by_category.get(t["category"], 0.0) + spend, 2
            )
            total_spend += spend
        else:
            total_income += amount

    balances = {row["name"]: round(row["balance"], 2)
                for row in store.latest_balances(conn)}

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window_days": WINDOW_DAYS,
        "total_spend": round(total_spend, 2),
        "total_income": round(total_income, 2),
        "txn_count": len(txns),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "balances": balances,
    }


def format_digest(s: dict) -> str:
    lines = [
        f"💰 Weekly Finance ({s['window_days']}d)",
        f"Spend: ${s['total_spend']:.2f}  |  Income: ${s['total_income']:.2f}  "
        f"|  {s['txn_count']} txns",
        "",
        "By category:",
    ]
    for cat, amt in s["by_category"].items():
        lines.append(f"  {cat:<14} ${amt:.2f}")
    lines += ["", "Balances:"]
    for name, bal in s["balances"].items():
        lines.append(f"  {name:<20} ${bal:.2f}")
    return "\n".join(lines)


def main() -> None:
    store.init_db()
    with store.connect() as conn:
        summary = build_summary(conn)

    config.ensure_runtime_dirs()
    (config.DATA_DIR / "finance-weekly.json").write_text(json.dumps(summary, indent=2))

    digest = format_digest(summary)
    print(digest)
    notify.send(digest)


if __name__ == "__main__":
    main()
