name: Filter Correlation Diagnostic

# ============================================================
# MANUAL-ONLY workflow (Session 1 — Linear Algebra).
#
# Runs filter_correlation.py — an OFFLINE analysis script that recomputes
# the 7 filter drivers across the full S&P 500 universe and reports
# correlation + PCA redundancy. It does NOT touch the live pipeline, the
# shadow log, or any config gate — it only READS market data and prints
# a report to the Actions log.
#
# NO CRON. Fires only when you click "Run workflow" in the Actions tab.
# This is deliberate: it is a diagnostic you run occasionally, not a
# scheduled job.
#
# IMPORTANT — avoid running this in the same window as the 08:00 WIB
# (01:00 UTC) daily pipeline. Both fetch ~503 tickers from yfinance;
# firing them close together risks yfinance rate-limiting. Run this a few
# hours off the daily window, or on a weekend when the daily cron is idle.
#
# SECRETS: none are strictly required — this script makes zero LLM calls
# and does not read FRED for the correlation itself. FRED_API_KEY is wired
# in only for the optional macro context line in data_fetcher; if it's
# absent the script still runs and produces the full correlation report.
# ============================================================

on:
  workflow_dispatch:
    inputs:
      limit:
        description: "Number of tickers to process (blank = full universe ~503). Start with 50 for a safe first run."
        required: false
        default: "50"
        type: string

# Read-only: this workflow does not commit anything back to the repo.
permissions:
  contents: read

jobs:
  filter-correlation:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Checkout repo
        uses: actions/checkout@v5

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run filter correlation diagnostic
        env:
          # Not required for the correlation itself — wired in only for the
          # optional macro-context header line. Script runs fine without it.
          FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
        run: |
          LIMIT_INPUT="${{ github.event.inputs.limit }}"
          if [ -n "$LIMIT_INPUT" ]; then
            echo "Running with --limit $LIMIT_INPUT"
            python filter_correlation.py --limit "$LIMIT_INPUT"
          else
            echo "Running full universe (no limit)"
            python filter_correlation.py
          fi
