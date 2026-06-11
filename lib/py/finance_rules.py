"""Deterministic, local transaction categorization (label_source='rule').

First match wins. Agents refine 'Other'/low-confidence labels later (§14.3)
without touching these rule labels.
"""
from __future__ import annotations

CATEGORY_RULES = [
    ("Groceries", ["whole foods", "trader joe", "safeway", "grocery", "aldi", "costco"]),
    ("Dining", ["restaurant", "coffee", "starbucks", "doordash", "uber eats", "chipotle"]),
    ("Transport", ["uber", "lyft", "shell", "chevron", "gas", "mta", "transit", "parking"]),
    ("Shopping", ["amazon", "target", "walmart", "best buy"]),
    ("Subscriptions", ["netflix", "spotify", "hulu", "icloud", "prime", "youtube"]),
    ("Utilities", ["electric", "water", "comcast", "verizon", "at&t", "internet"]),
]


def categorize(description: str, amount: float) -> str:
    if amount > 0:
        return "Income"
    desc = (description or "").lower()
    for category, keywords in CATEGORY_RULES:
        if any(kw in desc for kw in keywords):
            return category
    return "Other"
