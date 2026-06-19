"""Derive an account SUBTYPE — a refinement of the coarse type (checking/savings/
credit/investment) — so the app can label accounts without the user tagging them.

SimpleFIN provides no account type or yield, so we combine three signals:
  • the coarse `type` already inferred from the name,
  • the institution name / domain (SimpleFIN's `org`), and
  • behavioural evidence: implied APY from interest credits (for HYSA detection).

Keywords mirror features.py's taxable-vs-retirement split so labels and the
financial model agree. This is display/UX metadata; it does NOT change the
accuracy model (an HYSA is still 'savings'/liquid cash, a brokerage still
'investment').
"""
from __future__ import annotations

RETIRE_KW = ("401", "ira", "roth", "retirement", "403b", "hsa", "health savings", "pension")
EQUITY_KW = ("equity award", "rsu", "espp", "stock plan", "restricted stock")
# Known high-yield savings products / fintechs — used for cold-start, before any
# interest has posted to prove the rate behaviourally.
HYSA_KW = ("premium savings", "high yield", "high-yield", "performance savings",
           "marcus", "ally bank", "sofi", "wealthfront", "betterment", "apple savings",
           "360 performance", "yield savings", "online savings")
HYSA_DOMAINS = ("us.etrade.com", "marcus.com", "ally.com", "sofi.com",
                "wealthfront.com", "betterment.com")
HYSA_APY_FLOOR = 0.03          # ≥3% annualized → high-yield (vs ~0% at big banks)

LABELS = {
    "hysa": "High-yield savings",
    "brokerage": "Taxable brokerage",
    "retirement": "Retirement (tax-advantaged)",
    "equity_awards": "Equity awards / employer stock",
}
ICONS = {"hysa": "💸", "brokerage": "📈", "retirement": "🔒", "equity_awards": "🎯"}


def subtype(*, name: str, base_type: str, org_domain: str = "",
            implied_apy: float | None = None) -> str:
    """Return a subtype slug ('' if none applies). `implied_apy`, when known,
    overrides product/name heuristics for savings (behaviour beats branding)."""
    n = (name or "").lower()
    dom = (org_domain or "").lower()
    if base_type == "investment":
        if any(k in n for k in RETIRE_KW):
            return "retirement"
        if any(k in n for k in EQUITY_KW):
            return "equity_awards"
        return "brokerage"
    if base_type == "savings":
        if implied_apy is not None:                 # measured rate is the truth
            return "hysa" if implied_apy >= HYSA_APY_FLOOR else ""
        if any(k in n for k in HYSA_KW) or any(d in dom for d in HYSA_DOMAINS):
            return "hysa"
        return ""
    return ""


def label(subtype_slug: str | None) -> str:
    return LABELS.get(subtype_slug or "", "")


def icon(subtype_slug: str | None) -> str:
    return ICONS.get(subtype_slug or "", "")
