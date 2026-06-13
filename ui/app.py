"""my-automations — local finance & automation dashboard.

Launch: `python cli/auto ui` (binds 127.0.0.1 only). A view layer over the same
lib/py the CLI uses: manifests, run logs, the finance DB, and the `auto` runner.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib" / "py"))

import config  # noqa: E402
import finance_rules  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import runlog  # noqa: E402
import store  # noqa: E402
import yaml  # noqa: E402

st.set_page_config(page_title="my-automations", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

# --- styling ------------------------------------------------------------
st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 2rem; max-width: 1400px;}
  [data-testid="stMetric"] {
      background: #161b22; border: 1px solid #30363d; border-radius: 12px;
      padding: 14px 16px;
  }
  [data-testid="stMetricLabel"] {opacity: .7; font-size: .8rem;}
  h1, h2, h3 {letter-spacing: -0.01em;}
  .stPlotlyChart {background: #0d1117; border: 1px solid #21262d; border-radius: 12px; padding: 6px;}
</style>
""", unsafe_allow_html=True)

PLOT_TEMPLATE = "plotly_dark"
ACCENT = "#3b82f6"
PALETTE = px.colors.qualitative.Set2

# Category choices for manual edits (rule categories + money-movement + misc).
CATEGORIES = sorted(set(
    [c for c, _ in finance_rules.CATEGORY_RULES]
    + list(finance_rules.NON_SPEND) + ["Other", "Uncategorized"]
))


# --- data access (cached) ----------------------------------------------

@st.cache_data(ttl=30)
def get_features() -> dict:
    import features
    return features.build(profile=_profile(), lookback_days=90)


def _profile() -> dict:
    p = config.DATA_DIR / "profile.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


@st.cache_data(ttl=30)
def load_finance_db() -> dict:
    """Pull everything the dashboard needs in one cached snapshot."""
    import time
    since = int(time.time()) - 400 * 86400
    since_iso = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(since))
    with store.connect() as c:
        txns = [dict(r) for r in store.transactions_since(c, since)]
        balances = [dict(r) for r in store.latest_balances(c)]
        holdings = [dict(r) for r in store.latest_holdings(c)]
        bal_series = [dict(r) for r in store.balance_series(c)]
        hold_series = [dict(r) for r in store.holdings_series(c)]
        sw = [dict(r) for r in store.splitwise_balances_latest(c)]
        sw_share = store.splitwise_share_since(c, since_iso)
    return dict(txns=txns, balances=balances, holdings=holdings,
                bal_series=bal_series, hold_series=hold_series,
                splitwise=sw, sw_share=sw_share)


def _load_json(name: str):
    p = config.DATA_DIR / name
    return json.loads(p.read_text()) if p.exists() else None


def _has_data() -> bool:
    return (config.DATA_DIR / "finance.db").exists()


def _money(x) -> str:
    try:
        return f"${x:,.0f}"
    except Exception:
        return "—"


def _md(text) -> str:
    """Escape chars Streamlit markdown would misread: $ (LaTeX math) and ~ (a
    pair of ~ renders as strikethrough, e.g. '~$288 … ~$5,941')."""
    return str(text or "").replace("$", "\\$").replace("~", "\\~")


# --- shared chart helpers ----------------------------------------------

def donut(labels, values, title=""):
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.62,
                           marker=dict(colors=PALETTE)))
    fig.update_traces(textinfo="percent+label", textposition="inside")
    fig.update_layout(template=PLOT_TEMPLATE, title=title, showlegend=False,
                      height=320, margin=dict(t=40, b=10, l=10, r=10))
    return fig


# --- pages --------------------------------------------------------------

def _refresh_all():
    for step, label in [("finance-sync", "bank"), ("splitwise-sync", "Splitwise"),
                        ("finance-categorize", "AI-categorize"), ("market-data-sync", "prices/macro")]:
        _run_cli(["cli/auto", "run", step], f"Refreshing {label}…")
    st.cache_data.clear()


def page_overview():
    head, btn = st.columns([4, 1])
    head.title("💰 My Money")
    if not _has_data():
        st.info("No finance data yet. Run `finance-sync` first."); return
    if btn.button("🔄 Refresh all", help="Pull bank + Splitwise, AI-categorize, update prices/macro"):
        _refresh_all(); st.rerun()

    f = get_features()
    db = load_finance_db()
    cf, res = f["cashflow"], f["resilience"]
    nb = f["net_worth_breakdown"]
    n_mo = f.get("months_analyzed", 0)

    # ── Top line: net worth + the money buckets ──
    c = st.columns(5)
    c[0].metric("Net worth", _money(f["net_worth"]))
    c[1].metric("💵 Liquid cash", _money(nb["liquid_cash"]),
                help="Checking + savings. Part of this is your emergency reserve, not free to invest.")
    c[2].metric("📈 Taxable invest", _money(nb["taxable_investments"]))
    c[3].metric("🔒 Retirement (locked)", _money(nb["retirement_locked"]), help="401(k)/HSA — you won't touch it.")
    c[4].metric("💳 Card debt", _money(abs(nb["credit_debt"])))

    # Investable surplus = cash beyond the emergency reserve (what you can deploy).
    surplus = f.get("investable_surplus", 0)
    em = res.get("emergency_fund_months")
    tgt = res.get("emergency_fund_target_months", 6)
    if surplus > 0:
        st.success(f"✅ Investable surplus: **{_money(surplus)}** — cash beyond your "
                   f"{tgt:.0f}-month emergency reserve, free to deploy.")
    else:
        gap = max(0, (res.get("emergency_fund_target", 0) - nb["liquid_cash"]))
        st.warning(f"🪙 Investable surplus: **$0** — your liquid cash is still building the "
                   f"{tgt:.0f}-month emergency reserve ({em} of {tgt:.0f} months; ~{_money(gap)} to go). "
                   "Finish the reserve before deploying cash into investments.")

    # ── Monthly money flow (reconciled) ──
    st.divider()
    st.subheader("Monthly money flow")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Income", _money(cf["total_income_est"]),
              help=f"Salary {_money(cf['monthly_income'])} ({cf.get('pay_cadence','')}) + "
                   f"non-salary/RSU ~{_money(cf['est_other_income'])}.")
    m2.metric("Spend", _money(cf["monthly_spend"]), help=f"Typical month {_money(cf.get('typical_month_spend'))} (median).")
    m3.metric("Saved", _money(cf["monthly_to_savings"]), help="Net cash moved into savings/brokerage.")
    m4.metric("Savings rate", f"{cf['savings_rate']*100:.0f}%", help="Saved ÷ (saved + spent). After-tax; 401(k) not modeled.")
    st.caption(f"Of every dollar you deploy, **{cf['savings_rate']*100:.0f}% is saved**. Salary is "
               f"{cf.get('pay_cadence','')}; ~{_money(cf['est_other_income'])}/mo is non-salary income (RSU/bonus). "
               f"Emergency fund: **{res.get('emergency_fund_months','—')} months** of spend in liquid cash."
               + ("  ⚠️ only %d complete month(s) of history — firms up over time." % n_mo if n_mo < 2 else ""))

    # ── Net worth composition + allocation vs target ──
    left, right = st.columns(2)
    with left:
        st.subheader("Where your money is")
        comp = [("Liquid cash", nb["liquid_cash"]), ("Taxable invest", nb["taxable_investments"]),
                ("Retirement (locked)", nb["retirement_locked"])]
        comp = [(l, v) for l, v in comp if v > 0]
        if comp:
            st.plotly_chart(donut([l for l, _ in comp], [v for _, v in comp]), use_container_width=True)
    with right:
        st.subheader("Investable allocation vs target")
        st.caption("Cash + taxable holdings you control (excludes locked 401k).")
        cur, tgt = f["allocation"]["current"], f["allocation"]["target"]
        keys = ["equity", "bonds", "cash"]
        fig = go.Figure([
            go.Bar(name="Current", x=keys, y=[cur.get(k, 0)*100 for k in keys], marker_color=ACCENT),
            go.Bar(name="Target", x=keys, y=[tgt.get(k, 0)*100 for k in keys], marker_color="#22c55e"),
        ])
        fig.update_layout(template=PLOT_TEMPLATE, barmode="group", height=300, yaxis_title="%",
                          margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

    # ── Income vs spend by month + spend by category ──
    left2, right2 = st.columns(2)
    with left2:
        st.subheader("Income vs spend by month")
        series = f.get("monthly_series", {})
        if series:
            sdf = pd.DataFrame([{"month": m, "Income": v["income"], "Spend": v["spend"]}
                                for m, v in sorted(series.items())])
            bar = go.Figure([
                go.Bar(name="Income", x=sdf["month"], y=sdf["Income"], marker_color="#22c55e"),
                go.Bar(name="Spend", x=sdf["month"], y=sdf["Spend"], marker_color="#ef4444"),
            ])
            bar.update_layout(template=PLOT_TEMPLATE, barmode="group", height=300,
                              margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None, yaxis_title=None)
            st.plotly_chart(bar, use_container_width=True)
    with right2:
        st.subheader("Spend by category (typical month)")
        sbc = f.get("spend_by_category_monthly", {})
        if sbc:
            st.plotly_chart(donut(list(sbc)[:8], [sbc[k] for k in list(sbc)[:8]]), use_container_width=True)

    # ── Splitwise + concentration callout ──
    swn = sum(f["splitwise"]["net_balance_by_currency"].values()) if f["splitwise"]["net_balance_by_currency"] else 0
    conc = f["portfolio"]["top_concentration_pct"]
    s1, s2 = st.columns(2)
    s1.metric("🤝 Splitwise net", _money(swn), help="+ owed to you")
    s2.metric("⚠️ Top single-stock", f"{conc*100:.0f}% of investable",
              help="Largest single holding as % of cash + taxable investments.")
    st.caption(f"Updated {f.get('generated_at','')[:16].replace('T',' ')}. Tabs at left break down "
               "Spending, Accounts, Investments, Splitwise, Market, and the AI Analysis / Council.")


def page_spending():
    st.title("Spending")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    df = pd.DataFrame(db["txns"])
    if df.empty:
        st.info("No transactions."); return
    df["date"] = pd.to_datetime(df["posted"], unit="s")

    # Date-range picker.
    import time as _t
    presets = {"30 days": 30, "90 days": 90, "6 months": 180, "1 year": 365, "All": 3650}
    rc1, rc2 = st.columns([3, 2])
    choice = rc1.radio("Range", list(presets) + ["Custom"], horizontal=True, index=1)
    now = int(_t.time())
    if choice == "Custom":
        dmin = df["date"].min().date()
        dmax = df["date"].max().date()
        rng = rc2.date_input("Custom range", value=(dmin, dmax), min_value=dmin, max_value=dmax)
        if isinstance(rng, tuple) and len(rng) == 2:
            start = int(pd.Timestamp(rng[0]).timestamp()); end = int(pd.Timestamp(rng[1]).timestamp()) + 86399
        else:
            start, end = now - 90 * 86400, now
    else:
        start, end = now - presets[choice] * 86400, now

    df = df[(df["posted"] >= start) & (df["posted"] <= end)]

    # Spend = negative, excluding money-movement categories.
    spend = df[(df["amount"] < 0) & (~df["category"].isin(finance_rules.NON_SPEND))].copy()
    spend["spend"] = spend["amount"].abs()

    c = st.columns(4)
    c[0].metric("Total spend", _money(spend["spend"].sum()))
    c[1].metric("Income", _money(df[df["category"] == "Income"]["amount"].sum()))
    c[2].metric("Transactions", len(spend))
    c[3].metric("Top category", spend.groupby("category")["spend"].sum().idxmax() if len(spend) else "—")

    left, right = st.columns(2)
    with left:
        st.subheader("By category")
        bycat = spend.groupby("category")["spend"].sum().sort_values(ascending=True)
        fig = px.bar(bycat, orientation="h", template=PLOT_TEMPLATE,
                     color_discrete_sequence=[ACCENT])
        fig.update_layout(height=360, showlegend=False, margin=dict(t=20, b=10, l=10, r=10),
                          xaxis_title=None, yaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.subheader("Category share")
        bycat2 = spend.groupby("category")["spend"].sum()
        st.plotly_chart(donut(list(bycat2.index), list(bycat2.values)), use_container_width=True)

    st.subheader("Spend over time (weekly)")
    wk = spend.set_index("date").resample("W")["spend"].sum().reset_index()
    fig = px.bar(wk, x="date", y="spend", template=PLOT_TEMPLATE,
                 color_discrete_sequence=[ACCENT])
    fig.update_layout(height=280, margin=dict(t=20, b=10, l=10, r=10),
                      xaxis_title=None, yaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Transactions — edit categories")
    ec1, ec2 = st.columns([3, 1])
    ec1.caption("Change a category and Save — manual edits override everything, are "
                "remembered, and re-tag matching transactions. Or let AI categorize the rest.")
    if ec2.button("🤖 AI-categorize Other"):
        with st.spinner("Claude categorizing…"):
            res = subprocess.run([sys.executable, "cli/auto", "run", "finance-categorize"],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True)
        (st.success if res.returncode == 0 else st.error)((res.stdout or res.stderr).strip()[-200:])
        st.cache_data.clear(); st.rerun()
    allt = df.copy().sort_values("date", ascending=False)

    fc1, fc2 = st.columns([2, 3])
    cats = sorted(allt["category"].unique())
    pick = fc1.multiselect("Filter category", cats, default=cats)
    q = fc2.text_input("Search description", "")
    view = allt[allt["category"].isin(pick)]
    if q:
        view = view[view["description"].str.contains(q, case=False, na=False)]

    editor_df = view[["id", "date", "description", "amount", "category"]].reset_index(drop=True)
    edited = st.data_editor(
        editor_df, hide_index=True, use_container_width=True, height=460,
        key="txn_editor",
        column_config={
            "id": None,
            "date": st.column_config.DatetimeColumn("Date", disabled=True, format="YYYY-MM-DD"),
            "description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "amount": st.column_config.NumberColumn("Amount", disabled=True, format="$%.2f"),
            "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
        },
    )
    if st.button("💾 Save category changes", type="primary"):
        orig = dict(zip(editor_df["id"], editor_df["category"]))
        new = dict(zip(edited["id"], edited["category"]))
        changed = {i: c for i, c in new.items() if orig.get(i) != c}
        if not changed:
            st.info("No changes to save.")
        else:
            desc_by_id = dict(zip(view["id"], view["description"]))
            retagged = 0
            with store.connect() as conn:
                for tid, cat in changed.items():
                    store.set_manual_category(conn, tid, cat)        # this txn (wins always)
                    retagged += store.learn_category(conn, desc_by_id.get(tid, ""), cat)  # learn + re-tag similar
            st.success(f"Saved {len(changed)} edit(s); learned the merchant(s) and "
                       f"re-tagged {retagged} matching transaction(s). Future syncs apply this automatically.")
            st.cache_data.clear()
            st.rerun()


def page_accounts():
    st.title("Accounts")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    bal = pd.DataFrame(db["balances"])
    if bal.empty:
        st.info("No accounts."); return

    assets = bal[bal["balance"] > 0]["balance"].sum()
    debts = bal[bal["balance"] < 0]["balance"].sum()
    c = st.columns(3)
    c[0].metric("Total assets", _money(assets))
    c[1].metric("Total debt", _money(debts))
    c[2].metric("Net", _money(assets + debts))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Balances by account")
        b = bal.sort_values("balance")
        fig = px.bar(b, x="balance", y="name", orientation="h", template=PLOT_TEMPLATE,
                     color="balance", color_continuous_scale=["#ef4444", "#22c55e"])
        fig.update_layout(height=400, margin=dict(t=20, b=10, l=10, r=10),
                          xaxis_title=None, yaxis_title=None, coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.subheader("By account type")
        bytype = bal.groupby("type")["balance"].sum().abs()
        st.plotly_chart(donut(list(bytype.index), list(bytype.values)), use_container_width=True)

    st.dataframe(bal[["name", "type", "balance"]].sort_values("balance", ascending=False),
                 use_container_width=True, hide_index=True)


def page_investments():
    st.title("Investments")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    h = pd.DataFrame(db["holdings"])
    if h.empty:
        st.info("No holdings synced. (SimpleFIN exposes these for investment accounts.)"); return

    # Only count holdings in funded investment accounts (balance > 0). Excludes
    # unvested / $0-balance accounts (e.g. equity awards) that overstate value.
    bal = pd.DataFrame(db["balances"])
    funded = set(bal[(bal["type"] == "investment") & (bal["balance"] > 0)]["id"]) if not bal.empty else set()
    excluded = h[~h["account_id"].isin(funded)]
    h = h[h["account_id"].isin(funded)]
    if h.empty:
        st.info("No funded investment holdings. (Unvested/equity-award holdings are excluded.)"); return

    total_val = h["market_value"].sum()
    total_cost = h["cost_basis"].sum()
    gain = total_val - total_cost
    c = st.columns(3)
    c[0].metric("Portfolio value", _money(total_val))
    c[1].metric("Cost basis", _money(total_cost))
    c[2].metric("Unrealized gain", _money(gain),
                f"{(gain/total_cost*100):.1f}%" if total_cost else None)

    left, right = st.columns([2, 3])
    with left:
        st.subheader("Allocation")
        st.plotly_chart(donut(list(h["symbol"]), list(h["market_value"])), use_container_width=True)
    with right:
        st.subheader("Holdings")
        h2 = h.copy()
        gain_pct = ((h2["market_value"] - h2["cost_basis"]) /
                    h2["cost_basis"].replace(0, pd.NA) * 100)
        h2["gain %"] = pd.to_numeric(gain_pct, errors="coerce").round(1)
        st.dataframe(
            h2[["symbol", "quantity", "market_value", "cost_basis", "gain %"]],
            use_container_width=True, hide_index=True,
        )
    if not excluded.empty:
        st.caption(f"Excluded {len(excluded)} unvested/$0-balance holding(s) "
                   f"(${excluded['market_value'].sum():,.0f}) from invested totals.")
    st.caption("Concentration in a single holding is a key risk the analysis flags.")


def page_splitwise():
    st.title("Splitwise")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    sw = pd.DataFrame(db["splitwise"])
    if sw.empty:
        st.info("No Splitwise data. Run `splitwise-sync`."); return

    net = sw["amount"].sum()
    owed = sw[sw["amount"] > 0]["amount"].sum()
    owe = sw[sw["amount"] < 0]["amount"].sum()
    c = st.columns(3)
    c[0].metric("Net position", _money(net), "owed to you" if net >= 0 else "you owe")
    c[1].metric("Owed to you", _money(owed))
    c[2].metric("You owe", _money(abs(owe)))

    st.subheader("By friend (＋ owed to you)")
    s = sw.sort_values("amount")
    fig = px.bar(s, x="amount", y="friend_name", orientation="h", template=PLOT_TEMPLATE,
                 color="amount", color_continuous_scale=["#ef4444", "#22c55e"])
    fig.update_layout(height=360, margin=dict(t=20, b=10, l=10, r=10),
                      xaxis_title=None, yaxis_title=None, coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Your share of shared expenses (90d): {_money(db['sw_share'])}")


def page_analysis():
    st.title("AI Analysis")
    st.caption("Claude-orchestrated, over anonymized aggregates. Informational — not licensed financial advice.")
    a = _load_json("finance-analyze.json") or _load_json("finance-analysis.json")
    if st.button("▶ Run analysis now (~2 min)", type="primary"):
        with st.spinner("Claude analyzing…"):
            res = subprocess.run([sys.executable, "cli/auto", "run", "finance-analyze"],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True)
        (st.success if res.returncode == 0 else st.error)(f"exited {res.returncode}")
        st.cache_data.clear()
        a = _load_json("finance-analyze.json")

    if not a:
        st.info("No analysis yet. Click Run, or trigger /analyze in Telegram."); return

    if a.get("summary"):
        st.subheader("Summary")
        st.markdown(_md(a["summary"]))
    if a.get("monthly_investable") is not None:
        st.metric("Estimated monthly investable", _money(a["monthly_investable"]))

    cur, tgt = a.get("allocation_current"), a.get("allocation_target")
    if cur or tgt:
        st.subheader("Allocation: current vs target")
        keys = sorted(set(cur or {}) | set(tgt or {}))
        fig = go.Figure([
            go.Bar(name="Current", x=keys, y=[(cur or {}).get(k, 0)*100 for k in keys], marker_color=ACCENT),
            go.Bar(name="Target", x=keys, y=[(tgt or {}).get(k, 0)*100 for k in keys], marker_color="#22c55e"),
        ])
        fig.update_layout(template=PLOT_TEMPLATE, barmode="group", height=300,
                          yaxis_title="%", margin=dict(t=20, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

    if a.get("actions"):
        st.subheader("Prioritized actions")
        for act in sorted(a["actions"], key=lambda x: x.get("priority", 99)):
            st.markdown(f"**{act.get('priority','?')}. {_md(act.get('action',''))}**")
            st.caption(_md(act.get("rationale", "")))


def _run_cli(args: list[str], spinner: str) -> str:
    with st.spinner(spinner):
        res = subprocess.run([sys.executable] + args, cwd=str(REPO_ROOT),
                             capture_output=True, text=True)
    return (res.stdout or res.stderr or "").strip()


def page_market():
    st.title("Market")
    if st.button("🔄 Refresh news + prices"):
        _run_cli(["cli/auto", "run", "market-watch"], "Scouting the web…")
        _run_cli(["cli/auto", "run", "market-data-sync"], "Fetching prices + macro…")
        st.cache_data.clear(); st.rerun()
    brief = _load_json("market-brief.json")
    if not brief:
        st.info("No market brief yet. Run `market-watch` / `market-data-sync`."); return

    macro = brief.get("macro", {})
    if macro:
        cols = st.columns(len(macro))
        for col, (k, v) in zip(cols, macro.items()):
            col.metric(v.get("label", k), f"{v.get('value')}")

    prices = brief.get("prices", {})
    if prices:
        st.subheader("Prices")
        st.dataframe([{"symbol": k, "price": v} for k, v in sorted(prices.items())],
                     hide_index=True, use_container_width=True)

    news = brief.get("news", [])
    st.subheader(f"Market news ({len(news)})")
    for n in news:
        url = n.get("url", "")
        link = f" · [source]({url})" if url else ""
        st.markdown(f"**{_md(n.get('headline',''))}**  \n{_md(n.get('summary',''))}  \n"
                    f"<span style='opacity:.6'>{_md(n.get('source',''))}{link} · "
                    f"{_md(n.get('relevance',''))}</span>", unsafe_allow_html=True)
    st.caption(f"Brief generated {brief.get('generated_at','')}")


def _stream_council():
    """Launch the council and stream its live debate progress into the UI."""
    import time as _t
    prog = config.DATA_DIR / "council_progress.jsonl"
    try:
        prog.unlink()          # clear stale events; orchestrator recreates it
    except OSError:
        pass
    proc = subprocess.Popen([sys.executable, "cli/auto", "run", "finance-council"],
                            cwd=str(REPO_ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = st.status("🏛 Convening the council…", expanded=True)
    seen, done = 0, False
    deadline = _t.time() + 360
    while not done and _t.time() < deadline:
        try:
            lines = prog.read_text().splitlines()
        except OSError:
            lines = []
        for ln in lines[seen:]:
            seen += 1
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            e = ev.get("event")
            if e == "start":
                status.write(f"Panel: {', '.join(ev.get('personas', []))}")
            elif e == "phase":
                status.update(label=f"🏛 {ev.get('label','')}")
                status.write(f"**— {ev.get('label','')}**")
            elif e == "round1":
                status.write(f"🗣 **{ev.get('persona','')}** — _{(ev.get('stance') or '')[:140]}_")
            elif e == "round2":
                ds = ev.get("disagreements", []) or []
                whom = ", ".join(d.get("with", "") for d in ds[:2])
                status.write(f"⚔️ **{ev.get('persona','')}** pushes back"
                             + (f" on {whom}" if whom else ""))
            elif e == "done":
                done = True
        if proc.poll() is not None and seen >= len(lines):
            done = True
        if not done:
            _t.sleep(1)
    proc.wait()
    status.update(label="✅ Council complete — verdict below", state="complete")


def page_council():
    st.title("Investment Council")
    st.caption("Eight investing philosophies debate your portfolio over live market data; "
               "a fiduciary mediator synthesizes a balanced, informational view — not biased "
               "to any one school. Not licensed financial advice.")

    c1, c2 = st.columns([1, 2])
    if c1.button("▶ Run council (~2–3 min)", type="primary"):
        _stream_council()
        st.cache_data.clear(); st.rerun()

    with st.expander("🧠 Ask a single expert"):
        sys.path.insert(0, str(config.LIB_PY))
        import council as _council
        names = {k: v["name"] for k, v in _council.PERSONAS.items()}
        key = st.selectbox("Expert", list(names), format_func=lambda k: names[k])
        q = st.text_input("Question (optional)")
        if st.button("Ask"):
            args = ["lib/py/council_cli.py", "expert", key] + (["--q", q] if q else [])
            st.markdown(_md(_run_cli(args, f"Consulting {names[key]}…")))

    c = _load_json("finance-council.json")
    if not c:
        st.info("No council run yet. Click Run, or /finance council in Telegram."); return

    if c.get("summary"):
        st.subheader("Mediator synthesis")
        st.markdown(_md(c["summary"]))
    if c.get("whats_changed"):
        st.info("🔄 Since last council: " + _md(c["whats_changed"]))

    if c.get("consensus"):
        st.subheader("Where the panel agrees")
        for x in c["consensus"]:
            st.markdown(f"- {_md(x)}")

    if c.get("actions"):
        st.subheader("Prioritized actions (balanced)")
        for a in sorted(c["actions"], key=lambda x: x.get("priority", 99)):
            sup = ", ".join(a.get("supported_by", []) or [])
            opp = ", ".join(a.get("opposed_by", []) or [])
            st.markdown(f"**{a.get('priority','?')}. {_md(a.get('action',''))}**  "
                        f"·  _{a.get('confidence','')}_")
            st.caption(_md(a.get("rationale", "")) +
                       (f"  \n👍 {sup}" if sup else "") + (f"   👎 {opp}" if opp else ""))

    if c.get("tradeoffs"):
        st.subheader("Key tradeoffs (both sides)")
        for t in c["tradeoffs"]:
            with st.expander(_md(t.get("issue", ""))):
                st.markdown(f"**One side:** {_md(t.get('one_side',''))}")
                st.markdown(f"**Other side:** {_md(t.get('other_side',''))}")
                if t.get("what_would_tip_it"):
                    st.markdown(f"**What would tip it:** {_md(t['what_would_tip_it'])}")
                if t.get("your_context"):
                    st.caption("Your context: " + _md(t["your_context"]))

    if c.get("watch_items"):
        st.subheader("Watch")
        for w in c["watch_items"]:
            st.markdown(f"- {_md(w)}")

    st.subheader("The panel")
    for p in c.get("personas", []):
        with st.expander(f"{p.get('name','')} — {(_md(p.get('stance','')) or '')[:70]}"):
            if p.get("recommendation"):
                st.markdown("**Recommendation:** " + _md(p["recommendation"]))
            if p.get("pros"):
                st.markdown("**Pros:** " + "; ".join(_md(x) for x in p["pros"]))
            if p.get("cons"):
                st.markdown("**Cons:** " + "; ".join(_md(x) for x in p["cons"]))
            for dd in p.get("disagreements", []):
                st.markdown(f"- ⚔️ vs {_md(dd.get('with',''))}: {_md(dd.get('point',''))}")
    st.caption(c.get("disclaimer", ""))


def page_automations():
    st.title("Automations")
    manifests = manifest_mod.discover()
    latest = {}
    for rec in runlog.read_recent(500):
        latest[rec["id"]] = rec
    rows = [{
        "id": m.id, "runtime": m.runtime, "triggers": ", ".join(m.triggers),
        "schedule": m.schedule or "—", "deploy": m.deploy_target,
        "last status": latest.get(m.id, {}).get("status", "—"),
        "last run": latest.get(m.id, {}).get("end", "—"),
    } for m in manifests]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.subheader("Run now")
    chosen = st.selectbox("Automation", [m.id for m in manifests])
    if st.button(f"▶ Run {chosen}", type="primary"):
        with st.spinner(f"Running {chosen}…"):
            res = subprocess.run([sys.executable, "cli/auto", "run", chosen],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True)
        (st.success if res.returncode == 0 else st.error)(f"{chosen} exited {res.returncode}")
        st.cache_data.clear()
        if res.stdout: st.code(res.stdout)
        if res.stderr: st.code(res.stderr)

    st.subheader("Recent runs")
    st.dataframe(list(reversed(runlog.read_recent(100))), use_container_width=True, hide_index=True)


def page_settings():
    st.title("Settings")
    st.caption("Edit safe manifest fields. Secret values live in Keychain and are never shown here.")
    manifests = manifest_mod.discover()
    chosen = st.selectbox("Automation", [m.id for m in manifests])
    m = manifest_mod.get(chosen)
    path = m.dir / "automation.yaml"
    raw = yaml.safe_load(path.read_text())

    desc = st.text_area("Description", raw.get("description", ""))
    triggers = st.multiselect("Triggers", ["cli", "schedule", "skill", "webhook", "service"],
                              raw.get("triggers", []))
    schedule = st.text_input("Schedule (local cron)", raw.get("schedule", "") or "")
    tz = st.text_input("Timezone", raw.get("tz", "") or "")
    opts = ["none", "local-cron", "github-actions"]
    deploy_target = st.selectbox("Deploy target", opts,
                                 index=opts.index((raw.get("deploy") or {}).get("target", "none")))
    st.write("**Declared secret names:**")
    st.code("\n".join(raw.get("secrets", []) or ["(none)"]))

    if st.button("💾 Save", type="primary"):
        raw["description"] = desc
        raw["triggers"] = triggers
        if schedule.strip(): raw["schedule"] = schedule.strip()
        else: raw.pop("schedule", None)
        if tz.strip(): raw["tz"] = tz.strip()
        raw["deploy"] = {"target": deploy_target}
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        try:
            manifest_mod.load(path)
            st.success(f"Saved {path.name} ✓")
        except manifest_mod.ManifestError as e:
            st.error(f"Saved but INVALID: {e}")


# --- router -------------------------------------------------------------
def _build_nav():
    return st.navigation({
    "Finance": [
        st.Page(page_overview, title="Overview", icon="🏠", default=True),
        st.Page(page_spending, title="Spending", icon="💳"),
        st.Page(page_accounts, title="Accounts", icon="🏦"),
        st.Page(page_investments, title="Investments", icon="📈"),
        st.Page(page_splitwise, title="Splitwise", icon="🤝"),
        st.Page(page_market, title="Market", icon="🌐"),
        st.Page(page_analysis, title="AI Analysis", icon="🧠"),
        st.Page(page_council, title="Council", icon="🏛"),
    ],
    "System": [
        st.Page(page_automations, title="Automations", icon="⚙️"),
        st.Page(page_settings, title="Settings", icon="🔧"),
    ],
    })


if __name__ == "__main__":
    _build_nav().run()
