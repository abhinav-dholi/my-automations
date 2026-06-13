"""market-data-sync — prices (Finnhub) + macro (FRED) into the datastore."""
from __future__ import annotations

import time

import marketdata
import secrets_store
import store

BENCHMARKS = ["SPY", "QQQ", "AGG", "GLD", "VTI"]


def main() -> None:
    fk = secrets_store.get("FINNHUB_API_KEY")
    rk = secrets_store.get("FRED_API_KEY")
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    today = time.strftime("%Y-%m-%d")

    store.init_db()
    with store.connect() as conn:
        symbols = sorted(set(store.held_symbols(conn)) | set(BENCHMARKS))
        n_prices = 0
        for sym in symbols:
            try:
                px = marketdata.quote(fk, sym)
            except Exception as e:  # noqa: BLE001
                print(f"[market-data-sync] price {sym} error: {e}"); continue
            if px:
                store.upsert_market_price(conn, symbol=sym, date=today, close=px)
                n_prices += 1

        n_macro = 0
        for series_id, label in marketdata.MACRO_SERIES:
            got = marketdata.fred_latest(rk, series_id)
            if got:
                d, v = got
                store.upsert_macro(conn, series=series_id, date=d, value=v, label=label)
                n_macro += 1
        cpi = marketdata.fred_cpi_yoy(rk)
        if cpi:
            d, v = cpi
            store.upsert_macro(conn, series="CPI_YOY", date=d, value=v, label="CPI inflation YoY %")
            n_macro += 1

        store.record_sync(conn, source="market-data", started=started, status="ok",
                          n_accounts=n_prices, n_transactions=n_macro,
                          note=f"{n_prices} prices, {n_macro} macro")
    print(f"[market-data-sync] {n_prices} prices, {n_macro} macro series updated.")


if __name__ == "__main__":
    main()
