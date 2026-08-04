#!/usr/bin/env python3
"""
diagnose_fetch_failures.py — TEMPORARY diagnostic, not part of the pipeline.

Triggered by the 4 Aug 2026 verification run: 503 tickers screened, only 75
(14.9%) returned usable data. fetch_ohlcv() logs the real reason at DEBUG
level, which the daily pipeline never surfaces (it runs at INFO) — so the
production log shows nothing about WHY 428 tickers vanished.

This script does not modify data_fetcher.py or touch the failure-swallowing
path at all. It reimplements the same yfinance call with the error left
visible, so the production pipeline's behaviour and cost are completely
unaffected by this file existing.

METHOD — a paired before/after, not just a raw failure count:

  PASS 1 (RAPID)      Full universe (~503 tickers), zero delay between
                       requests — same conditions as the live screener.
                       This is what should reproduce today's ~15% number.

  PASS 2 (THROTTLED)  A deterministic subsample (every Nth ticker, same
                       tickers every run) from Pass 1, re-fetched with a
                       delay between requests.

Comparing Pass 2's success rate against Pass 1's rate ON THE SAME SUBSAMPLE
(not against the diluted full-universe number) answers the actual question:
does spacing out requests fix it? If yes, this is IP-level rate limiting and
the fix is a delay/backoff in fetch_ohlcv(). If the throttled pass fails at
close to the same rate, pacing is not the explanation and the printed
exception messages below are where to look instead (delisted tickers,
missing columns, a genuine data-source outage, etc).

For every failure this captures the ACTUAL exception type + message, or a
specific structural reason (empty dataframe / missing columns / too few
bars) — never just "None".

RUN THIS VIA GITHUB ACTIONS — yfinance needs real outbound network access,
which a sandboxed dev environment does not have. See the accompanying
diagnose_fetch.yml, workflow_dispatch, no inputs needed.

Sends nothing to Telegram, writes nothing to Sheets, calls no Anthropic
API. Pure read-only diagnostic. Delete this file and its workflow once the
question is answered — it is not meant to stay in the repo.
"""
import sys
import time
import logging
from collections import Counter
from typing import Optional, Tuple

import pandas as pd
import yfinance as yf

from src import universe as universe_mod

logging.basicConfig(level=logging.WARNING)  # silence yfinance's own chatter

THROTTLE_SAMPLE_STRIDE = 5     # every 5th ticker -> ~100 of 503
THROTTLE_DELAY_SECONDS = 0.5   # gap between requests in Pass 2


def fetch_with_reason(ticker: str) -> Tuple[bool, str]:
    """
    Same call shape as data_fetcher.fetch_ohlcv(), but returns WHY it
    failed instead of swallowing it. (True, "") on success.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="1y", interval="1d", auto_adjust=True)

        if df is None or df.empty:
            return False, "empty_dataframe"

        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(c.title() for c in df.columns)):
            return False, f"missing_columns:{list(df.columns)}"

        df = df.dropna(subset=[c for c in df.columns if c.lower() == "close"] or ["Close"])
        if len(df) < 10:
            return False, f"insufficient_bars:{len(df)}"

        return True, ""

    except Exception as e:
        # This is the line data_fetcher.py demotes to DEBUG. Keeping the
        # class name is what tells a rate limit (commonly a requests
        # HTTPError/429, or a JSONDecodeError when Yahoo returns an HTML
        # challenge page instead of data) apart from a genuinely bad ticker.
        return False, f"exception:{type(e).__name__}:{str(e)[:120]}"


def bucket_reason(reason: str) -> str:
    """Collapse a raw reason string into a stable category for counting."""
    if reason.startswith("exception:"):
        return reason.split(":", 2)[1]  # exception class name only
    return reason.split(":")[0]


def run_pass(tickers, delay: float, label: str):
    print(f"\n--- PASS: {label} ({len(tickers)} tickers, "
          f"{delay}s delay) ---")
    t0 = time.time()
    results = {}
    reasons = Counter()
    examples = {}

    for i, ticker in enumerate(tickers, 1):
        ok, reason = fetch_with_reason(ticker)
        results[ticker] = ok
        if not ok:
            cat = bucket_reason(reason)
            reasons[cat] += 1
            if cat not in examples:
                examples[cat] = (ticker, reason)
        if delay:
            time.sleep(delay)
        if i % 100 == 0:
            elapsed = time.time() - t0
            print(f"  {i}/{len(tickers)} done ({elapsed:.0f}s elapsed)")

    elapsed = time.time() - t0
    n_ok = sum(results.values())
    n_total = len(results)
    rate = (n_ok / n_total * 100) if n_total else 0.0

    print(f"  Done in {elapsed:.0f}s. Success: {n_ok}/{n_total} ({rate:.1f}%)")
    if reasons:
        print("  Failure reasons:")
        for cat, cnt in reasons.most_common():
            ticker, raw = examples[cat]
            print(f"    {cat:<25} {cnt:>4}x   e.g. {ticker}: {raw}")

    return results, rate


def main():
    print("=" * 70)
    print("FETCH FAILURE DIAGNOSTIC")
    print("=" * 70)
    print("Reproducing the 4 Aug verification run's 75/503 (14.9%) result, "
          "then testing whether spacing out requests changes it.")

    try:
        uni = universe_mod.get_sp500_universe()
    except Exception as e:
        print(f"ERROR: could not load universe at all: {e}")
        return 2

    all_tickers = list(uni["ticker"])
    print(f"\nUniverse loaded: {len(all_tickers)} tickers")

    # PASS 1 — rapid, full universe. Reproduces production conditions.
    pass1_results, pass1_rate = run_pass(all_tickers, delay=0.0, label="RAPID / full universe")

    # PASS 2 — throttled, deterministic subsample drawn from the SAME list.
    subsample = all_tickers[::THROTTLE_SAMPLE_STRIDE]
    pass2_results, pass2_rate = run_pass(
        subsample, delay=THROTTLE_DELAY_SECONDS,
        label=f"THROTTLED / every {THROTTLE_SAMPLE_STRIDE}th ticker",
    )

    # Paired comparison: how did THIS SAME subsample do in Pass 1?
    pass1_on_subsample_ok = sum(1 for t in subsample if pass1_results.get(t))
    pass1_on_subsample_rate = pass1_on_subsample_ok / len(subsample) * 100 if subsample else 0.0

    print("\n" + "=" * 70)
    print("PAIRED COMPARISON (same tickers, both passes)")
    print("=" * 70)
    print(f"  Pass 1 (rapid)     success rate on this subsample: "
          f"{pass1_on_subsample_rate:.1f}%")
    print(f"  Pass 2 (throttled) success rate on this subsample: "
          f"{pass2_rate:.1f}%")

    delta = pass2_rate - pass1_on_subsample_rate
    print(f"  Delta: {delta:+.1f} percentage points")

    print("\n" + "=" * 70)
    if delta >= 15:
        print("VERDICT: RATE LIMITING — spacing out requests recovered "
              f"{delta:.0f} points of success rate on identical tickers.")
        print(f"Fix: add a delay of roughly {THROTTLE_DELAY_SECONDS}s (or an "
              "exponential backoff on retry) inside data_fetcher.fetch_ohlcv() "
              "before it is used in the live screener loop.")
    elif delta <= 5:
        print("VERDICT: NOT explained by request pacing — throttling barely "
              "moved the needle. Look at the exception messages printed "
              "above instead (delisted tickers, a genuine source outage, "
              "or a structural issue unrelated to speed).")
    else:
        print(f"VERDICT: INCONCLUSIVE ({delta:+.1f} pts) — some effect from "
              "pacing, but not enough to call it the sole cause. Re-run at "
              "a different time of day before concluding anything; single-run "
              "network conditions are noisy.")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
