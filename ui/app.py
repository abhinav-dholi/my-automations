"""Local Streamlit dashboard for my-automations.

Launch: `python cli/auto ui` (binds 127.0.0.1 only — never expose finance data).
Thin view layer over the same lib/py the CLI uses: manifests, run logs, the
finance DB, and the `auto` runner for triggering.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib" / "py"))

import config  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import runlog  # noqa: E402
import yaml  # noqa: E402

st.set_page_config(page_title="my-automations", page_icon="🤖", layout="wide")


# --- helpers ------------------------------------------------------------

def _latest_runs() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for rec in runlog.read_recent(500):
        latest[rec["id"]] = rec  # read order is oldest→newest, so last wins
    return latest


def _run_automation(automation_id: str):
    return subprocess.run(
        [sys.executable, "cli/auto", "run", automation_id],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )


def _load_json(name: str):
    import json
    p = config.DATA_DIR / name
    if p.exists():
        return json.loads(p.read_text())
    return None


# --- pages --------------------------------------------------------------

def page_automations():
    st.header("Automations")
    manifests = manifest_mod.discover()
    latest = _latest_runs()

    if not manifests:
        st.info("No automations yet.")
        return

    rows = []
    for m in manifests:
        last = latest.get(m.id, {})
        rows.append({
            "id": m.id, "runtime": m.runtime,
            "triggers": ", ".join(m.triggers),
            "schedule": m.schedule or "—",
            "deploy": m.deploy_target,
            "last status": last.get("status", "—"),
            "last run": last.get("end", "—"),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.subheader("Run now")
    ids = [m.id for m in manifests]
    chosen = st.selectbox("Automation", ids)
    if st.button(f"▶ Run {chosen}", type="primary"):
        with st.spinner(f"Running {chosen}…"):
            res = _run_automation(chosen)
        (st.success if res.returncode == 0 else st.error)(
            f"{chosen} exited {res.returncode}"
        )
        if res.stdout:
            st.code(res.stdout, language="text")
        if res.stderr:
            st.code(res.stderr, language="text")


def page_runs():
    st.header("Run history")
    recs = list(reversed(runlog.read_recent(200)))  # newest first
    if not recs:
        st.info("No runs logged yet.")
        return
    ids = sorted({r["id"] for r in recs})
    flt = st.multiselect("Filter by automation", ids, default=ids)
    st.dataframe(
        [r for r in recs if r["id"] in flt],
        use_container_width=True, hide_index=True,
    )


def page_finance():
    import pandas as pd
    st.header("Finance")

    weekly = _load_json("finance-weekly.json")
    analysis = _load_json("finance-analysis.json")

    if weekly:
        c1, c2, c3 = st.columns(3)
        c1.metric("Spend (7d)", f"${weekly['total_spend']:,.2f}")
        c2.metric("Income (7d)", f"${weekly['total_income']:,.2f}")
        c3.metric("Transactions", weekly["txn_count"])

        if weekly.get("by_category"):
            st.subheader("Spend by category (7d)")
            df = pd.DataFrame(
                {"category": list(weekly["by_category"]),
                 "amount": list(weekly["by_category"].values())}
            ).set_index("category")
            st.bar_chart(df)

        if weekly.get("balances"):
            st.subheader("Account balances")
            st.dataframe(
                [{"account": k, "balance": v} for k, v in weekly["balances"].items()],
                use_container_width=True, hide_index=True,
            )
        if weekly.get("splitwise_net"):
            st.subheader("Splitwise (＋ owed to you)")
            st.dataframe(
                [{"currency": k, "net": v} for k, v in weekly["splitwise_net"].items()],
                use_container_width=True, hide_index=True,
            )
    else:
        st.info("No finance digest yet. Run `finance-sync` then `finance-weekly`.")

    st.divider()
    st.subheader("Latest analysis (finance-analyze)")
    if analysis:
        if analysis.get("summary"):
            st.write(analysis["summary"])
        if analysis.get("monthly_investable") is not None:
            st.metric("Est. monthly investable", f"${analysis['monthly_investable']:,.2f}")
        cur, tgt = analysis.get("allocation_current"), analysis.get("allocation_target")
        if cur or tgt:
            keys = sorted(set(cur or {}) | set(tgt or {}))
            st.dataframe(
                [{"asset": k,
                  "current": (cur or {}).get(k),
                  "target": (tgt or {}).get(k)} for k in keys],
                use_container_width=True, hide_index=True,
            )
        for a in analysis.get("actions", []):
            st.markdown(f"- **[{a.get('priority','?')}]** {a.get('action','')} — _{a.get('rationale','')}_")
        st.caption("Informational analysis from your own data — not licensed financial advice.")
    else:
        st.info("No analysis yet. Run `finance-analyze` (needs CLAUDE_CODE_OAUTH_TOKEN).")


def page_config():
    st.header("Edit configuration")
    st.caption("Edit safe manifest fields. Secret **values** are never shown or edited here — manage those in Keychain.")
    manifests = manifest_mod.discover()
    if not manifests:
        st.info("No automations.")
        return

    chosen = st.selectbox("Automation", [m.id for m in manifests])
    m = manifest_mod.get(chosen)
    path = m.dir / "automation.yaml"
    raw = yaml.safe_load(path.read_text())

    desc = st.text_area("Description", raw.get("description", ""))
    triggers = st.multiselect(
        "Triggers", ["cli", "schedule", "skill", "webhook"], raw.get("triggers", [])
    )
    schedule = st.text_input("Schedule (local cron)", raw.get("schedule", "") or "")
    tz = st.text_input("Timezone", raw.get("tz", "") or "")
    deploy_target = st.selectbox(
        "Deploy target", ["none", "local-cron", "github-actions"],
        index=["none", "local-cron", "github-actions"].index(
            (raw.get("deploy") or {}).get("target", "none")
        ),
    )
    st.write("**Declared secret names** (values live in Keychain):")
    st.code("\n".join(raw.get("secrets", []) or ["(none)"]), language="text")

    if st.button("💾 Save", type="primary"):
        raw["description"] = desc
        raw["triggers"] = triggers
        if schedule.strip():
            raw["schedule"] = schedule.strip()
        else:
            raw.pop("schedule", None)
        if tz.strip():
            raw["tz"] = tz.strip()
        raw["deploy"] = {"target": deploy_target}
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        try:
            manifest_mod.load(path)
            st.success(f"Saved {path.name} ✓ (valid)")
        except manifest_mod.ManifestError as e:
            st.error(f"Saved but INVALID — fix it: {e}")


# --- router -------------------------------------------------------------

nav = st.navigation([
    st.Page(page_automations, title="Automations", icon="🤖"),
    st.Page(page_runs, title="Runs", icon="📜"),
    st.Page(page_finance, title="Finance", icon="💰"),
    st.Page(page_config, title="Config", icon="⚙️"),
])
nav.run()
