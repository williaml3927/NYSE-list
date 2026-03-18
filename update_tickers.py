"""
UPDATE TICKER LISTS
====================
Fetches fresh NYSE and other exchange listings, merges with existing lists,
and removes confirmed delisted tickers.

Sources (all free, no API key):
  1. SEC EDGAR company_tickers.json — all SEC-registered companies
  2. SEC EDGAR company_tickers_exchange.json — exchange-filtered
  3. Existing lists — preserved as baseline (nothing removed without confirmation)

Delisting check:
  Uses yfinance to verify suspicious tickers — a ticker is only removed if
  yfinance confirms it is no longer valid (empty info, no price, unknown quoteType).
  Conservative: only removes tickers with strong delisting signals to avoid
  accidentally dropping legitimate tickers that yfinance temporarily can't fetch.

Output format matches existing NYSE.json and Other list.json:
  [ { "Symbol": "AAPL" }, { "Symbol": "MSFT" }, ... ]
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
NYSE_FILE   = "NYSE.json"
OTHER_FILE  = "Other list.json"

# Suffixes that are never valid investment tickers
EXCLUDED_SUFFIXES = ("W", "WS", "WW", "R", "U", "Z", "Q")

# Exchange codes in EDGAR that map to NYSE vs Other
NYSE_EXCHANGES  = {"NYSE", "NYSEArca", "NYSEAMERICAN"}
OTHER_EXCHANGES = {"NASDAQ", "OTC", "BATS", "CBOE"}

# Minimum market cap to consider for new additions ($10M — filters micro shells)
MIN_MARKET_CAP = 10_000_000

# How many suspicious tickers to verify with yfinance per run
# (keeps run time reasonable — full check would take hours)
MAX_DELIST_CHECKS = 200

# =============================================================================
# HELPERS
# =============================================================================
def _is_junk_symbol(sym):
    if not sym or len(sym) < 1:
        return True
    sym = sym.upper()
    # Single letter tickers (closed-end funds, preferred) — keep them
    # Multi-char suffixes indicating non-investable instruments
    for suffix in EXCLUDED_SUFFIXES:
        if sym.endswith(suffix) and len(sym) > len(suffix) + 1:
            return True
    # Contains non-alphanumeric except hyphen (for BRK-B style)
    if not all(c.isalpha() or c.isdigit() or c == '-' for c in sym):
        return True
    return False


def load_existing(filename):
    """Load existing ticker list, return set of symbols."""
    if not os.path.exists(filename):
        return set()
    try:
        with open(filename) as f:
            data = json.load(f)
        return {item.get("Symbol", "").upper().strip()
                for item in data if item.get("Symbol")}
    except Exception as e:
        print(f"  [WARN] Could not load {filename}: {e}")
        return set()


def save_list(filename, symbols):
    """Save sorted list of symbols in standard format."""
    data = [{"Symbol": s} for s in sorted(symbols)]
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {len(data)} tickers to {filename}")


# =============================================================================
# SOURCE 1 — SEC EDGAR (free, comprehensive, no key)
# =============================================================================
def fetch_edgar_tickers():
    """
    Fetch all company tickers from SEC EDGAR.
    Returns dict: { symbol: { exchange, cik, name } }
    """
    result = {}
    UA = "TickerUpdater contact@example.com"

    # Primary: exchange-filtered list
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers_exchange.json",
            headers={"User-Agent": UA}, timeout=20
        )
        if resp.status_code == 200:
            data  = resp.json()
            # Format: { "fields": [...], "data": [[cik, name, ticker, exchange], ...] }
            fields = data.get("fields", [])
            rows   = data.get("data", [])
            t_idx  = fields.index("ticker")   if "ticker"   in fields else 2
            e_idx  = fields.index("exchange") if "exchange" in fields else 3
            n_idx  = fields.index("name")     if "name"     in fields else 1
            for row in rows:
                try:
                    sym = str(row[t_idx]).upper().strip().replace(".", "-")
                    exc = str(row[e_idx]).strip()
                    name = str(row[n_idx]).strip()
                    if sym and not _is_junk_symbol(sym):
                        result[sym] = {"exchange": exc, "name": name}
                except (IndexError, TypeError):
                    pass
            print(f"  EDGAR exchange list: {len(result)} tickers")
    except Exception as e:
        print(f"  [WARN] EDGAR exchange list failed: {e}")

    # Fallback: basic tickers list
    if not result:
        try:
            resp2 = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": UA}, timeout=20
            )
            if resp2.status_code == 200:
                for entry in resp2.json().values():
                    sym = str(entry.get("ticker", "")).upper().strip().replace(".", "-")
                    if sym and not _is_junk_symbol(sym):
                        result[sym] = {"exchange": "Unknown", "name": entry.get("title", "")}
                print(f"  EDGAR basic list: {len(result)} tickers")
        except Exception as e:
            print(f"  [WARN] EDGAR basic list failed: {e}")

    return result


# =============================================================================
# SOURCE 2 — FMP free ticker list (backup)
# =============================================================================
def fetch_fmp_tickers():
    """
    Fetch available symbols from FMP free API.
    No key needed for the basic symbols endpoint.
    """
    result = {}
    try:
        for exchange in ["NYSE", "NASDAQ"]:
            url = f"https://financialmodelingprep.com/api/v3/stock-screener?exchange={exchange}&limit=10000&apikey=demo"
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                for item in resp.json():
                    sym = str(item.get("symbol", "")).upper().strip()
                    if sym and not _is_junk_symbol(sym):
                        result[sym] = {"exchange": exchange, "name": item.get("companyName", "")}
            time.sleep(0.5)
    except Exception:
        pass
    if result:
        print(f"  FMP: {len(result)} tickers")
    return result


# =============================================================================
# DELISTING CHECK
# =============================================================================
def check_delisted(symbols, max_checks=MAX_DELIST_CHECKS):
    """
    Check a sample of symbols for delisting using yfinance.
    Returns set of confirmed delisted symbols.

    A symbol is considered delisted if yfinance returns:
      - Empty info dict
      - quoteType in invalid set
      - No price data at all AND no market cap
    
    Conservative: only marks as delisted if ALL signals point to delisting.
    A single yfinance failure is NOT enough — could be a temporary API issue.
    """
    delisted = set()
    checked  = 0

    # Prioritise checking symbols that look suspicious:
    # very short trading history, low rank, or known acquisition targets
    sample = list(symbols)[:max_checks]

    print(f"  Checking {len(sample)} tickers for delisting...")
    for sym in sample:
        if checked >= max_checks:
            break
        try:
            time.sleep(0.3)
            stock = yf.Ticker(sym)
            info  = stock.info

            # Strong delisting signals
            if not info:
                # Empty info — but could be rate limit, check price history
                hist = stock.history(period="5d")
                if hist is None or hist.empty:
                    delisted.add(sym)
                    print(f"    [DELIST] {sym}: no info and no price history")
                continue

            qt = info.get("quoteType", "").upper()

            # These quoteTypes mean the instrument no longer exists as a stock
            if qt in {"NONE", ""}:
                price = info.get("currentPrice") or info.get("regularMarketPrice")
                mc    = info.get("marketCap")
                if not price and not mc:
                    delisted.add(sym)
                    print(f"    [DELIST] {sym}: quoteType={qt}, no price, no market cap")

            checked += 1

        except Exception:
            pass   # Any exception = skip, don't remove

    return delisted


# =============================================================================
# MAIN
# =============================================================================
def main():
    print(f"\n{'='*60}")
    print(f"  TICKER LIST UPDATE")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # ── Step 1: Load existing lists ───────────────────────────────────────────
    print("[1] Loading existing ticker lists...")
    existing_nyse  = load_existing(NYSE_FILE)
    existing_other = load_existing(OTHER_FILE)
    existing_all   = existing_nyse | existing_other
    print(f"  Existing NYSE:  {len(existing_nyse)}")
    print(f"  Existing Other: {len(existing_other)}")
    print(f"  Total existing: {len(existing_all)}")

    # ── Step 2: Fetch fresh sources ───────────────────────────────────────────
    print("\n[2] Fetching fresh ticker sources...")
    edgar   = fetch_edgar_tickers()
    fmp     = fetch_fmp_tickers()

    # Merge all sources — EDGAR is authoritative
    all_fresh = {}
    for sym, data in fmp.items():
        all_fresh[sym] = data
    for sym, data in edgar.items():   # EDGAR overwrites FMP
        all_fresh[sym] = data

    print(f"  Total from all sources: {len(all_fresh)}")

    # ── Step 3: Split into NYSE vs Other ──────────────────────────────────────
    print("\n[3] Splitting by exchange...")
    fresh_nyse  = set()
    fresh_other = set()
    for sym, data in all_fresh.items():
        exc = data.get("exchange", "")
        if exc in NYSE_EXCHANGES:
            fresh_nyse.add(sym)
        elif exc in OTHER_EXCHANGES or exc:
            fresh_other.add(sym)
        else:
            fresh_other.add(sym)   # Unknown exchange → Other

    print(f"  Fresh NYSE:  {len(fresh_nyse)}")
    print(f"  Fresh Other: {len(fresh_other)}")

    # ── Step 4: Check for delisted tickers ───────────────────────────────────
    print("\n[4] Checking for delisted tickers...")
    # Only check tickers that exist in current lists but NOT in any fresh source
    # — these are the most likely to have been delisted
    possibly_delisted = existing_all - set(all_fresh.keys())
    print(f"  {len(possibly_delisted)} tickers in existing lists not found in fresh sources")

    confirmed_delisted = set()
    if possibly_delisted:
        confirmed_delisted = check_delisted(possibly_delisted)
        print(f"  Confirmed delisted: {len(confirmed_delisted)}")
    else:
        print(f"  No candidates for delisting check")

    # ── Step 5: Build final lists ─────────────────────────────────────────────
    print("\n[5] Building final lists...")

    # NYSE: union of existing and fresh, minus confirmed delisted
    final_nyse = (existing_nyse | fresh_nyse) - confirmed_delisted

    # Other: union of existing and fresh, minus confirmed delisted
    # Also remove anything promoted to NYSE
    final_other = (existing_other | fresh_other) - confirmed_delisted - final_nyse

    # Remove junk from both
    final_nyse  = {s for s in final_nyse  if not _is_junk_symbol(s)}
    final_other = {s for s in final_other if not _is_junk_symbol(s)}

    new_nyse    = final_nyse  - existing_nyse
    new_other   = final_other - existing_other
    removed     = confirmed_delisted & existing_all

    print(f"  Final NYSE:  {len(final_nyse)}  (+{len(new_nyse)} new, -{len(removed & existing_nyse)} removed)")
    print(f"  Final Other: {len(final_other)}  (+{len(new_other)} new, -{len(removed & existing_other)} removed)")

    if new_nyse:
        print(f"\n  New NYSE tickers: {sorted(new_nyse)[:20]}"
              + (" ..." if len(new_nyse) > 20 else ""))
    if new_other:
        print(f"  New Other tickers: {sorted(new_other)[:20]}"
              + (" ..." if len(new_other) > 20 else ""))
    if removed:
        print(f"  Removed (delisted): {sorted(removed)}")

    # ── Step 6: Save ──────────────────────────────────────────────────────────
    print("\n[6] Saving updated lists...")
    save_list(NYSE_FILE,  final_nyse)
    save_list(OTHER_FILE, final_other)

    print(f"\n✅ Ticker lists updated successfully.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
