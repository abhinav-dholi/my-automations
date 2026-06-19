"""integrity — data-health checks that enforce the reconciling invariants.

The point: surface "totalling errors" instead of letting them hide inside an
aggregate. Each check returns issues with a severity so the UI can show a single
green/amber/red health status. Pure reads; no mutations.

Severities: "error" (an invariant is violated), "warn" (likely data problem to
review), "info" (FYI / source limitation).
"""
from __future__ import annotations

import time

import account_classify
import features as features_mod
import store

# Truly unclassified buckets (Credit is a handled bucket — one-time inflows).
_UNCLASSIFIED = {"Other", "Uncategorized"}
_LARGE = 200.0          # $ threshold for flagging an unclassified flow
_STALE_HOURS = 36.0     # account feed considered stale beyond this


def _issue(sev, code, msg, **extra):
    return {"severity": sev, "code": code, "message": msg, **extra}


def _eff(b, hb):
    if b["type"] == "investment":
        return max(b["balance"], hb.get(b["id"], 0.0))
    return b["balance"]


def check() -> list[dict]:
    issues: list[dict] = []
    f = features_mod.build()
    with store.connect() as c:
        bals = [dict(r) for r in store.latest_balances(c)]
        holds = [dict(r) for r in store.latest_holdings(c)]
        txns = [dict(r) for r in store.transactions_since(c, 0)]
        flags = store.txn_flags(c)
    txns = [t for t in txns if not flags.get(t["id"], {}).get("excluded")]  # confirmed dupes/errors out

    hb: dict = {}
    for h in holds:
        hb[h["account_id"]] = hb.get(h["account_id"], 0.0) + (h.get("market_value") or 0.0)

    # 1. Net worth invariant: must equal Σ effective account values − debts + the
    #    Splitwise receivable, and total assets (incl. receivable) ≥ net worth.
    sw_recv = f["net_worth_breakdown"].get("splitwise_receivable", 0.0)
    assets = sum(_eff(b, hb) for b in bals if _eff(b, hb) > 0) + max(sw_recv, 0.0)
    debts = sum(_eff(b, hb) for b in bals if _eff(b, hb) < 0) + min(sw_recv, 0.0)
    derived_nw = round(assets + debts, 2)
    if abs(derived_nw - round(f["net_worth"], 2)) > 1.0:
        issues.append(_issue("error", "networth_mismatch",
                             f"Net worth {f['net_worth']:,.2f} ≠ assets+debts {derived_nw:,.2f} "
                             f"(gap {f['net_worth']-derived_nw:,.2f})."))
    if derived_nw - assets > 1.0:
        issues.append(_issue("error", "assets_lt_networth",
                             f"Net worth {derived_nw:,.2f} exceeds total assets {assets:,.2f} — impossible."))

    # 2. Duplicate transaction ids (idempotency broke somewhere).
    seen, dups = set(), set()
    for t in txns:
        if t["id"] in seen:
            dups.add(t["id"])
        seen.add(t["id"])
    if dups:
        issues.append(_issue("error", "duplicate_txns", f"{len(dups)} duplicate transaction id(s)."))

    # 3. Large unclassified flows — money the model couldn't bucket (review/categorize).
    big = [t for t in txns if (t.get("category") in _UNCLASSIFIED) and abs(t["amount"]) >= _LARGE]
    if big:
        total = sum(abs(t["amount"]) for t in big)
        issues.append(_issue("warn", "unclassified_flows",
                             f"{len(big)} unclassified flow(s) ≥ ${_LARGE:,.0f} totaling ${total:,.0f} "
                             "(Other/Credit) — categorize so they don't distort analytics.",
                             items=[{"date": time.strftime('%Y-%m-%d', time.localtime(t["posted"])),
                                     "amount": round(t["amount"], 2),
                                     "description": (t.get("description") or "")[:50],
                                     "category": t.get("category")} for t in sorted(big, key=lambda x: -abs(x["amount"]))[:15]]))

    # 3b. Possible duplicate charges — DETERMINISTIC signature: same account, same
    #     normalized MERCHANT, same amount (to the cent), within DUP_DAYS. Requiring
    #     the merchant match removes coincidental same-amount charges at different
    #     places (e.g. two restaurants that both cost $27.09). Flagged, never
    #     auto-removed — some identical repeat charges are legitimate (a bag fee +
    #     ticket, two segments), which only the user can confirm.
    DUP_DAYS = 2
    dup_pairs = []
    spend = sorted((t for t in txns if t["amount"] < 0),
                   key=lambda x: (x["account_id"], round(abs(x["amount"]), 2), x["posted"]))
    for i in range(len(spend) - 1):
        a, b = spend[i], spend[i + 1]
        if (a["account_id"] == b["account_id"]
                and abs(abs(a["amount"]) - abs(b["amount"])) < 0.01
                and abs(a["amount"]) >= 1.0
                and store.merchant_key(a.get("description")) == store.merchant_key(b.get("description"))
                and store.merchant_key(a.get("description"))           # non-empty key
                and 0 <= (b["posted"] - a["posted"]) <= DUP_DAYS * 86400):
            gap = (b["posted"] - a["posted"]) // 86400
            dup_pairs.append({"date": time.strftime('%Y-%m-%d', time.localtime(b["posted"])),
                              "amount": round(b["amount"], 2),
                              "merchant": store.merchant_key(b.get("description")).title(),
                              "category": b.get("category"),
                              "gap_days": int(gap)})
    if dup_pairs:
        tot = sum(abs(d["amount"]) for d in dup_pairs)
        issues.append(_issue("warn", "possible_duplicates",
                             f"{len(dup_pairs)} possible duplicate charge(s) (~${tot:,.0f}) — same "
                             "account + same merchant + same amount, ≤2 days apart. Review and exclude real dupes.",
                             items=dup_pairs))

    # 4. Unpaired internal-transfer legs: a Transfer should have an opposite leg.
    #    A lone Transfer leg may be a mislabeled external flow (e.g. real income/spend).
    from collections import Counter
    signed = Counter()
    for t in txns:
        if t.get("category") == "Transfer":
            signed[round(abs(t["amount"]), 2)] += (1 if t["amount"] > 0 else -1)
    unpaired = sum(abs(v) for v in signed.values())
    if unpaired:
        issues.append(_issue("info", "unpaired_transfers",
                             f"{unpaired} internal-transfer leg(s) have no matching opposite leg — "
                             "often external (e.g. a Zelle from a person, an ACH in). Worth a glance."))

    # 5. Stale account feeds — if a feed lags > source window we lose history.
    now = time.time()
    stale = []
    for b in bals:
        try:
            import datetime as dt
            ts = dt.datetime.fromisoformat(b["as_of"])
            hrs = (dt.datetime.now(ts.tzinfo) - ts).total_seconds() / 3600
            if hrs > _STALE_HOURS:
                stale.append((b["name"], hrs))
        except (ValueError, KeyError):
            continue
    if stale:
        worst = max(h for _, h in stale)
        issues.append(_issue("warn", "stale_feeds",
                             f"{len(stale)} account feed(s) stale (worst {worst:.0f}h). "
                             "Run a sync — source history is capped (~90d), don't let gaps form."))

    # 6b. Stale/phantom holdings: a position the feed stopped reporting (or with no
    #     shares) whose value lingers and would inflate net worth. latest_holdings
    #     already drops these; surface them so the cause is visible.
    import datetime as _dt
    with store.connect() as c:
        raw = c.execute(
            """SELECT h.symbol, h.market_value mv, h.quantity qty, h.as_of, a.name nm, a.last_synced ls
               FROM holdings h JOIN accounts a ON a.id=h.account_id
               WHERE h.as_of=(SELECT MAX(as_of) FROM holdings
                              WHERE account_id=h.account_id AND symbol=h.symbol)""").fetchall()
    phantom = []
    for r in raw:
        mv = r["mv"] or 0
        if mv <= 1:
            continue
        try:
            stale = r["as_of"][:10] < (_dt.date.fromisoformat(r["ls"][:10]) - _dt.timedelta(days=1)).isoformat()
        except (ValueError, TypeError):
            stale = False
        if stale or (r["qty"] or 0) <= 0:
            phantom.append({"account": r["nm"][:24], "symbol": r["symbol"],
                            "value": round(mv, 2), "last_seen": r["as_of"][:10]})
    if phantom:
        tot = sum(p["value"] for p in phantom)
        issues.append(_issue("warn", "stale_holdings",
                             f"{len(phantom)} holding(s) worth ${tot:,.0f} no longer in the feed "
                             "(closed/vested-out) — excluded from net worth, shown here for transparency.",
                             items=phantom))

    # 6. Balance reconciliation (best-effort): over the balance-history window, do an
    #    account's transactions explain its balance change? Noisy (posting lag), so
    #    flagged only when the residual is material.
    bser = {}
    with store.connect() as c:
        for r in c.execute("SELECT account_id, balance, as_of FROM balances ORDER BY as_of"):
            bser.setdefault(r["account_id"], []).append((r["as_of"], r["balance"]))
    acct_name = {b["id"]: b["name"] for b in bals}
    for aid, pts in bser.items():
        if len(pts) < 2:
            continue
        (t0, b0), (t1, b1) = pts[0], pts[-1]
        flow = sum(t["amount"] for t in txns if t["account_id"] == aid and t0 <= time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(t["posted"])) <= t1)
        resid = (b1 - b0) - flow
        if abs(resid) > max(50.0, 0.05 * abs(b1 or 1)):
            issues.append(_issue("info", "balance_reconcile",
                                 f"{acct_name.get(aid,'?')[:24]}: balance moved {b1-b0:,.0f} but txns sum "
                                 f"{flow:,.0f} (residual {resid:,.0f}). Often posting-lag / pre-history."))
    return issues


def summary() -> dict:
    issues = check()
    sev = {s: sum(1 for i in issues if i["severity"] == s) for s in ("error", "warn", "info")}
    status = "error" if sev["error"] else ("warn" if sev["warn"] else "ok")
    return {"status": status, "counts": sev, "issues": issues}


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2, default=str))
