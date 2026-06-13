"""Structured market data — Finnhub (prices) + FRED (macro). Both free keys.

Public data only; no PII. Keys: FINNHUB_API_KEY, FRED_API_KEY.
"""
from __future__ import annotations

import requests

import scrub

FINNHUB = "https://finnhub.io/api/v1"
FRED = "https://api.stlouisfed.org/fred/series/observations"

# (FRED series id, human label). CPI handled separately as YoY.
MACRO_SERIES = [
    ("FEDFUNDS", "Fed funds rate %"),
    ("DGS10", "10y Treasury yield %"),
    ("UNRATE", "Unemployment %"),
]


def quote(api_key: str, symbol: str) -> float | None:
    """Latest price for a US stock/ETF via Finnhub. None if unavailable."""
    scrub.register(api_key)
    r = requests.get(f"{FINNHUB}/quote", params={"symbol": symbol, "token": api_key}, timeout=20)
    if r.status_code != 200:
        return None
    c = r.json().get("c")
    return float(c) if c else None


def _fred(api_key: str, series_id: str, limit: int) -> list[dict]:
    scrub.register(api_key)
    r = requests.get(FRED, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "desc", "limit": limit,
    }, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(scrub.clean(f"FRED {series_id} failed {r.status_code}: {r.text[:120]}"))
    return r.json().get("observations", [])


def fred_latest(api_key: str, series_id: str) -> tuple[str, float] | None:
    obs = _fred(api_key, series_id, 1)
    for o in obs:
        if o.get("value") not in (".", None, ""):
            return o["date"], float(o["value"])
    return None


def fred_cpi_yoy(api_key: str) -> tuple[str, float] | None:
    """Year-over-year CPI inflation % from CPIAUCSL (needs ~13 monthly obs)."""
    obs = [o for o in _fred(api_key, "CPIAUCSL", 14) if o.get("value") not in (".", None, "")]
    if len(obs) < 13:
        return None
    latest, year_ago = float(obs[0]["value"]), float(obs[12]["value"])
    if not year_ago:
        return None
    return obs[0]["date"], round((latest / year_ago - 1) * 100, 2)
