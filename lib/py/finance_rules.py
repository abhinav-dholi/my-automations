"""Deterministic, local transaction categorization (label_source='rule').

First match wins. Agents refine 'Other'/low-confidence labels later (§14.3)
without touching these rule labels.

NON_SPEND categories are excluded from spend totals — internal transfers,
credit-card payments, and investment contributions are money movement, not
consumption, and would otherwise massively inflate spend across accounts.
"""
from __future__ import annotations

# Money-movement / inflows that are NOT consumption and NOT recurring income.
# Excluded from spend; only the "Income" category counts toward income.
NON_SPEND = {"Income", "Transfer", "Payment", "Investment", "Credit"}

CATEGORY_RULES = [
    # Recurring income (payroll / stipend) — must be matched explicitly; a
    # positive amount alone is NOT assumed to be income.
    ("Income", ["payroll", "direct deposit", "dir dep", "dirdep", "salary",
                "stipend", "meta platforms", "meta payroll", "gusto", "adp ",
                "workday", "paychex", "paycheck"]),
    # Money movement (these must win over spend categories).
    ("Payment", ["payment thank you", "autopay", "online payment", "auto pay",
                 "credit card payment", "card payment", "bill pay", "billpay",
                 "pymt", "e-payment", "epay", "ach pymt", "mobile payment"]),
    ("Transfer", ["transfer", "xfer", "wire ", "online transfer", "internal transfer",
                  "zelle", "venmo", "cash app", "apple cash", "withdrawal", "deposit to"]),
    ("Investment", ["robinhood", "wealthfront", "betterment", "acorns",
                    "brokerage", "td ameritrade", "etrade", "e*trade",
                    "fidelity", "schwab", "vanguard", "coinbase", "401k", "ira contrib"]),
    # Spend categories.
    ("Groceries", ["whole foods", "trader joe", "safeway", "grocery", "aldi",
                   "costco", "wegmans", "kroger", "publix", "h mart", "patel"]),
    ("Dining", ["restaurant", "coffee", "starbucks", "doordash", "uber eats",
                "chipotle", "grubhub", "mcdonald", "cafe", "pizza", "bar ", "tst*"]),
    ("Transport", ["uber", "lyft", "shell", "chevron", "exxon", "gas ", "fuel",
                   "mta", "transit", "parking", "toll", "metro", "amtrak"]),
    ("Travel", ["airline", "airlines", "hotel", "airbnb", "expedia", "delta",
                "united", "marriott", "hilton", "tsa", "flight"]),
    ("Shopping", ["amazon", "amzn", "target", "walmart", "best buy", "ebay",
                  "etsy", "ikea", "apple store"]),
    ("Subscriptions", ["netflix", "spotify", "hulu", "icloud", "prime video",
                       "youtube", "patreon", "openai", "chatgpt", "disney+",
                       "github", "notion", "adobe"]),
    ("Utilities", ["electric", "water", "comcast", "xfinity", "verizon", "at&t",
                   "t-mobile", "internet", "pg&e", "con ed", "utility"]),
    ("Health", ["pharmacy", "cvs", "walgreens", "doctor", "dental", "clinic",
                "hospital", "fitness", "gym", "insurance"]),
    ("Rent", ["rent", "landlord", "property mgmt", "apartment", "leasing"]),
]


def categorize(description: str, amount: float) -> str:
    desc = (description or "").lower()
    for category, keywords in CATEGORY_RULES:
        if any(kw in desc for kw in keywords):
            return category
    # Unmatched inflows are NOT assumed to be income — they're generic credits
    # (refunds, reimbursements, external transfers). Mark the real stipend as
    # Income in the dashboard and the classifier will learn it.
    if amount > 0:
        return "Credit"
    return "Other"
