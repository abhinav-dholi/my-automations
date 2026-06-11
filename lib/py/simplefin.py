"""Minimal SimpleFIN Bridge client (read-only bank aggregation).

Flow:
  1. One-time: claim a setup token -> Access URL (contains Basic auth creds).
  2. Repeatedly: GET {access_url}/accounts?start-date=..&end-date=.. -> JSON.

The Access URL is the single credential, stored as SIMPLEFIN_ACCESS_URL.
A 403 means the token is compromised/revoked — surfaced as TokenRevoked so
callers can stop and notify rather than retry (§11.4).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests

import scrub


class TokenRevoked(Exception):
    """SimpleFIN returned 403 — credentials revoked or compromised."""


def claim_access_url(setup_token: str) -> str:
    """Exchange a one-time setup token for a durable Access URL.

    Run once during setup; store the result as SIMPLEFIN_ACCESS_URL.
    """
    claim_url = base64.b64decode(setup_token).decode("utf-8").strip()
    resp = requests.post(claim_url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            scrub.clean(f"Claim failed {resp.status_code}: {resp.text}")
        )
    access_url = resp.text.strip()
    scrub.register(access_url)
    return access_url


@dataclass
class Transaction:
    id: str
    posted: int          # unix seconds
    amount: float
    description: str
    pending: bool


@dataclass
class Holding:
    symbol: str
    name: str
    quantity: float
    cost_basis: float
    market_value: float


@dataclass
class Account:
    id: str
    name: str
    currency: str
    balance: float
    transactions: list[Transaction]
    holdings: list[Holding]


def get_accounts(access_url: str, start: int, end: int,
                 pending: bool = False) -> list[Account]:
    """Fetch accounts + transactions in [start, end] (unix seconds)."""
    scrub.register(access_url)
    params = {"start-date": start, "end-date": end}
    if pending:
        params["pending"] = 1

    resp = requests.get(f"{access_url}/accounts", params=params, timeout=60)
    if resp.status_code == 403:
        raise TokenRevoked("SimpleFIN returned 403 — Access URL revoked/compromised.")
    if resp.status_code == 402:
        raise RuntimeError(
            "SimpleFIN returned 402 Payment Required — your SimpleFIN Bridge "
            "subscription isn't active. Pay/activate at the Bridge, then retry."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            scrub.clean(f"SimpleFIN fetch failed {resp.status_code}: {resp.text}")
        )

    data = resp.json()
    for err in data.get("errlist") or []:
        # errlist entries are non-fatal connection issues — log, don't crash.
        print(f"[simplefin] errlist: {err.get('code')} {err.get('msg')}")

    accounts = []
    for a in data.get("accounts", []):
        txns = [
            Transaction(
                id=t.get("id", ""),
                posted=int(t.get("posted", 0)),
                amount=float(t.get("amount", "0")),
                description=t.get("description", ""),
                pending=bool(t.get("pending", False)),
            )
            for t in a.get("transactions", [])
        ]
        holdings = [
            Holding(
                symbol=h.get("symbol") or h.get("description", ""),
                name=h.get("description", ""),
                quantity=float(h.get("shares", "0") or 0),
                cost_basis=float(h.get("cost_basis", "0") or 0),
                market_value=float(h.get("market_value", "0") or 0),
            )
            for h in a.get("holdings", [])  # present on investment accounts
        ]
        accounts.append(
            Account(
                id=a.get("id", ""),
                name=a.get("name", ""),
                currency=a.get("currency", "USD"),
                balance=float(a.get("balance", "0")),
                transactions=txns,
                holdings=holdings,
            )
        )
    return accounts
