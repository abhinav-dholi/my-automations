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

import account_classify  # noqa: E402
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
    + list(finance_rules.NON_SPEND) + ["Reimbursement", "Other", "Uncategorized"]
))


# --- data access (cached) ----------------------------------------------

@st.cache_data(ttl=30)
def get_features() -> dict:
    import features
    return features.build(profile=_profile())  # model default (ANALYZE_DAYS) — use all available complete months


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


@st.cache_data(ttl=30)
def _bank_freshness() -> dict:
    """When the bank feed was last synced, and how stale that is — so the UI
    reports real data age, not just when the page (re)rendered."""
    import datetime as dt
    with store.connect() as c:
        ts = store.data_freshness(c).get("bank")
    if not ts:
        return {"ts": None, "hours": None}
    try:
        d = dt.datetime.fromisoformat(ts)
        hrs = (dt.datetime.now(d.tzinfo) - d).total_seconds() / 3600.0
        return {"ts": ts, "hours": hrs}
    except ValueError:
        return {"ts": ts, "hours": None}


def page_overview():
    st.title("💰 My Money")
    if not _has_data():
        st.info("No finance data yet. Run `finance-sync` first."); return

    fr = _bank_freshness()
    if fr["hours"] is not None and fr["hours"] > 12:
        st.warning(f"⚠️ Bank data is **{fr['hours']:.0f}h old** "
                   f"(last synced {fr['ts'][:16].replace('T',' ')}). "
                   "Use **🔄 Refresh all** in the sidebar for current balances.")

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
    ess = res.get("essential_monthly", 0)
    if surplus > 0:
        st.success(_md(f"✅ **Investable surplus: {_money(surplus)}** — liquid cash beyond a "
                   f"{tgt:.0f}-month emergency reserve (you have {em} mo of essentials covered). "
                   f"Reserve sizes essentials only (~{_money(ess)}/mo: rent/utilities/groceries/"
                   "transport/health), not discretionary or one-off moving costs."))
    else:
        gap = max(0, (res.get("emergency_fund_target", 0) - nb["liquid_cash"]))
        st.warning(_md(f"🪙 **Investable surplus: $0** — still building the {tgt:.0f}-month essentials "
                   f"reserve ({em} of {tgt:.0f} mo; ~{_money(gap)} to go). Finish it before investing cash."))

    # ── Monthly cash flow (reconciling waterfall — every line adds up) ──
    st.divider()
    st.subheader("Monthly cash flow (typical month)")
    surplus = cf["operating_cash_surplus"]
    netreimb = cf.get("net_reimbursements", 0)
    rate = cf["operating_surplus_rate"] * 100

    def _sgn(v):
        return f"+{_money(v)}" if v >= 0 else f"−{_money(abs(v))}"

    left, right = st.columns([3, 2])
    with left:
        rows = [("💰 Take-home salary (payroll)", cf["monthly_income"]),
                ("🛒 Spending", -cf["monthly_spend"])]
        if abs(netreimb) >= 1:
            rows.append(("🔄 Net paid back to you (Zelle/Venmo)", netreimb))
        tbl = "| Cash in / out | / month |\n|:--|--:|\n"
        for lbl, v in rows:
            tbl += f"| {lbl} | {_sgn(v)} |\n"
        tbl += f"| **= Operating cash surplus** | **{_sgn(surplus)}** |\n"
        st.markdown(_md(tbl))
        st.caption("Every line is actual cash in/out of your accounts — it reconciles to the surplus.")
    with right:
        st.metric("Operating cash surplus", _money(surplus),
                  delta=f"{rate:.0f}% of take-home",
                  delta_color="normal" if surplus >= 0 else "inverse",
                  help="The real recurring cash you keep: salary − spending + net reimbursements.")

    # Building wealth / lumpy inflows — DELIBERATELY separate from the cash surplus.
    onetime = cf.get("one_time_inflows_window", 0)
    st.markdown("**Building wealth — separate from cash surplus (not spendable / not recurring):**")
    w1, w2, w3 = st.columns(3)
    w1.metric("🏦 Pre-tax 401(k)", _money(cf["monthly_pretax_retirement"]) + "/mo",
              help="Retirement contributions — pre-tax, locked, not in take-home above.")
    w2.metric("🎁 One-time inflows", _money(onetime),
              help=f"Refunds/bonuses over the last {cf.get('complete_months',0)} mo — lumpy, not recurring.")
    w3.metric("🤝 Splitwise (owed to you)",
              _money(sum(f["splitwise"]["net_balance_by_currency"].values())
                     if f["splitwise"]["net_balance_by_currency"] else 0),
              help="A receivable — money friends owe you, separate from cash. Settled via Zelle/Venmo.")
    st.caption(f"ℹ︎ Run-rate over **{n_mo} complete calendar month(s)** (current month excluded so a "
               "partial month doesn't skew it). Live current-month activity is below 👇"
               + ("  ⚠️ only %d complete month — firms up over time." % n_mo if n_mo < 2 else ""))

    # ── This month so far (live, current incomplete month) ──
    mtd = f.get("month_to_date") or {}
    if mtd:
        import datetime as _dt
        try:
            mlabel = _dt.datetime.strptime(mtd["month"], "%Y-%m").strftime("%B")
        except (ValueError, KeyError):
            mlabel = mtd.get("month", "")
        st.markdown(f"**This month so far — {mlabel} 1–{mtd.get('day','')}**")
        d1, d2, d3 = st.columns(3)
        d1.metric("Income", _money(mtd.get("income", 0)))
        d2.metric("Spend", _money(mtd.get("spend", 0)))
        net = mtd.get("net", 0)
        d3.metric("Net so far", _money(net), delta=("surplus" if net >= 0 else "deficit"),
                  delta_color=("normal" if net >= 0 else "inverse"))
        st.caption("Live month-to-date (updates on every sync); not yet in the typical-month figures above.")

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
    synced = fr["ts"][:16].replace("T", " ") if fr["ts"] else "—"
    st.caption(f"Bank data synced {synced}. Tabs at left break down "
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
    if not st.session_state.get("_flags_ready"):
        store.init_db(); st.session_state["_flags_ready"] = True   # ensure txn_flags table
    with store.connect() as _c:
        _fl = store.txn_flags(_c)
    df["one_time"] = df["id"].map(lambda i: _fl.get(i, {}).get("one_time", False))
    df["exclude"] = df["id"].map(lambda i: _fl.get(i, {}).get("excluded", False))

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

    if spend.empty:
        st.info("No spending in this range."); return

    import features as _feat
    ESS = _feat.ESSENTIAL_CATEGORIES
    months_in = max(0.5, (end - start) / 2629800)   # seconds → ~months
    total = spend["spend"].sum()
    spend["bucket"] = spend["category"].apply(lambda x: "Essential" if x in ESS else "Discretionary")
    ess_tot = spend.loc[spend["bucket"] == "Essential", "spend"].sum()
    disc_tot = total - ess_tot

    c = st.columns(5)
    c[0].metric("Total spend", _money(total))
    c[1].metric("Avg / month", _money(total / months_in))
    c[2].metric("Essential", _money(ess_tot),
                help="Rent, utilities, groceries, transport, health — your baseline.")
    c[3].metric("Discretionary", _money(disc_tot),
                help="Dining, shopping, travel, etc. — the flexible part.")
    c[4].metric("Income (range)", _money(df[df["category"] == "Income"]["amount"].sum()))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("By category")
        bycat = spend.groupby("category")["spend"].sum().sort_values(ascending=True)
        fig = px.bar(bycat, orientation="h", template=PLOT_TEMPLATE,
                     color_discrete_sequence=[ACCENT], text=bycat.values)
        fig.update_traces(texttemplate="$%{text:,.0f}", textposition="outside", cliponaxis=False)
        fig.update_layout(height=380, showlegend=False, margin=dict(t=10, b=10, l=10, r=50),
                          xaxis_title=None, yaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.subheader("Essential vs discretionary")
        st.plotly_chart(donut(["Essential", "Discretionary"], [ess_tot, disc_tot]),
                        use_container_width=True)
        st.caption(f"{ess_tot/total*100:.0f}% essential · {disc_tot/total*100:.0f}% discretionary"
                   if total else "")

    cL, cR = st.columns(2)
    with cL:
        st.subheader("Spend over time (weekly)")
        wk = spend.set_index("date").resample("W")["spend"].sum().reset_index()
        fig = px.bar(wk, x="date", y="spend", template=PLOT_TEMPLATE, color_discrete_sequence=[ACCENT])
        fig.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None, yaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)
    with cR:
        st.subheader("Top merchants")
        sp2 = spend.copy()
        sp2["merchant"] = sp2["description"].apply(store.merchant_key)
        topm = (sp2.groupby("merchant")
                .agg(Spent=("spend", "sum"), Txns=("spend", "size"))
                .sort_values("Spent", ascending=False).head(12).reset_index())
        topm["merchant"] = topm["merchant"].str.title()
        st.dataframe(topm.rename(columns={"merchant": "Merchant"}), hide_index=True,
                     use_container_width=True, height=300,
                     column_config={"Spent": st.column_config.NumberColumn(format="$%.0f")})

    st.subheader("Transactions — edit categories & tags")
    ec1, ec2 = st.columns([3, 1])
    ec1.caption("Edit category, or tag a row: **one-time** keeps it in actuals/net worth but out "
                "of your typical-month figures (e.g. family flights, moving costs); **exclude** drops "
                "it from all analytics (a duplicate/error). Both persist across syncs.")
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

    editor_df = view[["id", "date", "description", "amount", "category",
                      "one_time", "exclude"]].reset_index(drop=True)
    edited = st.data_editor(
        editor_df, hide_index=True, use_container_width=True, height=460,
        key="txn_editor",
        column_config={
            "id": None,
            "date": st.column_config.DatetimeColumn("Date", disabled=True, format="YYYY-MM-DD"),
            "description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "amount": st.column_config.NumberColumn("Amount", disabled=True, format="$%.2f"),
            "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
            "one_time": st.column_config.CheckboxColumn("One-time", help="Keep in actuals, exclude from typical month"),
            "exclude": st.column_config.CheckboxColumn("Exclude", help="Drop from all analytics (duplicate/error)"),
        },
    )
    if st.button("💾 Save changes", type="primary"):
        orig = dict(zip(editor_df["id"], editor_df["category"]))
        new = dict(zip(edited["id"], edited["category"]))
        changed = {i: c for i, c in new.items() if orig.get(i) != c}
        # Persist flag changes (one-time / exclude).
        of = dict(zip(editor_df["id"], zip(editor_df["one_time"], editor_df["exclude"])))
        nf = dict(zip(edited["id"], zip(edited["one_time"], edited["exclude"])))
        flag_changes = {i: nf[i] for i in nf if of.get(i) != nf[i]}
        if flag_changes:
            with store.connect() as conn:
                for tid, (ot, ex) in flag_changes.items():
                    store.set_txn_flags(conn, tid, one_time=bool(ot), excluded=bool(ex))
        if not changed and not flag_changes:
            st.info("No changes to save.")
        else:
            desc_by_id = dict(zip(view["id"], view["description"]))
            retagged = 0
            with store.connect() as conn:
                for tid, cat in changed.items():
                    store.set_manual_category(conn, tid, cat)        # this txn (wins always)
                    retagged += store.learn_category(conn, desc_by_id.get(tid, ""), cat)  # learn + re-tag similar
            parts = []
            if changed:
                parts.append(f"{len(changed)} category edit(s) (re-tagged {retagged} similar)")
            if flag_changes:
                parts.append(f"{len(flag_changes)} tag change(s)")
            st.success("Saved " + " and ".join(parts) + ". Persists across syncs.")
            st.cache_data.clear()
            st.rerun()


TYPE_ICON = {"checking": "🏦", "savings": "💵", "credit": "💳",
             "investment": "📊", "loan": "🏷️"}


def _acct_kind(r: dict) -> str:
    """Human label: the subtype if any (HYSA / brokerage / retirement / equity),
    else the coarse type."""
    return account_classify.label(r.get("subtype")) or (r.get("type") or "").title()


def _acct_icon(r: dict) -> str:
    return account_classify.icon(r.get("subtype")) or TYPE_ICON.get(r.get("type"), "•")


def page_accounts():
    st.title("Accounts")
    st.caption("Auto-classified from your institutions (SimpleFIN) — HYSA vs regular "
               "savings, brokerage vs retirement vs equity awards. No manual tagging.")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    bal = pd.DataFrame(db["balances"])
    if bal.empty:
        st.info("No accounts."); return
    for col in ("subtype", "institution"):           # tolerate pre-migration rows
        if col not in bal.columns:
            bal[col] = ""
    bal["institution"] = bal["institution"].fillna("").replace("", "Other")
    bal["kind"] = bal.apply(lambda r: _acct_kind(r), axis=1)

    # Effective value = max(cash balance, holdings) for investment accounts, so an
    # equity-award account that reports $0 cash but holds vested stock still counts.
    # This is the SAME logic features uses for net worth — keeps the page reconciled.
    hb: dict = {}
    for h in (db.get("holdings") or []):
        hb[h["account_id"]] = hb.get(h["account_id"], 0) + (h.get("market_value") or 0)
    bal["value"] = bal.apply(
        lambda r: max(r["balance"], hb.get(r["id"], 0)) if r["type"] == "investment"
        else r["balance"], axis=1)

    aq = st.text_input("🔎 Search accounts", "",
                       placeholder="name, institution, or kind (e.g. brokerage, HYSA)…").lower().strip()
    if aq:
        bal = bal[bal["name"].str.lower().str.contains(aq, na=False)
                  | bal["institution"].str.lower().str.contains(aq, na=False)
                  | bal["kind"].str.lower().str.contains(aq, na=False)]
        if bal.empty:
            st.caption("No accounts match your search."); return

    assets = bal[bal["value"] > 0]["value"].sum()
    debts = bal[bal["value"] < 0]["value"].sum()
    liquid = bal[bal["type"].isin(["checking", "savings"])]["value"].sum()
    sw_recv = sum(x.get("amount", 0) for x in (db.get("splitwise") or []))
    net_worth = assets + debts + sw_recv
    c = st.columns(4)
    c[0].metric("Total assets", _money(assets + max(sw_recv, 0)),
                help="Account values" + (f" + {_money(sw_recv)} Splitwise owed to you" if sw_recv > 0 else ""))
    c[1].metric("💵 Liquid cash", _money(liquid))
    c[2].metric("Total debt", _money(debts + min(sw_recv, 0)))
    c[3].metric("Net worth", _money(net_worth),
                help="Account values (investments use holdings value) ± Splitwise receivable. "
                     "Matches the Overview net worth.")
    if abs(sw_recv) >= 1:
        st.caption(_md(f"Includes **{_money(sw_recv)}** net {'owed to you' if sw_recv>=0 else 'you owe'} "
                       "on Splitwise (a receivable — money you fronted that returns to you)."))

    # ── Grouped by institution (card per bank) ──
    st.subheader("Accounts by institution")
    order = (bal.groupby("institution")["value"].sum()
             .sort_values(ascending=False).index.tolist())
    cols = st.columns(2)
    for i, inst in enumerate(order):
        g = bal[bal["institution"] == inst].sort_values("value", ascending=False)
        with cols[i % 2].container(border=True):
            st.markdown(f"**{inst}** · net {_money(g['value'].sum())}")
            for _, r in g.iterrows():
                a, b = st.columns([5, 2])
                note = " · _stock_" if r["type"] == "investment" and r["balance"] < 1 and r["value"] > 1 else ""
                a.markdown(f"{_acct_icon(r)} {r['name']}  \n"
                           f"<span style='color:#888;font-size:0.8em'>{r['kind']}{note}</span>",
                           unsafe_allow_html=True)
                b.markdown(f"<div style='text-align:right'>{_money(r['value'])}</div>",
                           unsafe_allow_html=True)

    # ── Charts ──
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Value by account")
        b = bal.sort_values("value")
        fig = px.bar(b, x="value", y="name", orientation="h", template=PLOT_TEMPLATE,
                     color="value", color_continuous_scale=["#ef4444", "#22c55e"])
        fig.update_layout(height=400, margin=dict(t=20, b=10, l=10, r=10),
                          xaxis_title=None, yaxis_title=None, coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.subheader("Assets by kind")
        pos = bal[bal["value"] > 0]
        bykind = pos.groupby("kind")["value"].sum()
        st.plotly_chart(donut(list(bykind.index), list(bykind.values)), use_container_width=True)

    with st.expander("📋 All accounts (table)"):
        st.dataframe(
            bal[["name", "institution", "kind", "type", "value"]]
            .sort_values("value", ascending=False)
            .rename(columns={"name": "Account", "institution": "Institution",
                             "kind": "Kind", "type": "Type", "value": "Value"}),
            use_container_width=True, hide_index=True,
            column_config={"Value": st.column_config.NumberColumn(format="$%.2f")})

    over = [r for r in db["balances"] if r.get("overridden")]
    if over:
        st.info("✏️ Active balance corrections (auto-clear when the bank's feed changes): "
                + ", ".join(f"**{r['name']}** → {_money(r['balance'])} "
                            f"(feed says {_money(r.get('synced_balance'))})" for r in over))

    with st.expander("✏️ Correct a balance (stale bank feed)"):
        st.caption("Use this when a synced balance is wrong — e.g. a transfer was credited to "
                   "one account but not yet debited from the source, so money is double-counted. "
                   "Your correction applies until the bank's feed changes, then auto-clears.")
        opts = {r["id"]: r for r in db["balances"]}
        aid = st.selectbox("Account", list(opts),
                           format_func=lambda i: f"{opts[i]['name']} — feed: "
                           f"{_money(opts[i].get('synced_balance', opts[i]['balance']))}")
        synced = float(opts[aid].get("synced_balance", opts[aid]["balance"]))
        correct = st.number_input("Correct balance ($)", value=synced, step=100.0, format="%.2f")
        b1, b2 = st.columns(2)
        if b1.button("💾 Save correction", type="primary"):
            with store.connect() as conn:
                store.set_balance_override(conn, account_id=aid, override_balance=correct,
                                           stale_balance=synced, note="manual UI correction")
            st.cache_data.clear(); st.success("Correction saved."); st.rerun()
        if b2.button("↩️ Clear correction"):
            with store.connect() as conn:
                store.clear_balance_override(conn, aid)
            st.cache_data.clear(); st.rerun()


def page_investments():
    st.title("📈 Investments")
    st.caption("Is your portfolio healthy? Unrealized gains, what you can rebalance vs locked "
               "retirement, single-stock concentration, and allocation vs target — in one view.")
    if not _has_data():
        st.info("No data yet."); return
    import features as _feat
    f = get_features(); db = load_finance_db()
    port, nb = f["portfolio"], f["net_worth_breakdown"]

    if not db["holdings"]:
        st.info("No holdings synced yet."); return

    # ── Per-holding detail (every account, incl. vested equity awards) ──
    accts = {b["id"]: (b["name"], b["type"]) for b in db["balances"]}
    rows = []
    for h in db["holdings"]:
        nm, ty = accts.get(h["account_id"], ("?", "?"))
        role = _feat._acct_role(nm, ty)
        gain = (h["market_value"] or 0) - (h["cost_basis"] or 0)
        gpct = (gain / h["cost_basis"] * 100) if h["cost_basis"] else None
        retire = "401" in nm.lower() or "ira" in nm.lower() or "hsa" in nm.lower() or "health savings" in nm.lower()
        rows.append({"Symbol": h["symbol"], "Holding": h["name"], "Account": _feat._clean_acct(nm),
                     "Bucket": "Locked (retirement)" if retire else "Taxable / vested",
                     "Value": h["market_value"] or 0, "Cost": h["cost_basis"] or 0,
                     "Gain $": gain, "Gain %": round(gpct, 1) if gpct is not None else None,
                     "Qty": h["quantity"]})
    hdf = pd.DataFrame(rows)
    taxable_df = hdf[hdf["Bucket"] == "Taxable / vested"]
    taxable_gain = taxable_df["Gain $"].sum()
    taxable_cost = taxable_df["Cost"].sum()

    # ── KPIs ──
    c = st.columns(5)
    c[0].metric("📈 Taxable + vested", _money(nb["taxable_investments"]),
                help="What you can rebalance / sell.")
    c[1].metric("🔒 Retirement (locked)", _money(nb["retirement_locked"]),
                help="401(k)/HSA — diversified, left alone.")
    c[2].metric("Unrealized gain (taxable)", _money(taxable_gain),
                f"{taxable_gain/taxable_cost*100:.1f}%" if taxable_cost else None)
    c[3].metric("💵 Investable surplus", _money(f.get("investable_surplus", 0)),
                help="Cash beyond your emergency reserve — free to deploy.")
    c[4].metric("⚠️ Top concentration", f"{port['top_concentration_pct']*100:.0f}%",
                help="Largest single holding as % of cash + taxable investments.")

    # ── Allocation vs target (controllable) ──
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Investable allocation vs target")
        st.caption("Across cash + taxable holdings you control (excludes the locked 401k).")
        cur, tgt = f["allocation"]["current"], f["allocation"]["target"]
        keys = ["equity", "bonds", "cash"]
        fig = go.Figure([
            go.Bar(name="Current", x=keys, y=[cur.get(k, 0)*100 for k in keys], marker_color=ACCENT),
            go.Bar(name="Target", x=keys, y=[tgt.get(k, 0)*100 for k in keys], marker_color="#22c55e"),
        ])
        fig.update_layout(template=PLOT_TEMPLATE, barmode="group", height=320, yaxis_title="%",
                          margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.subheader("Holdings mix")
        agg = hdf.groupby("Symbol")["Value"].sum().sort_values(ascending=False)
        st.plotly_chart(donut(list(agg.index), list(agg.values)), use_container_width=True)

    # ── Concentration insight ──
    if port["top_concentration_pct"] > 0.2 and port.get("holdings"):
        top = port["holdings"][0]
        st.warning(f"⚠️ **{top['symbol']}** is **{top['pct_of_taxable']*100:.0f}% of your taxable "
                   f"investments** and {top['pct_of_controllable']*100:.0f}% of investable assets"
                   + (f" (down {abs(top['gain_pct'])*100:.0f}%)" if top.get('gain_pct') and top['gain_pct'] < 0 else "")
                   + " — a single-stock concentration the analysis/council flag for diversification.")

    # ── All holdings table ──
    st.subheader("All holdings")
    hq = st.text_input("🔎 Search holdings", "",
                       placeholder="symbol, holding name, or account…").lower().strip()
    hview = hdf.sort_values("Value", ascending=False)
    if hq:
        mask = (hview["Symbol"].str.lower().str.contains(hq, na=False)
                | hview["Holding"].str.lower().str.contains(hq, na=False)
                | hview["Account"].str.lower().str.contains(hq, na=False))
        hview = hview[mask]
    money_col = st.column_config.NumberColumn(format="$%.0f")
    st.dataframe(hview,
                 use_container_width=True, hide_index=True,
                 column_config={"Value": money_col, "Cost": money_col, "Gain $": money_col,
                                "Gain %": st.column_config.NumberColumn(format="%.1f%%"),
                                "Qty": st.column_config.NumberColumn(format="%.2f")})

    # ── By account ──
    st.subheader("By account")
    byacct = (hdf.groupby(["Account", "Bucket"])["Value"].sum().reset_index()
              .sort_values("Value", ascending=False))
    st.dataframe(byacct, use_container_width=True, hide_index=True,
                 column_config={"Value": money_col})
    st.caption("Taxable/vested = rebalanceable now. Locked (401k/HSA) = retirement, left alone. "
               "Vested equity-award stock is counted even when the account shows \\$0 cash.")


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
    st.caption(_md(f"Your share of shared expenses (90d): {_money(db['sw_share'])}"))


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


@st.cache_data(ttl=60)
def _price_changes() -> dict:
    """Latest close + change vs the prior recorded close, per symbol, from the
    price history we already store (market-data-sync)."""
    with store.connect() as c:
        rows = c.execute(
            "SELECT symbol, close, date FROM market_prices ORDER BY symbol, date").fetchall()
    series: dict = {}
    for r in rows:
        series.setdefault(r["symbol"], []).append((r["date"], r["close"]))
    out = {}
    for sym, pts in series.items():
        close = pts[-1][1]
        prev = pts[-2][1] if len(pts) > 1 else None
        pct = ((close - prev) / prev * 100) if prev else None
        out[sym] = {"close": close, "pct": pct, "date": pts[-1][0]}
    return out


@st.cache_data(ttl=60)
def _price_series() -> dict:
    """Per-symbol close history (date-sorted) from market-data-sync, for rebased
    performance comparison."""
    with store.connect() as c:
        rows = c.execute(
            "SELECT symbol, close, date FROM market_prices ORDER BY date").fetchall()
    series: dict = {}
    for r in rows:
        s = series.setdefault(r["symbol"], {"date": [], "close": []})
        s["date"].append(r["date"]); s["close"].append(r["close"])
    return series


def _price_grid(symbols, prices, chg, ncol=4):
    """Render a metric grid of symbol → price (+ % change colored)."""
    syms = [s for s in symbols if s in prices or s in chg]
    if not syms:
        st.caption("No price data yet."); return
    for i in range(0, len(syms), ncol):
        cols = st.columns(ncol)
        for col, sym in zip(cols, syms[i:i + ncol]):
            price = prices.get(sym) or chg.get(sym, {}).get("close")
            pct = chg.get(sym, {}).get("pct")
            delta = (f"{pct:+.2f}%" if pct is not None else None)
            col.metric(sym, f"${price:,.2f}" if isinstance(price, (int, float)) else "—", delta=delta)


def page_market():
    head, btn = st.columns([4, 1])
    head.title("🌐 Market")
    brief = _load_json("market-brief.json")
    if btn.button("🔄 Refresh", use_container_width=True,
                  help="Scout web for news (Claude) + pull prices/macro"):
        _run_cli(["cli/auto", "run", "market-watch"], "Scouting the web…")
        _run_cli(["cli/auto", "run", "market-data-sync"], "Fetching prices + macro…")
        st.cache_data.clear(); st.rerun()
    if not brief:
        st.info("No market brief yet. Click **🔄 Refresh** (or run `market-watch` / "
                "`market-data-sync`)."); return

    # Freshness banner — the brief is only as fresh as the last market-watch run.
    gen = brief.get("generated_at", "")
    if gen:
        import datetime as _dt
        try:
            g = _dt.datetime.fromisoformat(gen)
            hrs = (_dt.datetime.now(g.tzinfo) - g).total_seconds() / 3600
            msg = f"Brief from {gen[:16].replace('T', ' ')} ({hrs:.0f}h ago)"
            (st.warning if hrs > 24 else st.caption)(
                ("⚠️ " + msg + " — Refresh for current news.") if hrs > 24 else "🟢 " + msg)
        except ValueError:
            st.caption(f"Brief from {gen}")

    held = list(brief.get("held_tickers") or [])
    benchmarks = list(brief.get("benchmarks") or [])
    prices = brief.get("prices", {})
    chg = _price_changes()

    t_mkt, t_news = st.tabs(["📈 Prices & macro", f"📰 News ({len(brief.get('news', []))})"])

    with t_mkt:
        # ── Performance: your holdings vs benchmarks (the core "how am I doing vs the market") ──
        st.subheader("Your stocks vs the market")
        series = _price_series()
        priced_holdings = [s for s in held if s in series]
        default_cmp = (priced_holdings or []) + [b for b in ["SPY", "QQQ"] if b in series]
        avail = sorted(series.keys())
        if not avail:
            st.caption("No price history yet — click 🔄 Refresh, then it builds daily.")
        else:
            pick = st.multiselect(
                "Compare (rebased to 100 at window start)", avail,
                default=[s for s in default_cmp if s in avail] or avail[:3],
                help="Your held tickers with a price feed + market benchmarks. "
                     "META = your stock; SPY/QQQ/VTI = the market.")
            fig = go.Figure()
            ret_rows = []
            for sym in pick:
                s = series[sym]
                base = s["close"][0] if s["close"] else None
                if not base:
                    continue
                reb = [c / base * 100 for c in s["close"]]
                is_mine = sym in held
                fig.add_trace(go.Scatter(
                    x=s["date"], y=reb, name=("★ " + sym) if is_mine else sym,
                    mode="lines+markers",
                    line=dict(width=3.5 if is_mine else 1.8,
                              dash=None if is_mine else "dot")))
                ret_rows.append({"Symbol": sym, "Mine": "★" if is_mine else "",
                                 "Return": (reb[-1] - 100) if reb else 0})
            fig.update_layout(template=PLOT_TEMPLATE, height=340, yaxis_title="Rebased (100 = start)",
                              margin=dict(t=10, b=10, l=10, r=10),
                              legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(fig, use_container_width=True)
            if ret_rows:
                rdf = pd.DataFrame(ret_rows).sort_values("Return", ascending=False)
                st.dataframe(rdf, hide_index=True, use_container_width=True,
                             column_config={"Return": st.column_config.NumberColumn(
                                 "Return over window", format="%.2f%%")})
            days = max((len(series[s]["close"]) for s in series), default=0)
            st.caption(f"★ = your holding. Window ≈ {days} trading day(s) of data so far — it "
                       "lengthens each day market-data-sync runs. O5L9/FDRXX have no public feed, "
                       "so they're not plotted.")

        st.divider()
        st.subheader("Your holdings")
        db = load_finance_db()
        hold = pd.DataFrame(db.get("holdings") or [])
        if hold.empty:
            st.caption("No holdings synced yet.")
        else:
            agg = (hold.groupby(["symbol", "name"], as_index=False)
                   .agg(shares=("quantity", "sum"), value=("market_value", "sum")))
            agg["price"] = agg["symbol"].map(lambda s: prices.get(s) or chg.get(s, {}).get("close"))
            agg["chgpct"] = agg["symbol"].map(lambda s: chg.get(s, {}).get("pct"))
            agg = agg.sort_values("value", ascending=False)
            total = agg["value"].sum()
            agg["weight"] = agg["value"] / total if total else 0
            m1, m2 = st.columns(2)
            m1.metric("Total holdings value", _money(total))
            m2.metric("Positions", f"{len(agg)}")
            st.dataframe(
                agg[["symbol", "name", "shares", "price", "chgpct", "value", "weight"]],
                hide_index=True, use_container_width=True,
                column_config={
                    "symbol": "Symbol",
                    "name": st.column_config.TextColumn("Name", width="medium"),
                    "shares": st.column_config.NumberColumn("Shares", format="%.3f"),
                    "price": st.column_config.NumberColumn("Price", format="$%.2f"),
                    "chgpct": st.column_config.NumberColumn("Chg", format="%.2f%%"),
                    "value": st.column_config.NumberColumn("Value", format="$%.0f"),
                    "weight": st.column_config.ProgressColumn("Weight", format="%.0f%%",
                                                              min_value=0, max_value=1),
                })
            st.caption("Prices/changes from market-data-sync; '—' means no price feed for that "
                       "symbol (e.g. money-market or plan funds).")
        if benchmarks:
            st.subheader("Benchmarks")
            _price_grid(benchmarks, prices, chg)
        macro = brief.get("macro", {})
        if macro:
            st.subheader("Macro indicators")
            items = list(macro.items())
            for i in range(0, len(items), 4):
                cols = st.columns(4)
                for col, (k, v) in zip(cols, items[i:i + 4]):
                    col.metric(v.get("label", k), f"{v.get('value')}",
                               help=f"As of {v.get('date','')}")

    with t_news:
        news = brief.get("news", [])
        if not news:
            st.caption("No news in the latest brief.")
        else:
            fcol, tcol = st.columns([3, 2])
            q = fcol.text_input("🔎 Search news", "", placeholder="headline, summary, source, ticker…")
            only_mine = tcol.toggle("Only my holdings", value=False)
            heldset = {h.upper() for h in held}
            ql = q.lower().strip()
            shown = 0
            for n in sorted(news, key=lambda x: str(x.get("relevance", "")), reverse=True):
                tks = [t.upper() for t in (n.get("tickers") or [])]
                if only_mine and not (heldset & set(tks)):
                    continue
                if ql:
                    hay = " ".join([n.get("headline", ""), n.get("summary", ""),
                                    n.get("source", ""), " ".join(tks)]).lower()
                    if ql not in hay:
                        continue
                shown += 1
                url = n.get("url", "")
                link = f" · [source]({url})" if url else ""
                tagline = " ".join(f"`{t}`" for t in tks) if tks else ""
                with st.container(border=True):
                    st.markdown(f"**{_md(n.get('headline',''))}** {tagline}")
                    if n.get("summary"):
                        st.markdown(_md(n.get("summary", "")))
                    st.markdown(f"<span style='opacity:.6;font-size:0.85em'>"
                                f"{_md(n.get('source',''))}{link} · {_md(n.get('relevance',''))}</span>",
                                unsafe_allow_html=True)
            if shown == 0:
                st.caption("No news matches your filters.")


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


# --- AI chat (answer + auto-visual) -------------------------------------
def _chat_chart(spec: dict):
    """Render one chart from a finance_chat spec: a catalog id (drawn from our own
    loaded data) or a small custom {type,x,y}. Returns a Plotly fig or None."""
    try:
        cid = spec.get("id")
        if cid:
            f = get_features(); db = load_finance_db()
            if cid == "spend_by_category":
                d = f.get("spend_by_category_monthly", {})
                return donut(list(d)[:8], [d[k] for k in list(d)[:8]]) if d else None
            if cid == "monthly_income_vs_spend":
                s = f.get("monthly_series", {})
                if not s:
                    return None
                xs = sorted(s)
                fig = go.Figure([
                    go.Bar(name="Income", x=xs, y=[s[m]["income"] for m in xs], marker_color="#22c55e"),
                    go.Bar(name="Spend", x=xs, y=[s[m]["spend"] for m in xs], marker_color="#ef4444")])
                fig.update_layout(template=PLOT_TEMPLATE, barmode="group", height=320,
                                  margin=dict(t=10, b=10, l=10, r=10))
                return fig
            if cid == "allocation_vs_target":
                cur, tgt = f["allocation"]["current"], f["allocation"]["target"]
                keys = ["equity", "bonds", "cash"]
                fig = go.Figure([
                    go.Bar(name="Current", x=keys, y=[cur.get(k, 0)*100 for k in keys], marker_color=ACCENT),
                    go.Bar(name="Target", x=keys, y=[tgt.get(k, 0)*100 for k in keys], marker_color="#22c55e")])
                fig.update_layout(template=PLOT_TEMPLATE, barmode="group", height=320,
                                  yaxis_title="%", margin=dict(t=10, b=10, l=10, r=10))
                return fig
            if cid == "net_worth_composition":
                nb = f["net_worth_breakdown"]
                comp = [("Liquid cash", nb["liquid_cash"]), ("Taxable invest", nb["taxable_investments"]),
                        ("Retirement", nb["retirement_locked"])]
                comp = [(l, v) for l, v in comp if v > 0]
                return donut([l for l, _ in comp], [v for _, v in comp]) if comp else None
            if cid == "holdings_mix":
                h = pd.DataFrame(db.get("holdings") or [])
                if h.empty:
                    return None
                agg = h.groupby("symbol")["market_value"].sum().sort_values(ascending=False)
                return donut(list(agg.index), list(agg.values))
            if cid == "top_merchants":
                df = pd.DataFrame(db.get("txns") or [])
                if df.empty:
                    return None
                sp = df[(df["amount"] < 0) & (~df["category"].isin(finance_rules.NON_SPEND))].copy()
                sp["m"] = sp["description"].apply(store.merchant_key)
                top = sp.groupby("m")["amount"].sum().abs().sort_values(ascending=False).head(12)
                fig = px.bar(x=top.values, y=[m.title() for m in top.index], orientation="h",
                             template=PLOT_TEMPLATE)
                fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                                  xaxis_title="$", yaxis_title=None)
                return fig
            if cid == "performance_vs_benchmark":
                series = _price_series()
                if not series:
                    return None
                fig = go.Figure()
                for sym, s in series.items():
                    base = s["close"][0] if s["close"] else None
                    if base:
                        fig.add_trace(go.Scatter(x=s["date"], y=[c/base*100 for c in s["close"]],
                                                 name=sym, mode="lines+markers"))
                fig.update_layout(template=PLOT_TEMPLATE, height=320, yaxis_title="Rebased (100=start)",
                                  margin=dict(t=10, b=10, l=10, r=10))
                return fig
            if cid == "accounts_by_kind":
                bal = pd.DataFrame(db.get("balances") or [])
                if bal.empty:
                    return None
                bal["kind"] = bal.apply(lambda r: _acct_kind(r), axis=1)
                pos = bal[bal["balance"] > 0].groupby("kind")["balance"].sum()
                return donut(list(pos.index), list(pos.values)) if len(pos) else None
            if cid == "splitwise_by_friend":
                sw = pd.DataFrame(db.get("splitwise") or [])
                if sw.empty:
                    return None
                sw = sw[sw["amount"].abs() > 0.5].sort_values("amount")
                fig = px.bar(sw, x="amount", y="friend_name", orientation="h", template=PLOT_TEMPLATE,
                             color="amount", color_continuous_scale=["#ef4444", "#22c55e"])
                fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10),
                                  xaxis_title=None, yaxis_title=None, coloraxis_showscale=False)
                return fig
            return None
        # custom chart
        typ = spec.get("type")
        title = spec.get("title", "")
        if typ == "pie":
            return donut(spec.get("labels", []), spec.get("y", []), title=title)
        if typ in ("bar", "line"):
            x, y = spec.get("x", []), spec.get("y", [])
            trace = (go.Bar(x=x, y=y) if typ == "bar"
                     else go.Scatter(x=x, y=y, mode="lines+markers"))
            fig = go.Figure([trace])
            fig.update_layout(template=PLOT_TEMPLATE, title=title, height=320,
                              margin=dict(t=40, b=10, l=10, r=10))
            return fig
    except Exception:
        return None
    return None


CHAT_STARTERS = [
    "How's my financial health overall?",
    "Where is my money going each month?",
    "Am I too concentrated in any one stock?",
    "How are my accounts doing?",
]


def page_chat():
    st.title("💬 Ask AI")
    st.caption("Ask anything about your money in plain English — get a written answer **and** the "
               "right chart, drawn from your own data. Multi-turn: follow up freely.")
    if not _has_data():
        st.info("No finance data yet. Run `finance-sync` first."); return

    if not st.session_state.get("_chat_db_ready"):
        store.init_db()                       # ensure chat_messages table exists
        st.session_state["_chat_db_ready"] = True

    hist = st.session_state.setdefault("chat_history", [])
    with store.connect() as conn:
        past = store.list_chats(conn, 30)

    top = st.columns([1, 3])
    if top[0].button("🆕 New chat", use_container_width=True):
        st.session_state["chat_history"] = []
        st.session_state["conv_id"] = None
        st.rerun()
    top[1].caption(f"{len([m for m in hist if m['role']=='user'])} question(s) in this chat · "
                   f"{len(past)} saved conversation(s). History persists across restarts.")

    if past:
        with st.expander(f"📚 Past chats ({len(past)})",
                         expanded=not hist and bool(past)):
            for p in past:
                title = (p.get("title") or "Chat").strip().replace("\n", " ")[:60]
                meta = f"{p.get('started','')[:10]} · {p.get('questions',0)} Q"
                lc, dc = st.columns([6, 1])
                if lc.button(f"{title}  ·  _{meta}_", key=f"load_{p['conv_id']}",
                             use_container_width=True):
                    with store.connect() as conn:
                        st.session_state["chat_history"] = store.load_chat(conn, p["conv_id"])
                    st.session_state["conv_id"] = p["conv_id"]
                    st.rerun()
                if dc.button("🗑", key=f"del_{p['conv_id']}", help="Delete this conversation"):
                    with store.connect() as conn:
                        store.delete_chat(conn, p["conv_id"])
                    if st.session_state.get("conv_id") == p["conv_id"]:
                        st.session_state["chat_history"] = []
                        st.session_state["conv_id"] = None
                    st.rerun()

    if not hist:
        st.markdown("**Try:**")
        cols = st.columns(2)
        for i, s in enumerate(CHAT_STARTERS):
            if cols[i % 2].button(s, use_container_width=True, key=f"start_{i}"):
                st.session_state["pending_prompt"] = s; st.rerun()

    for m in hist:
        with st.chat_message(m["role"]):
            st.markdown(_md(m["content"]))      # escape $/~ so amounts don't render as LaTeX
            for spec in m.get("charts", []):
                fig = _chat_chart(spec)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True, key=f"c_{id(spec)}")

    # Follow-up chips from the latest assistant turn.
    if hist and hist[-1]["role"] == "assistant" and hist[-1].get("followups"):
        st.markdown("**Follow up:**")
        fcols = st.columns(len(hist[-1]["followups"]))
        for i, fu in enumerate(hist[-1]["followups"]):
            if fcols[i].button(fu, key=f"fu_{len(hist)}_{i}", use_container_width=True):
                st.session_state["pending_prompt"] = fu; st.rerun()

    pending = st.session_state.pop("pending_prompt", None)
    typed = st.chat_input("Ask about your finances… e.g. 'What did I spend on travel last month?'")
    user_msg = pending or typed
    if user_msg:
        import time as _time
        conv_id = st.session_state.get("conv_id") or f"chat-{int(_time.time())}"
        st.session_state["conv_id"] = conv_id
        hist.append({"role": "user", "content": user_msg})
        with st.spinner("Thinking + crunching your numbers…"):
            import finance_chat
            res = finance_chat.chat(hist)
        hist.append({"role": "assistant", "content": res["answer"],
                     "charts": res.get("charts", []), "followups": res.get("followups", [])})
        with store.connect() as conn:                # persist both turns
            store.save_chat_message(conn, conv_id=conv_id, role="user", content=user_msg)
            store.save_chat_message(conn, conv_id=conv_id, role="assistant",
                                    content=res["answer"], charts=res.get("charts", []),
                                    followups=res.get("followups", []))
        st.rerun()


# --- data health (reconciliation invariants) ----------------------------
@st.cache_data(ttl=60)
def _health() -> dict:
    import integrity
    return integrity.summary()


def page_health():
    st.title("🩺 Data Health")
    st.caption("Reconciliation invariants that keep the numbers honest — surfaces totalling "
               "errors and data gaps instead of letting them hide inside an aggregate.")
    if not _has_data():
        st.info("No data yet."); return
    summ = _health()
    badge = {"ok": "🟢 All checks passing", "warn": "🟡 Items to review",
             "error": "🔴 Errors found"}[summ["status"]]
    st.subheader(badge)
    counts = summ["counts"]
    c = st.columns(3)
    c[0].metric("🔴 Errors", counts["error"])
    c[1].metric("🟡 Warnings", counts["warn"])
    c[2].metric("ℹ️ Info", counts["info"])
    st.caption("Invariants checked: net worth = assets − debts · total assets ≥ net worth · no "
               "duplicate transactions · all flows classified · feeds fresh · balances reconcile to transactions.")
    if not summ["issues"]:
        st.success("Everything reconciles — net worth = assets − debts, no duplicates, all flows classified.")
        return
    for i in summ["issues"]:
        icon = {"error": "🔴", "warn": "🟡", "info": "ℹ️"}[i["severity"]]
        with st.expander(f"{icon} {i['message']}", expanded=(i["severity"] == "error")):
            st.caption(f"check: `{i['code']}`")
            if i.get("items"):
                st.dataframe(pd.DataFrame(i["items"]), hide_index=True, use_container_width=True,
                             column_config={"amount": st.column_config.NumberColumn(format="$%.2f")})


# --- sidebar (global, shown on every page) ------------------------------
def _sidebar():
    """Data freshness + one-click refresh, visible on every page so the user is
    never stuck looking at stale balances on a non-Overview tab."""
    if not _has_data():
        return
    with st.sidebar:
        st.divider()
        fr = _bank_freshness()
        if fr["ts"]:
            synced = fr["ts"][:16].replace("T", " ")
            if fr["hours"] is not None and fr["hours"] > 12:
                st.warning(f"⚠️ Bank data {fr['hours']:.0f}h old\n\n_synced {synced}_")
            else:
                st.caption(f"🟢 Bank data synced {synced}")
        if st.button("🔄 Refresh all data", use_container_width=True,
                     help="Pull bank + Splitwise, AI-categorize, update prices/macro"):
            _refresh_all(); st.rerun()


# --- router -------------------------------------------------------------
def _build_nav():
    return st.navigation({
    "Finance": [
        st.Page(page_overview, title="Overview", icon="🏠", default=True),
        st.Page(page_chat, title="Ask AI", icon="💬"),
        st.Page(page_spending, title="Spending", icon="💳"),
        st.Page(page_accounts, title="Accounts", icon="🏦"),
        st.Page(page_investments, title="Investments", icon="📈"),
        st.Page(page_splitwise, title="Splitwise", icon="🤝"),
        st.Page(page_market, title="Market", icon="🌐"),
        st.Page(page_analysis, title="AI Analysis", icon="🧠"),
        st.Page(page_council, title="Council", icon="🏛"),
    ],
    "System": [
        st.Page(page_health, title="Data Health", icon="🩺"),
        st.Page(page_automations, title="Automations", icon="⚙️"),
        st.Page(page_settings, title="Settings", icon="🔧"),
    ],
    })


if __name__ == "__main__":
    nav = _build_nav()
    _sidebar()
    nav.run()
