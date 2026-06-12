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
    since = int(time.time()) - 90 * 86400
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


# --- shared chart helpers ----------------------------------------------

def donut(labels, values, title=""):
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.62,
                           marker=dict(colors=PALETTE)))
    fig.update_traces(textinfo="percent+label", textposition="inside")
    fig.update_layout(template=PLOT_TEMPLATE, title=title, showlegend=False,
                      height=320, margin=dict(t=40, b=10, l=10, r=10))
    return fig


# --- pages --------------------------------------------------------------

def page_overview():
    st.title("Overview")
    if not _has_data():
        st.info("No finance data yet. Run `finance-sync` first.")
        return
    f = get_features()
    db = load_finance_db()
    cf, res, port = f["cashflow"], f["resilience"], f["portfolio"]

    c = st.columns(6)
    c[0].metric("Net worth", _money(f["net_worth"]))
    c[1].metric("Monthly income", _money(cf["monthly_income"]))
    c[2].metric("Monthly spend", _money(cf["monthly_spend"]))
    c[3].metric("Savings rate", f"{cf['savings_rate']*100:.0f}%")
    c[4].metric("Investable / mo", _money(cf["monthly_investable_estimate"]))
    em = res.get("emergency_fund_months")
    c[5].metric("Emergency fund", f"{em:.1f} mo" if em is not None else "—")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Net worth trend")
        # Net worth = sum of all account balances (investment balances already
        # include holdings — adding holdings again would double-count).
        bs = pd.DataFrame(db["bal_series"]).rename(columns={"total": "net worth"})
        if not bs.empty:
            m = bs.sort_values("as_of").copy()
            m["t"] = pd.to_datetime(m["as_of"], errors="coerce")
            fig = px.area(m, x="t", y="net worth", template=PLOT_TEMPLATE,
                          color_discrete_sequence=[ACCENT])
            fig.update_layout(height=340, margin=dict(t=20, b=10, l=10, r=10),
                              xaxis_title=None, yaxis_title=None)
            st.plotly_chart(fig, use_container_width=True)
            if len(m) < 2:
                st.caption("Trend builds as `finance-sync` runs over time (one point so far).")
    with right:
        st.subheader("Asset mix")
        liquid = res.get("liquid_cash", 0) or 0
        invested = port.get("invested_total", 0) or 0
        sw_net = sum(v for v in f["splitwise"]["net_balance_by_currency"].values())
        labels, vals = [], []
        for lbl, v in [("Liquid cash", liquid), ("Investments", invested),
                       ("Splitwise (net)", max(sw_net, 0))]:
            if v > 0:
                labels.append(lbl); vals.append(v)
        if vals:
            st.plotly_chart(donut(labels, vals), use_container_width=True)

    st.subheader("Income vs spend (monthly)")
    bar = go.Figure([
        go.Bar(name="Income", x=["monthly"], y=[cf["monthly_income"]], marker_color="#22c55e"),
        go.Bar(name="Spend", x=["monthly"], y=[cf["monthly_spend"]], marker_color="#ef4444"),
        go.Bar(name="Investable", x=["monthly"], y=[cf["monthly_investable_estimate"]], marker_color=ACCENT),
    ])
    bar.update_layout(template=PLOT_TEMPLATE, barmode="group", height=280,
                      margin=dict(t=20, b=10, l=10, r=10))
    st.plotly_chart(bar, use_container_width=True)
    st.caption(f"Spend excludes {_money(cf.get('excluded_movement_total',0))} of "
               "transfers / card payments / investment moves.")


def page_spending():
    st.title("Spending")
    if not _has_data():
        st.info("No data yet."); return
    db = load_finance_db()
    df = pd.DataFrame(db["txns"])
    if df.empty:
        st.info("No transactions."); return

    # Spend = negative, excluding money-movement categories.
    spend = df[(df["amount"] < 0) & (~df["category"].isin(finance_rules.NON_SPEND))].copy()
    spend["spend"] = spend["amount"].abs()
    spend["date"] = pd.to_datetime(spend["posted"], unit="s")

    total = spend["spend"].sum()
    c = st.columns(3)
    c[0].metric("Total spend (90d)", _money(total))
    c[1].metric("Transactions", len(spend))
    c[2].metric("Top category", spend.groupby("category")["spend"].sum().idxmax() if len(spend) else "—")

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

    st.subheader("Transactions")
    cats = sorted(spend["category"].unique())
    pick = st.multiselect("Category", cats, default=cats)
    view = spend[spend["category"].isin(pick)].sort_values("date", ascending=False)
    st.dataframe(
        view[["date", "description", "category", "spend"]].rename(columns={"spend": "amount"}),
        use_container_width=True, hide_index=True,
    )


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
    a = _load_json("finance-analysis.json")
    if st.button("▶ Run analysis now (~2 min)", type="primary"):
        with st.spinner("Claude analyzing…"):
            res = subprocess.run([sys.executable, "cli/auto", "run", "finance-analyze"],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True)
        (st.success if res.returncode == 0 else st.error)(f"exited {res.returncode}")
        st.cache_data.clear()
        a = _load_json("finance-analysis.json")

    if not a:
        st.info("No analysis yet. Click Run, or trigger /analyze in Telegram."); return

    if a.get("summary"):
        st.markdown(f"#### Summary\n{a['summary']}")
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
            st.markdown(f"**{act.get('priority','?')}. {act.get('action','')}**  \n"
                        f"<span style='opacity:.7'>{act.get('rationale','')}</span>",
                        unsafe_allow_html=True)


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
        st.Page(page_analysis, title="AI Analysis", icon="🧠"),
    ],
    "System": [
        st.Page(page_automations, title="Automations", icon="⚙️"),
        st.Page(page_settings, title="Settings", icon="🔧"),
    ],
    })


if __name__ == "__main__":
    _build_nav().run()
