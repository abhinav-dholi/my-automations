"""Minimal Splitwise API v3.0 client (read-only, Bearer API key).

Free personal API key from https://dev.splitwise.com (register an app → API
key). Auth: `Authorization: Bearer <key>`. Base https://secure.splitwise.com.

Balance sign convention (per Splitwise friends endpoint): a friend balance
`amount > 0` means that friend owes YOU (you are owed); `< 0` means you owe
them. Verify against the app after the first sync; flip OWED_SIGN if reversed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

import scrub

BASE = "https://secure.splitwise.com/api/v3.0"
OWED_SIGN = 1  # multiply friend balance by this so + == owed to me


def _headers(api_key: str) -> dict:
    scrub.register(api_key)
    return {"Authorization": f"Bearer {api_key}"}


def _get(api_key: str, path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{BASE}/{path}", headers=_headers(api_key),
                        params=params or {}, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError(
            scrub.clean(f"Splitwise auth failed {resp.status_code} — check SPLITWISE_API_KEY.")
        )
    if resp.status_code != 200:
        raise RuntimeError(scrub.clean(f"Splitwise {path} failed {resp.status_code}: {resp.text}"))
    return resp.json()


def current_user_id(api_key: str) -> int:
    return int(_get(api_key, "get_current_user")["user"]["id"])


@dataclass
class FriendBalance:
    friend_id: int
    name: str
    currency: str
    amount: float  # + == owed to me (after OWED_SIGN)


def get_friend_balances(api_key: str) -> list[FriendBalance]:
    """Net balance per friend (consolidated across groups + non-group)."""
    out = []
    for f in _get(api_key, "get_friends").get("friends", []):
        name = " ".join(x for x in [f.get("first_name"), f.get("last_name")] if x)
        for b in f.get("balance", []):
            out.append(FriendBalance(
                friend_id=int(f["id"]), name=name or str(f["id"]),
                currency=b.get("currency_code", "USD"),
                amount=float(b.get("amount", "0")) * OWED_SIGN,
            ))
    return out


@dataclass
class Expense:
    id: int
    date: str
    description: str
    currency: str
    cost: float
    my_owed_share: float
    my_paid_share: float
    group_id: int | None


def get_expenses(api_key: str, my_id: int, dated_after_iso: str,
                 limit: int = 200) -> list[Expense]:
    data = _get(api_key, "get_expenses",
                {"dated_after": dated_after_iso, "limit": limit})
    out = []
    for e in data.get("expenses", []):
        if e.get("deleted_at"):
            continue
        mine = next((u for u in e.get("users", []) if int(u["user_id"]) == my_id), None)
        out.append(Expense(
            id=int(e["id"]),
            date=e.get("date", ""),
            description=e.get("description", ""),
            currency=e.get("currency_code", "USD"),
            cost=float(e.get("cost", "0") or 0),
            my_owed_share=float(mine["owed_share"]) if mine else 0.0,
            my_paid_share=float(mine["paid_share"]) if mine else 0.0,
            group_id=int(e["group_id"]) if e.get("group_id") not in (None, 0) else None,
        ))
    return out
