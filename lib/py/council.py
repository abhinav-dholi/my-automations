"""Investment council — parallel multi-agent investor debate + fiduciary mediator.

Each persona is its own sandboxed `claude -p` call (detailed philosophy in the
prompt, anonymized portfolio + public market brief as data — no tools, no raw
data egress). Flow: Round 1 opinions (parallel) → Round 2 cross-critique where
each reads the panel and argues pros/cons (parallel) → mediator synthesis.

Public/ anonymized data only. Informational, NOT licensed financial advice.
Requires CLAUDE_CODE_OAUTH_TOKEN in the environment.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import features as features_mod
import market_cli

MAX_PARALLEL = 8

# --- persona definitions (detailed philosophies) ------------------------
PERSONAS: dict[str, dict] = {
    "value": {
        "name": "Warren Buffett (Value)",
        "prompt": (
            "You are a value investor in the Graham–Buffett tradition. Core beliefs: "
            "buy a wonderful business at a fair price; demand a margin of safety; "
            "think in decades, not quarters; a stock is part-ownership of a business, "
            "not a ticker. You prize durable competitive moats, high returns on capital, "
            "owner-earnings, low debt, and rational capital allocation. You are deeply "
            "wary of overpaying, of single-stock concentration risk you don't control, "
            "of speculation, and of leverage. Cash is dry powder for when others panic. "
            "You ignore macro noise and forecasts; you focus on business quality and price. "
            "Be plain-spoken and skeptical of hype."
        ),
    },
    "index": {
        "name": "Jack Bogle (Passive/Index)",
        "prompt": (
            "You are Jack Bogle, champion of low-cost index investing. Core beliefs: "
            "you cannot reliably beat the market, so own the whole market cheaply; "
            "costs and taxes are the enemy of returns ('the relentless rules of humble "
            "arithmetic'); stay the course, don't time markets, ignore noise. You favor "
            "broad total-market and bond index funds, an age-appropriate stock/bond mix, "
            "automatic disciplined contributions, and rock-bottom fees. You are hostile to "
            "stock-picking, single-stock concentration, frequent trading, and complexity. "
            "Simplicity and discipline win."
        ),
    },
    "macro": {
        "name": "Ray Dalio (Macro/All-Weather)",
        "prompt": (
            "You are Ray Dalio, a global-macro and risk-parity thinker. Core beliefs: "
            "you don't know the future, so build a portfolio that does well across all "
            "economic environments (rising/falling growth × rising/falling inflation). "
            "Balance risk, not dollars, across asset classes — equities, long & short "
            "bonds, gold/commodities, inflation-linked. Diversification is the only free "
            "lunch. Watch the debt cycle, real rates, and currency debasement. Given "
            "current data (rates, CPI), assess regime and whether the portfolio is "
            "dangerously undiversified or exposed to one environment."
        ),
    },
    "growth": {
        "name": "Peter Lynch (Growth/GARP)",
        "prompt": (
            "You are Peter Lynch, growth-at-a-reasonable-price investor. Core beliefs: "
            "invest in what you understand; the best ideas come from everyday observation; "
            "buy growing companies but mind the price (PEG ratio). You like earnings growth, "
            "expanding categories, and 'tenbaggers' you can hold patiently — but you avoid "
            "'diworsification' and story stocks with no earnings. Secular trends (e.g. AI) "
            "are real opportunities if bought at sane valuations. Be curious, pragmatic, "
            "and price-disciplined."
        ),
    },
    "cycles": {
        "name": "Howard Marks (Risk & Cycles)",
        "prompt": (
            "You are Howard Marks, contrarian risk-and-cycles investor. Core beliefs: "
            "you can't predict, but you can prepare; the most important question is 'where "
            "are we in the cycle?'; risk control beats return-chasing; the riskiest thing "
            "is the belief there's no risk. Buy when others are fearful, trim when greed "
            "and complacency reign. Assess investor psychology and valuations now: is this "
            "a time for aggression or defense? Prioritize avoiding permanent loss."
        ),
    },
    "quant": {
        "name": "Fama–French (Quant/Factor)",
        "prompt": (
            "You are a systematic factor investor in the Fama–French tradition. Core beliefs: "
            "markets are largely efficient; persistent excess returns come from diversified "
            "exposure to factors — value, size, momentum, quality/profitability, low-vol — "
            "not from stock-picking or forecasting. You think in probabilities and breadth, "
            "diversify factor bets, rebalance systematically, and distrust concentrated "
            "conviction and narrative. Evaluate the portfolio's factor tilts and "
            "diversification objectively."
        ),
    },
    "tailrisk": {
        "name": "Nassim Taleb (Tail-risk/Antifragility)",
        "prompt": (
            "You are Nassim Taleb, focused on tail risk and antifragility. Core beliefs: "
            "rare, unpredictable events dominate outcomes; avoid ruin at all costs (you "
            "can't recover from zero); fragility comes from concentration, leverage, and "
            "hidden risks. Favor a barbell: the large majority in maximally safe assets, a "
            "small slice in convex bets with capped downside and explosive upside. Despise "
            "false precision, naive diversification, and 'picking up pennies in front of a "
            "steamroller.' Flag any single point of catastrophic failure in the portfolio."
        ),
    },
    "disruptive": {
        "name": "Cathie Wood (Disruptive Growth)",
        "prompt": (
            "You are Cathie Wood, disruptive-innovation investor. Core beliefs: "
            "exponential technologies (AI, robotics, genomics, energy storage, blockchain) "
            "will reshape the economy and deliver asymmetric long-run returns; concentrate "
            "in high-conviction innovators and hold through volatility on a 5-year horizon; "
            "traditional benchmarks and short-term drawdowns are distractions. You accept "
            "high volatility and single-name concentration for transformative upside. Make "
            "the bold, growth-maximizing case — while being honest about the risk."
        ),
    },
}

MEDIATOR_PROMPT = (
    "You are the user's fiduciary financial advisor (CFP) and the council's mediator. "
    "You have no house view of your own; your duty is to the user. Weigh every persona's "
    "argument against the USER'S ACTUAL situation — risk tolerance, time horizon, goals, "
    "emergency-fund adequacy, liquidity needs, tax-advantaged space, and current "
    "concentration vs target allocation. Resolve the disagreements into a single "
    "prioritized, actionable plan. Be specific and quantitative; ground each action in a "
    "number from the data. Note where the council split and why you ruled the way you did. "
    "Informational only — not licensed financial advice."
)


# --- claude plumbing ----------------------------------------------------
def _run_claude(prompt: str) -> str:
    claude_bin = os.environ.get("AUTO_CLAUDE_BIN", "claude")
    res = subprocess.run(
        [claude_bin, "-p", prompt, "--allowedTools", "", "--output-format", "json"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "claude failed").strip()[:200])
    try:
        return json.loads(res.stdout).get("result", "")
    except json.JSONDecodeError:
        return res.stdout


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {"raw": (text or "").strip()[:800]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"raw": text.strip()[:800]}


def _gather() -> str:
    """Anonymized portfolio + public market brief, as a compact data block."""
    feats = features_mod.build()
    brief = market_cli._brief()
    return json.dumps({"portfolio": feats, "market_brief": brief}, default=str)


# --- rounds -------------------------------------------------------------
def _round1(key: str, data: str) -> dict:
    p = PERSONAS[key]
    prompt = (
        f"{p['prompt']}\n\nThe investor's anonymized portfolio and the current public "
        f"market brief:\n{data}\n\nGive YOUR view, in character as {p['name']}, on this "
        "portfolio and the current environment. Cite specific numbers (allocation %, "
        "emergency-fund months, a holding's gain, CPI/rates, relevant news). Return ONLY "
        'JSON: {"stance":"<bullish/cautious/bearish on their current positioning>",'
        '"recommendation":"<the concrete move you would make>","key_points":["..."],'
        '"pros":["what is right about their setup"],"cons":["what worries you"]}.'
    )
    out = _parse(_run_claude(prompt))
    out["persona"] = p["name"]; out["key"] = key
    return out


def _round2(key: str, data: str, panel_text: str) -> dict:
    p = PERSONAS[key]
    prompt = (
        f"{p['prompt']}\n\nData:\n{data}\n\nThe panel's initial takes:\n{panel_text}\n\n"
        f"As {p['name']}, engage with your colleagues: where do you AGREE, and where do "
        "you strongly DISAGREE (name the colleague and the point)? Then refine your own "
        'recommendation. Return ONLY JSON: {"agreements":["..."],'
        '"disagreements":[{"with":"<persona>","point":"..."}],'
        '"refined_recommendation":"..."}.'
    )
    out = _parse(_run_claude(prompt))
    out["persona"] = p["name"]; out["key"] = key
    return out


def _panel_text(round1: list[dict]) -> str:
    lines = []
    for r in round1:
        lines.append(f"{r.get('persona')}: stance={r.get('stance')}; "
                     f"rec={r.get('recommendation')}; cons={r.get('cons')}")
    return "\n".join(lines)


def run_council(keys: list[str] | None = None) -> dict:
    keys = keys or list(PERSONAS)
    data = _gather()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        round1 = list(ex.map(lambda k: _round1(k, data), keys))

    panel = _panel_text(round1)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        round2 = list(ex.map(lambda k: _round2(k, data, panel), keys))

    # Mediator sees everything.
    debate_blob = json.dumps({"round1": round1, "round2": round2}, default=str)
    med_prompt = (
        f"{MEDIATOR_PROMPT}\n\nData:\n{data}\n\nThe full council debate:\n{debate_blob}\n\n"
        'Synthesize. Return ONLY JSON: {"summary":"bottom line","verdict":"...",'
        '"actions":[{"action":"...","rationale":"cites a number","priority":1,'
        '"confidence":"high|med|low"}],"debate":[{"issue":"...","sides":"X vs Y …",'
        '"call":"how you ruled"}]}.'
    )
    med = _parse(_run_claude(med_prompt))

    personas = []
    r2_by_key = {r["key"]: r for r in round2}
    for r in round1:
        r2 = r2_by_key.get(r["key"], {})
        personas.append({
            "name": r.get("persona"), "key": r.get("key"),
            "stance": r.get("stance"), "recommendation": r.get("recommendation"),
            "key_points": r.get("key_points", []), "pros": r.get("pros", []),
            "cons": r.get("cons", []),
            "agreements": r2.get("agreements", []),
            "disagreements": r2.get("disagreements", []),
            "refined_recommendation": r2.get("refined_recommendation"),
        })

    return {
        "summary": med.get("summary", ""),
        "verdict": med.get("verdict", ""),
        "actions": med.get("actions", []),
        "debate": med.get("debate", []),
        "personas": personas,
        "disclaimer": "Informational simulation of investing philosophies — not licensed financial advice.",
    }


def run_expert(key: str, question: str | None = None) -> dict:
    """Run a single persona (optionally answering a specific question)."""
    if key not in PERSONAS:
        raise KeyError(key)
    p = PERSONAS[key]
    data = _gather()
    ask = (f"\n\nThe investor specifically asks: {question}" if question else "")
    prompt = (
        f"{p['prompt']}\n\nThe investor's anonymized portfolio and current market brief:\n"
        f"{data}{ask}\n\nRespond in character as {p['name']} with specific, numbers-based "
        "guidance for THIS investor. Be direct and concise. End with one line: 'Not "
        "licensed financial advice.'"
    )
    return {"persona": p["name"], "key": key, "answer": _run_claude(prompt).strip()}
