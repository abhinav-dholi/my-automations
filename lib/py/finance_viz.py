"""finance_viz — render a finance_chat chart spec to a PNG (for Telegram).

The UI draws chart specs with Plotly inline; Telegram can't, so this renders the
same catalog (and custom {type,x,y} specs) to a PNG with matplotlib. Data comes
from the same aggregates the chat agent sees. Returns PNG bytes or None.
"""
from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")            # headless: no display, safe under launchd
import matplotlib.pyplot as plt  # noqa: E402

import account_classify  # noqa: E402
import features as features_mod  # noqa: E402
import finance_rules  # noqa: E402
import store  # noqa: E402

_BG = "#0d1117"
_FG = "#e6edf3"
_PALETTE = ["#6ea8fe", "#22c55e", "#f59e0b", "#ef4444", "#a78bfa",
            "#2dd4bf", "#f472b6", "#94a3b8", "#eab308"]


def _new_fig(w=7.2, h=4.2):
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    for s in ax.spines.values():
        s.set_color("#30363d")
    ax.tick_params(colors=_FG, labelsize=9)
    ax.title.set_color(_FG)
    ax.yaxis.label.set_color(_FG)
    ax.xaxis.label.set_color(_FG)
    return fig, ax


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _pie(labels, values, title):
    labels = [l for l, v in zip(labels, values) if v and v > 0]
    values = [v for v in values if v and v > 0]
    if not values:
        return None
    fig, ax = _new_fig(6.4, 4.6)
    w, txts, auto = ax.pie(values, labels=labels, autopct="%1.0f%%",
                           colors=_PALETTE, startangle=90,
                           textprops={"color": _FG, "fontsize": 9})
    for a in auto:
        a.set_color("#0d1117"); a.set_fontsize(9)
    ax.set_title(title, color=_FG, fontsize=12)
    return _png(fig)


def _grouped_bars(cats, series, title, ylabel=""):
    """series: list of (name, [values], color)."""
    import numpy as np
    x = np.arange(len(cats))
    n = len(series)
    width = 0.8 / max(n, 1)
    fig, ax = _new_fig()
    for i, (name, vals, color) in enumerate(series):
        ax.bar(x + i * width, vals, width, label=name, color=color)
    ax.set_xticks(x + width * (n - 1) / 2)
    ax.set_xticklabels(cats, rotation=0)
    ax.set_ylabel(ylabel)
    ax.set_title(title, color=_FG, fontsize=12)
    leg = ax.legend(facecolor=_BG, edgecolor="#30363d", labelcolor=_FG, fontsize=9)
    leg.get_frame().set_alpha(0.9)
    return _png(fig)


def _barh(labels, values, title, xlabel="$"):
    if not values:
        return None
    fig, ax = _new_fig(7.2, max(3.0, 0.4 * len(labels) + 1.2))
    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in values]
    ax.barh(labels, values, color=colors)
    ax.invert_yaxis()
    ax.set_xlabel(xlabel)
    ax.set_title(title, color=_FG, fontsize=12)
    return _png(fig)


def _catalog(cid: str):
    f = features_mod.build()
    if cid == "spend_by_category":
        d = f.get("spend_by_category_monthly", {})
        items = list(d.items())[:8]
        return _pie([k for k, _ in items], [v for _, v in items], "Spend by category (typical month)")
    if cid == "monthly_income_vs_spend":
        s = f.get("monthly_series", {})
        xs = sorted(s)
        if not xs:
            return None
        return _grouped_bars(
            xs, [("Income", [s[m]["income"] for m in xs], "#22c55e"),
                 ("Spend", [s[m]["spend"] for m in xs], "#ef4444")],
            "Income vs spend by month", "$")
    if cid == "allocation_vs_target":
        cur, tgt = f["allocation"]["current"], f["allocation"]["target"]
        keys = ["equity", "bonds", "cash"]
        return _grouped_bars(
            keys, [("Current", [cur.get(k, 0) * 100 for k in keys], "#6ea8fe"),
                   ("Target", [tgt.get(k, 0) * 100 for k in keys], "#22c55e")],
            "Allocation vs target", "%")
    if cid == "net_worth_composition":
        nb = f["net_worth_breakdown"]
        return _pie(["Liquid cash", "Taxable invest", "Retirement"],
                    [nb["liquid_cash"], nb["taxable_investments"], nb["retirement_locked"]],
                    "Net worth composition")
    if cid == "holdings_mix":
        with store.connect() as c:
            holds = [dict(r) for r in store.latest_holdings(c)]
        agg: dict = {}
        for h in holds:
            agg[h["symbol"]] = agg.get(h["symbol"], 0) + (h.get("market_value") or 0)
        items = sorted(agg.items(), key=lambda kv: -kv[1])[:8]
        return _pie([k for k, _ in items], [v for _, v in items], "Holdings mix")
    if cid == "top_merchants":
        import time
        since = int(time.time()) - 90 * 86400
        with store.connect() as c:
            txns = [dict(r) for r in store.transactions_since(c, since)]
        spend: dict = {}
        for t in txns:
            if (t["amount"] or 0) < 0 and t.get("category") not in finance_rules.NON_SPEND:
                spend[store.merchant_key(t["description"])] = \
                    spend.get(store.merchant_key(t["description"]), 0) + abs(t["amount"])
        items = sorted(spend.items(), key=lambda kv: -kv[1])[:10]
        return _barh([k.title() for k, _ in items], [v for _, v in items],
                     "Top merchants (90d)")
    if cid == "performance_vs_benchmark":
        with store.connect() as c:
            rows = c.execute("SELECT symbol, close, date FROM market_prices ORDER BY date").fetchall()
        series: dict = {}
        for r in rows:
            s = series.setdefault(r["symbol"], {"d": [], "c": []})
            s["d"].append(r["date"]); s["c"].append(r["close"])
        if not series:
            return None
        fig, ax = _new_fig()
        for i, (sym, s) in enumerate(series.items()):
            base = s["c"][0] if s["c"] else None
            if base:
                ax.plot(s["d"], [c / base * 100 for c in s["c"]], marker="o",
                        label=sym, color=_PALETTE[i % len(_PALETTE)])
        ax.set_ylabel("Rebased (100=start)")
        ax.set_title("Your holdings vs benchmarks", color=_FG, fontsize=12)
        ax.tick_params(axis="x", rotation=45)
        leg = ax.legend(facecolor=_BG, edgecolor="#30363d", labelcolor=_FG, fontsize=8)
        leg.get_frame().set_alpha(0.9)
        return _png(fig)
    if cid == "accounts_by_kind":
        with store.connect() as c:
            bals = [dict(r) for r in store.latest_balances(c)]
        agg: dict = {}
        for b in bals:
            if b["balance"] > 0:
                kind = account_classify.label(b.get("subtype")) or (b["type"] or "").title()
                agg[kind] = agg.get(kind, 0) + b["balance"]
        return _pie(list(agg), list(agg.values()), "Assets by kind")
    if cid == "splitwise_by_friend":
        with store.connect() as c:
            sw = [dict(r) for r in store.splitwise_balances_latest(c)]
        sw = sorted([x for x in sw if abs(x["amount"]) > 0.5], key=lambda x: x["amount"])
        return _barh([x["friend_name"][:18] for x in sw], [x["amount"] for x in sw],
                     "Splitwise by friend (+ owed to you)")
    return None


def _custom(spec: dict):
    typ = spec.get("type")
    title = spec.get("title", "")
    if typ == "pie":
        return _pie(spec.get("labels", []), spec.get("y", []), title)
    if typ in ("bar", "line"):
        x, y = spec.get("x", []), spec.get("y", [])
        if not y:
            return None
        fig, ax = _new_fig()
        if typ == "bar":
            ax.bar([str(v) for v in x], y, color="#6ea8fe")
        else:
            ax.plot([str(v) for v in x], y, marker="o", color="#6ea8fe")
        ax.set_title(title, color=_FG, fontsize=12)
        ax.tick_params(axis="x", rotation=45)
        return _png(fig)
    return None


def render(spec: dict) -> bytes | None:
    """Render a chart spec (catalog id or custom) to PNG bytes, or None."""
    try:
        return _catalog(spec["id"]) if spec.get("id") else _custom(spec)
    except Exception:
        return None
