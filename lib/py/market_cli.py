"""market_cli — tools the market-watch / finance-council Claude runs may call.

All data here is PUBLIC (tickers, prices, macro, news) — no PII. Subcommands:
  tickers       held tickers + benchmarks (what to research)
  brief         combined market brief JSON (prices + macro + recent news)
  store-news    persist news items (JSON array via --json)
  write-brief   assemble data/market-brief.json from the DB
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import config
import store

BENCHMARKS = ["SPY", "QQQ", "AGG", "GLD", "VTI"]


def _brief() -> dict:
    with store.connect() as conn:
        prices = {r["symbol"]: r["close"] for r in store.latest_prices(conn)}
        macro = {r["series"]: {"value": r["value"], "label": r["label"], "date": r["date"]}
                 for r in store.latest_macro(conn)}
        news = [
            {"source": r["source"], "headline": r["headline"], "summary": r["summary"],
             "url": r["url"], "tickers": json.loads(r["tickers"] or "[]"),
             "relevance": r["relevance"], "fetched_at": r["fetched_at"]}
            for r in store.recent_news(conn, 25)
        ]
        held = store.held_symbols(conn)
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "held_tickers": held, "benchmarks": BENCHMARKS,
            "prices": prices, "macro": macro, "news": news}


def cmd_tickers(_a) -> int:
    with store.connect() as conn:
        held = store.held_symbols(conn)
    print(json.dumps({"held": held, "benchmarks": BENCHMARKS}))
    return 0


def cmd_brief(_a) -> int:
    print(json.dumps(_brief(), indent=2))
    return 0


def cmd_store_news(args) -> int:
    items = json.loads(args.json)
    if isinstance(items, dict):
        items = [items]
    store.init_db()
    with store.connect() as conn:
        for it in items:
            store.add_news(conn, source=it.get("source", ""), headline=it.get("headline", ""),
                           summary=it.get("summary", ""), url=it.get("url", ""),
                           tickers=it.get("tickers", []), relevance=it.get("relevance", ""))
    print(f"stored {len(items)} news item(s)")
    return 0


def cmd_write_brief(_a) -> int:
    config.ensure_runtime_dirs()
    (config.DATA_DIR / "market-brief.json").write_text(json.dumps(_brief(), indent=2))
    print("wrote data/market-brief.json")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="market_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tickers").set_defaults(func=cmd_tickers)
    sub.add_parser("brief").set_defaults(func=cmd_brief)
    sub.add_parser("write-brief").set_defaults(func=cmd_write_brief)
    ps = sub.add_parser("store-news")
    ps.add_argument("--json", required=True, help="JSON array of news items (single-line)")
    ps.set_defaults(func=cmd_store_news)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
