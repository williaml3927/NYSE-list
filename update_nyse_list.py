#!/usr/bin/env python3
"""
VI Pro — NYSE-list Enricher
============================
Fetches all US stock tickers from rreichel3/US-Stock-Symbols (NASDAQ + NYSE + AMEX),
enriches each with a company name from yfinance, and saves two JSON files
in the same format your app's assetRegistry.ts already expects:

  NYSE.json       — all tickers with { "Symbol": "AAPL", "Name": "Apple Inc." }
  Other list.json — copy of NYSE.json (kept for backward compatibility)

Upload both files to your williaml3927/NYSE-list GitHub repo to replace the
old ones. Your app will immediately start showing company names in search.

Usage:
  pip install requests yfinance
  python update_nyse_list.py

Output:
  NYSE.json        (upload to williaml3927/NYSE-list repo)
  Other list.json  (upload to williaml3927/NYSE-list repo)
  name_cache.json  (local cache — speeds up re-runs)

Notes:
  - yfinance lookups take ~0.3s each. With 7,000 tickers this takes ~30-40 minutes.
  - The script saves progress every 100 tickers so you can resume if interrupted.
  - Run it once, upload the two JSON files, done. No need to run again unless
    you want to refresh company names (quarterly is fine).
"""

import json
import os
import time
import requests
import yfinance as yf
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================

# rreichel3 ticker sources — updated nightly
NASDAQ_URL = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_tickers.json"
NYSE_URL   = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nyse/nyse_tickers.json"
AMEX_URL   = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/amex/amex_tickers.json"

# Output files — one per exchange, mirrors the rreichel3 source structure
OUTPUT_NASDAQ = "NASDAQ.json"
OUTPUT_NYSE   = "NYSE.json"
OUTPUT_AMEX   = "AMEX.json"
CACHE_FILE    = "name_cache.json"

# How many tickers to process before saving progress
SAVE_EVERY = 100

# Delay between yfinance calls (seconds) — keeps rate limits happy
YFINANCE_DELAY = 0.25

# Junk filter — matches calculate_stocks.py
def _is_junk(symbol: str) -> bool:
    n = len(symbol)
    if (symbol.endswith("WS") or symbol.endswith("WW")) and n >= 5:
        return True
    if symbol.endswith("W") and n >= 5:
        return True
    if symbol.endswith("U") and n >= 5:
        return True
    return False


# =============================================================================
# STEP 1 — Fetch ticker universe
# =============================================================================
def fetch_universe() -> dict[str, list[str]]:
    """Returns dict of {exchange: [ticker, ...]} keeping exchanges separate."""
    print("\n[1/3] Fetching ticker universe from rreichel3/US-Stock-Symbols...")
    sources = [("NASDAQ", NASDAQ_URL), ("NYSE", NYSE_URL), ("AMEX", AMEX_URL)]
    universe = {}
    all_seen = set()   # global dedup — a ticker only goes in one exchange list

    for exchange, url in sources:
        try:
            resp = requests.get(url, timeout=20)
            data = resp.json()
            tickers = []
            for raw in data:
                if not isinstance(raw, str):
                    continue
                symbol = raw.strip().upper().replace(".", "-").replace("/", "-")
                if symbol and symbol not in all_seen and not _is_junk(symbol):
                    all_seen.add(symbol)
                    tickers.append(symbol)
            universe[exchange] = sorted(tickers)
            print(f"  [{exchange}] {len(tickers):,} tickers")
        except Exception as e:
            print(f"  [{exchange}] ERROR: {e}")
            universe[exchange] = []

    total = sum(len(v) for v in universe.values())
    print(f"  Total: {total:,} tickers across all exchanges")
    return universe


# =============================================================================
# STEP 2 — Load existing name cache
# =============================================================================
def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            print(f"\n[2/3] Loaded name cache: {len(cache):,} entries")
            return cache
        except Exception:
            pass
    print("\n[2/3] No cache found — will fetch all names fresh")
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


# =============================================================================
# STEP 3 — Enrich with company names
# =============================================================================
def get_name(ticker: str, cache: dict) -> str:
    """
    Returns the company name for a ticker.
    Checks cache first, then yfinance, then falls back to the ticker itself.
    """
    if ticker in cache:
        return cache[ticker]

    try:
        time.sleep(YFINANCE_DELAY)
        info = yf.Ticker(ticker).info
        name = (
            info.get("longName") or
            info.get("shortName") or
            info.get("displayName") or
            ticker
        )
        # Clean up trailing legal suffixes for readability
        name = name.strip()
        cache[ticker] = name
        return name
    except Exception:
        cache[ticker] = ticker
        return ticker


def enrich_exchange(exchange: str, tickers: list[str], cache: dict) -> list[dict]:
    """Enriches one exchange's ticker list with company names."""
    results = []
    already_cached = sum(1 for t in tickers if t in cache)
    to_fetch = len(tickers) - already_cached
    print(f"  [{exchange}] {len(tickers):,} tickers  "
          f"({already_cached:,} cached, {to_fetch:,} to fetch)")

    start = time.time()
    fetched = 0

    for i, ticker in enumerate(tickers, start=1):
        was_cached = ticker in cache
        name = get_name(ticker, cache)
        results.append({"Symbol": ticker, "Name": name})

        if not was_cached:
            fetched += 1

        if i % 10 == 0 or i == len(tickers):
            elapsed = time.time() - start
            rate = fetched / elapsed if elapsed > 0 else 1
            remaining = max(0, (to_fetch - fetched) / rate)
            print(f"    {i:>5}/{len(tickers)}  {ticker:<12}  "
                  f"~{remaining/60:.0f}m left", end="\r")

        if i % SAVE_EVERY == 0:
            save_cache(cache)

    save_cache(cache)
    print(f"    {len(results):,} done{' ' * 30}")
    return results


def enrich_tickers(universe: dict[str, list[str]], cache: dict) -> dict[str, list[dict]]:
    total = sum(len(v) for v in universe.values())
    print(f"\n[3/3] Fetching company names for {total:,} tickers...")
    print("      Progress saved every 100 tickers. Re-run anytime to resume.\n")
    return {
        exchange: enrich_exchange(exchange, tickers, cache)
        for exchange, tickers in universe.items()
    }


# =============================================================================
# STEP 4 — Write output files
# =============================================================================
def write_outputs(enriched: dict[str, list[dict]]):
    print(f"\n[4/4] Writing output files...")
    output_map = {"NASDAQ": OUTPUT_NASDAQ, "NYSE": OUTPUT_NYSE, "AMEX": OUTPUT_AMEX}

    for exchange, results in enriched.items():
        path = output_map.get(exchange, f"{exchange}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"  Saved: {path} ({len(results):,} tickers)")

    # Preview a few from each
    print()
    for exchange, results in enriched.items():
        print(f"  {exchange} preview:")
        for entry in results[:3]:
            print(f"    {entry['Symbol']:<12} → {entry['Name']}")
        print()


# =============================================================================
# MAIN
# =============================================================================
def main():
    print("=" * 60)
    print("VI Pro — NYSE-list Enricher")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    universe = fetch_universe()
    cache    = load_cache()
    enriched = enrich_tickers(universe, cache)
    write_outputs(enriched)

    print("\n" + "=" * 60)
    print("DONE — next steps:")
    print("  1. Go to github.com/williaml3927/NYSE-list")
    print("  2. Upload NASDAQ.json  (new file)")
    print("  3. Upload NYSE.json    (replace existing)")
    print("  4. Upload AMEX.json    (new file)")
    print("  5. Delete 'Other list.json' — no longer needed")
    print("  6. Update assetRegistry.ts to read all three files")
    print("=" * 60)


if __name__ == "__main__":
    main()
