"""
src/sheets_logger.py  —  Phase 2.6
-----------------------------------
Google Sheets bridge for the shadow log AND (new in Phase 2.6) the
`pending_entries` near-miss state tab.

Setup (one-time):
  1. Google Cloud Console -> create a Service Account -> create JSON key (download).
  2. Enable the "Google Sheets API" for that project.
  3. Open your shadow-log sheet -> Share -> add the service-account email
     (xxxx@yyyy.iam.gserviceaccount.com) as EDITOR.
  4. Add GitHub repo secrets:
       GSHEET_SA_JSON  = full contents of the service-account JSON key
       GSHEET_ID       = the sheet id from the URL (.../d/<THIS_PART>/edit)
       GSHEET_TAB      = (optional) worksheet/tab name, default "shadow_log"
  5. requirements.txt: add  gspread>=6.0.0  and  google-auth>=2.30.0

  NOTE: the pending-entries tab name is a CONFIG CONSTANT
  (config.PENDING_ENTRIES_TAB), deliberately NOT a new secret. The three
  Sheets secrets were once created correctly but never wired into the
  workflow `env:` block, and the mirror silently no-op'd for 22 days.
  Fewer secrets = fewer places to forget.

HEADER ROBUSTNESS FIX (June 2026, round 1):
====================================
The original header-write check was `if not ws.get_all_values()`, which only
writes the header when the sheet is COMPLETELY empty. This silently failed
once: data was written to a tab whose row 1 was not actually empty (e.g. a
stray formatted cell), so the header was permanently skipped and every
auto-logged field appeared to be in the wrong place with no labels.

DUPLICATE-HEADER BUG FIX (June 21 2026, round 2):
====================================
The round-1 fix introduced a NEW bug: it compared row 1 against HEADER with
an exact match, and on mismatch it INSERTED a fresh canonical header row
above whatever was already there. In production, row 1 held a hand-curated
header (different naming convention — no leading underscore — plus an extra
"Remarks" column the user had added manually). Because that header could
never exact-match HEADER, every run after the round-1 fix shipped, the code
inserted a second header row on top of the user's own header — producing
the two-header-row stack seen in the sheet.

Round-2 fix: stop trying to validate/replace an existing header at all.
If row 1 has ANY content — canonical or hand-curated — leave it alone.
Only write HEADER when row 1 is genuinely empty (brand-new tab). This
trusts whatever header convention the user has actually set up in the
sheet, which is the thing this function kept breaking.

ROW-GAP BUG FIX (June 21 2026, round 2):
====================================
`ws.append_rows()` asks the Sheets API to auto-detect the end of the
existing "table" before appending below it. Data-validation dropdowns
applied to a large pre-formatted range (e.g. `_shadow_outcome` validation
dragged down to row 1000) can throw this detection off, causing the next
day's rows to land further down than the actual last data row — producing
a visible blank-row gap between one day's entries and the next.

Fixed by replacing append_rows() with an explicit-position write: read the
actual row count via get_all_values(), then update() starting at
row_count + 1. This is deterministic — there is no "detect the table"
step left for a dropdown or stray cell to confuse, so a gap can't form.

PHASE 2.6 — READ PATH + PENDING ENTRIES (August 2026):
====================================
This module was WRITE-ONLY and fully non-blocking: every failure was
swallowed so signal delivery could never be stopped by a Sheets outage.
Phase 2.6 adds a READ path, which changes that contract in one specific
place and must be handled with care.

WHY A READ PATH EXISTS NOW:
  Risk per trade is 2% of CURRENT equity (not a fixed dollar amount), and
  current equity is derived from realised P&L:

      equity = ACCOUNT_START_USD + sum(r_realized * risk_usd)   [CLOSED rows]

  Unfilled signals and still-open FLOAT positions have no outcome, so they
  are skipped automatically — which is exactly the desired behaviour: a
  signal that never filled must not move the equity base.

  This makes position sizing DEPEND on Sheets. To keep the "never block
  signal delivery" contract intact, main.py caches the last successful
  equity read to config.EQUITY_CACHE_PATH and falls back to it on failure,
  sending a Telegram warning when it does. Falling back silently to
  ACCOUNT_START_USD is explicitly forbidden — that would size every trade
  off a stale $100 base with no visible symptom.

HEADER NORMALISATION — THE IMPORTANT PART:
  The LIVE sheet header is hand-curated: no leading underscores, different
  capitalisation, spaces instead of underscores, and an extra "Remarks"
  column. Reading by COLUMN INDEX against HEADER would therefore silently
  read the wrong column. Every read in this module resolves columns by
  NORMALISED HEADER NAME instead:

      "_shadow_r_realized"  ->  "r_realized"
      "R Realized"          ->  "r_realized"
      "  r-realized "       ->  "r_realized"

  Same normalisation approach already proven in entry_diagnostic.py's
  _normalize_fieldnames(). If a required column cannot be resolved, the
  read FAILS LOUDLY (returns None) rather than guessing — a wrong equity
  number is far worse than no equity number, because it silently mis-sizes
  every trade in the run.

NEW COLUMNS IN THIS RELEASE (appended to HEADER, never inserted):
  _shadow_risk_usd          - dollar risk at entry. REQUIRED for the equity
                              calculation: r_realized alone is scale-free
                              and cannot be converted back to dollars once
                              risk floats with equity.
  _shadow_entry_type        - "strict" (EMA9 limit filled) or "fallback"
                              (two-stage near-miss, market on next open).
                              The out-of-sample test of ENTRY2_ATR_TOLERANCE.
  _shadow_force_closed      - TRUE when the position was closed by an
                              administrative batch cutoff rather than by its
                              own stop/target/time rule.
  _shadow_discretionary_action - TAKEN / VETOED / MODIFIED. Without this,
                              a manually-filtered batch cannot be read at
                              all: there is no way to separate "the system
                              is good" from "the operator rescued it".
  _shadow_veto_reason       - free text, e.g. "double top".
  _shadow_spy_vs_sma20      - regime context, LOG ONLY. NAMED sma20, not
                              ema20: fetch_market_context() returns a SIMPLE
                              moving average. Labelling it EMA would mislead
                              whoever analyses this column later.
  _shadow_vix_level         - regime context, LOG ONLY.
  _shadow_pct_above_50dma   - regime context, LOG ONLY.

  The three regime fields are recorded for later analysis and MUST NOT gate
  anything. A regime hard-gate was tested on Batch 3 and rejected: it
  improved backtested R but removed the 6 largest winners (+8.4R combined)
  and cut breadth 32 -> 13, i.e. ~36% of statistical power. Log first,
  decide later, on out-of-sample data.

  APPEND-ONLY IS NOT COSMETIC: append_shadow_rows() writes payload into
  columns A..N in HEADER order. Inserting a column in the middle of HEADER
  would shift every subsequent column in the live sheet and silently
  corrupt both the existing Batch 3 rows and the R Calculator formulas.
  Always append. See SHEET MIGRATION note below.

SHEET MIGRATION REQUIRED BEFORE FIRST BATCH 4 RUN:
  The live sheet currently ends with a hand-added "Remarks" column
  immediately after the last code-written column. The 8 new columns above
  must be inserted BEFORE "Remarks" so that the code-written block stays
  contiguous from column A. Manual/formula columns (fill_date, exit_date,
  mfe_price, mfe_r, days_to_mfe, "Available in Pluang?", Remarks) all live
  to the RIGHT of the code-written block and are never touched by writes.
"""
import os
import json
import re

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _LIBS_OK = True
except Exception:                       # libs not installed -> degrade silently
    _LIBS_OK = False

from . import config

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Must match the exact keys written by _build_shadow_rows() in main.py
# (same dicts are passed to append_shadow_rows() — no renaming/adapter layer,
# so this list IS the single source of truth for shadow_log.csv column order
# AND the Sheet column order, for any tab that is genuinely empty when first
# written. Existing hand-curated headers are left untouched — see
# _ensure_header() docstring below).
#
# APPEND NEW FIELDS AT THE END ONLY. See module docstring.
HEADER = [
    "_shadow_date",
    "_shadow_ticker",
    "_shadow_close_price",
    "_shadow_ema9_price",
    "_shadow_ema9_gap_pct",
    "_shadow_ema9_ext_atr",
    "_shadow_atr",
    "_shadow_atr_pct",
    "_shadow_rs_gap_vs_spy",
    "_shadow_spy_10d_return",
    "_shadow_stock_10d_return",
    "_shadow_rs_60d_gap",
    "_shadow_rs_126d_gap",
    "_shadow_conviction",
    "_shadow_confluence_count",
    "_shadow_haiku_score",
    "_shadow_adx",
    "_shadow_rsi",
    "_shadow_volume_ratio",
    "_shadow_entry_close",
    "_shadow_entry_ema9",
    "_shadow_stop",
    "_shadow_tp1",
    "_shadow_tp2",
    "_shadow_rr",
    "_shadow_ema9_filled",
    "_shadow_outcome",
    "_shadow_r_realized",
    "_shadow_hold_days",
    # ---- Phase 2.6 additions (APPENDED, never inserted) ----
    "_shadow_risk_usd",
    "_shadow_entry_type",
    "_shadow_force_closed",
    "_shadow_discretionary_action",
    "_shadow_veto_reason",
    "_shadow_spy_vs_sma20",
    "_shadow_vix_level",
    "_shadow_pct_above_50dma",
]

# Header for the near-miss state tab. Small and self-contained on purpose:
# keeping it in its own tab makes it immune to any future column change in
# shadow_log, and lets a failure here be swallowed without touching the
# main signal path.
PENDING_HEADER = [
    "signal_date",      # date the original signal was generated
    "ticker",
    "ema9_price",       # the Stage-1 limit price that did not fill
    "atr",
    "zone_high",        # ema9_price + ENTRY2_ATR_TOLERANCE * atr
    "day_low",          # session low that entered the zone without filling
    "status",           # PENDING -> resolved next run to one of:
                        #   FILLED   - session low reached EMA9, limit worked
                        #   FALLBACK - low entered the tolerance zone but did
                        #              not fill -> market on next open
                        #   EXPIRED  - low never entered the zone, or the
                        #              1-trading-day window elapsed
    "execute_date",     # date the run acted on it
    "actual_entry",     # MANUAL — real fill price achieved at next open
    "note",
    # PHASE 2.6.1 — date of the last COMPLETED session the signal was
    # computed from. Appended last, so existing 10-column pending tabs keep
    # working: rows written before this column existed simply leave it blank
    # and the resolver falls back to the old signal_date behaviour.
    #
    # Why this exists: `signal_date` is the wall-clock UTC date of the run,
    # which is NOT reliably the session the limit order sits in.
    #   01:00 UTC run (production cron): last bar = D-1, order lives in D,
    #                                    signal_date = D          -> order in signal_date's session
    #   23:12 UTC run (off-schedule):    last bar = D,   order lives in D+1,
    #                                    signal_date = D          -> order in the session AFTER
    # Same column, two different meanings, depending only on what time the
    # workflow happened to fire. Anchoring on the bar date removes the
    # ambiguity entirely: the order is always live in the first session
    # STRICTLY AFTER bar_date, at any run time.
    "bar_date",
]


def _normalize(name: str) -> str:
    """
    Canonicalise a header cell so hand-curated and code-written headers
    resolve to the same key.

        "_shadow_r_realized" -> "r_realized"
        "R Realized"         -> "r_realized"
        "  r-realized "      -> "r_realized"
        "Available in Pluang?" -> "available_in_pluang"

    Deliberately aggressive: the live sheet header is maintained by hand and
    WILL drift in capitalisation/spacing. Resolving by index instead would
    silently read the wrong column, which is the failure mode this whole
    function exists to prevent.
    """
    s = str(name or "").strip().lower()
    s = s.replace("-", " ").replace("/", " ")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if s.startswith("shadow_"):
        s = s[len("shadow_"):]
    return s


def _worksheet(tab: str = None, header: list = None):
    """
    Returns the target worksheet, or None if not configured / unavailable.

    Phase 2.6: parameterised. Previously hard-coded to the GSHEET_TAB env
    var, which made the pending-entries tab impossible to reach.

    tab=None  -> shadow log tab (GSHEET_TAB env, default "shadow_log")
    header    -> used ONLY to size a newly created tab; never written here.
    """
    if not _LIBS_OK:
        return None
    raw = os.environ.get("GSHEET_SA_JSON", "")
    sheet_id = os.environ.get("GSHEET_ID", "")
    if not raw or not sheet_id:
        return None
    if tab is None:
        tab = os.environ.get("GSHEET_TAB", "shadow_log")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab, rows=1000, cols=len(header or HEADER))


def _ensure_header(ws, header: list = None) -> None:
    """
    Write `header` only if row 1 is genuinely empty (brand-new tab).

    Round-2 fix: this function used to compare row 1 against HEADER and
    INSERT a fresh header on any mismatch — which duplicated the user's
    own hand-curated header (different naming convention + extra columns)
    every run. It no longer validates or replaces an existing header at
    all. If row 1 already has anything in it, this is a no-op — whatever
    header the user has in place is trusted as-is.
    """
    header = header or HEADER
    try:
        current_row1 = ws.row_values(1)
    except Exception:
        current_row1 = []

    if current_row1:
        # Row 1 already has content (canonical or hand-curated) — leave it.
        return

    ws.append_row(header, value_input_option="USER_ENTERED")


def append_shadow_rows(rows):
    """
    rows: list[dict] keyed by HEADER (the SAME dicts written to shadow_log.csv
    by _build_shadow_rows() in main.py — pass them through unmodified).
    Returns True on success, False on any failure (never raises).

    Round-2 fix: writes at an explicit position (get_all_values() row count + 1)
    instead of append_rows(), which avoids the Sheets API's table-end
    auto-detection — the mechanism behind the observed blank-row gap between
    days. See module docstring "ROW-GAP BUG FIX" for detail.
    """
    try:
        ws = _worksheet()
        if ws is None:
            print("[sheets_logger] not configured (missing libs/secrets) — skipped.")
            return False

        _ensure_header(ws)

        payload = [[r.get(c, "") for c in HEADER] for r in rows]
        if not payload:
            print("[sheets_logger] no rows to write — skipped.")
            return True

        existing = ws.get_all_values()
        next_row = len(existing) + 1
        end_row = next_row + len(payload) - 1
        end_col_a1 = gspread.utils.rowcol_to_a1(1, len(HEADER))
        end_col_letters = "".join(ch for ch in end_col_a1 if ch.isalpha())
        target_range = f"A{next_row}:{end_col_letters}{end_row}"

        ws.update(target_range, payload, value_input_option="USER_ENTERED")
        print(f"[sheets_logger] wrote {len(payload)} row(s) to Google Sheet starting at row {next_row}.")
        return True
    except Exception as e:                               # non-blocking by design
        print(f"[sheets_logger] write failed (non-blocking): {e}")
        return False


# ============================================================
# PHASE 2.6 — READ PATH
# ============================================================

def _to_float(v):
    """
    Tolerant numeric parse for hand-entered cells.

    Handles the shapes a human actually types into a sheet: "1,25",
    "+0.88", "$3.00", "(0.39)" for negative, "2.5R". Returns None when the
    cell is blank or genuinely non-numeric — callers treat None as
    "not closed yet" and skip the row, so a stray note in a numeric column
    degrades to "ignored", never to a wrong number.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None

    # PHASE 2.6.3 — Unicode dash normalisation. A cell copied out of a
    # formatted Sheets range, a PDF, or a chat message can carry U+2212 MINUS
    # SIGN, U+2013 EN DASH or U+2011 NON-BREAKING HYPHEN instead of ASCII "-".
    # float() rejects all three, so "−1.00" returned None and the row was
    # silently dropped from equity — a real loss recorded as no loss at all.
    for _dash in ("\u2212", "\u2013", "\u2014", "\u2011", "\u2010"):
        s = s.replace(_dash, "-")

    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace("$", "").replace("R", "").replace("r", "").replace("+", "").strip()

    # COMMA HANDLING — sheet locale is US: "." is the decimal separator and
    # "," is the thousands separator ("10.15", "1,122.15").
    #
    # Naively stripping every comma turns a mistyped "1,25" into 125.0 — a
    # silent 100x error that would corrupt equity on the first hand-entered
    # r_realized. But guessing it means 1.25 is also a guess. Since "1,25"
    # cannot be valid in US format, it is treated as MALFORMED and returns
    # None: the row is skipped and shows up in read_equity()'s
    # skipped_no_risk counter, so a typo surfaces as a visible gap rather
    # than as a plausible-looking wrong number.
    #
    #   "1,122.15" -> 1122.15   (comma + period -> thousands separator)
    #   "1,122"    -> 1122.0    (3 digits after comma -> thousands separator)
    #   "1,25"     -> None      (<=2 digits, no period -> malformed, rejected)
    if "," in s:
        if "." in s:
            s = s.replace(",", "")
        else:
            tail = s.rsplit(",", 1)[-1]
            if tail.isdigit() and len(tail) == 3:
                s = s.replace(",", "")
            else:
                return None

    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def read_shadow_rows():
    """
    Read the shadow log as a list of dicts keyed by NORMALISED header name.

    Returns None on any failure (not configured, API error, empty tab).
    Callers must treat None as "unknown" and use their fallback — never as
    "no trades yet", which would silently reset equity to the starting
    balance.
    """
    try:
        ws = _worksheet()
        if ws is None:
            print("[sheets_logger] read skipped — not configured.")
            return None
        values = ws.get_all_values()
        if not values or len(values) < 2:
            print("[sheets_logger] sheet has no data rows.")
            return []
        head = [_normalize(h) for h in values[0]]
        out = []
        for raw_row in values[1:]:
            # zip() stops at the shorter of the two, which is what we want:
            # trailing empty cells are simply absent from the dict.
            row = {}
            for key, cell in zip(head, raw_row):
                # PHASE 2.6.3 (B14) — FIRST-WINS on duplicate normalised names.
                # _normalize() is aggressive by design, so a hand-added column
                # can collide with a code-written one ("_shadow_date" and a
                # manual "Date" both -> "date"). Last-wins would silently make
                # the manual column authoritative for reads. The code-written
                # block is always leftmost (A..AK), so first-wins keeps the
                # authoritative column authoritative.
                if key and key not in row:
                    row[key] = cell
            if any(str(v).strip() for v in row.values()):
                out.append(row)
        return out
    except Exception as e:
        print(f"[sheets_logger] read failed: {e}")
        return None


def read_equity():
    """
    Compute current equity from CLOSED shadow-log rows.

        equity = ACCOUNT_START_USD + sum(r_realized * risk_usd)

    A row contributes ONLY when it has both a numeric r_realized AND a
    numeric risk_usd. That single condition handles every exclusion the
    design requires, without special-casing:
      - unfilled EMA9 limit orders  -> no outcome, no r_realized -> skipped
      - still-open FLOAT positions  -> no r_realized yet          -> skipped
      - Batch 3 legacy rows          -> no risk_usd column        -> skipped

    Returns (equity: float, meta: dict) on success, or (None, meta) on
    failure. meta carries diagnostics for the Telegram warning:
        closed_rows   - number of rows that contributed
        skipped_no_risk - rows that had r_realized but NO risk_usd
        stale_rows    - list of (ticker, hold_days) with a blank outcome
                        but hold_days already past EQUITY_STALE_WARN_DAYS

    IMPORTANT (Batch 3 -> Batch 4 boundary): legacy rows have no risk_usd,
    so they contribute nothing and equity starts from ACCOUNT_START_USD.
    That is intentional — Batch 3 was paper, its P&L is not real capital.

    PHASE 2.6.1 — EXECUTION-TRACK FILTER (bug fix):
    ------------------------------------------------
    Batch 4 runs two tracks on ONE sheet (Phase 2.6 §2.6): every signal is
    logged for statistics at full n, but only a subset is actually bought
    (~7/month at 2% risk on $100). This function previously summed EVERY
    closed row, so the paper P&L of a VETOED trade moved the REAL account
    balance — and the next run sized 2% off a number that existed in no
    brokerage account.

    That is the same class of defect as the RULE 4 / RULE 5 conflict fixed
    in Phase 2.6: two design decisions (#3 "equity = realised only" and #13
    "shadow log records everything") that cannot both be true in one code
    path. Resolved the same way — by making the code say which one wins.

    A row now contributes ONLY if discretionary_action is in
    config.EQUITY_COUNTED_ACTIONS. Blank actions are excluded and WARNED
    about (config.EQUITY_COUNT_UNMARKED), because a blank is genuinely
    ambiguous and an equity number that quietly invents capital is worse
    than one that visibly lags.
    """
    meta = {
        "closed_rows": 0,
        "skipped_no_risk": 0,
        "stale_rows": [],
        "vetoed_rows": 0,
        "unmarked_rows": [],
        "source": "sheets",
    }
    rows = read_shadow_rows()
    if rows is None:
        return None, meta
    if not rows:
        return float(config.ACCOUNT_START_USD), meta

    present_cols = {k for r in rows for k in r.keys()}

    if "risk_usd" not in present_cols:
        # The migration has not been applied. Refuse to guess.
        print("[sheets_logger] ERROR: no 'risk_usd' column found — sheet migration not applied.")
        meta["error"] = "missing_risk_usd_column"
        return None, meta

    if "discretionary_action" not in present_cols:
        # Same migration, same refusal. Without this column every paper row
        # is indistinguishable from an executed one, and equity cannot be
        # computed correctly at all — only plausibly. Fail loudly instead:
        # main.py falls back to the cached value and warns the operator.
        print("[sheets_logger] ERROR: no 'discretionary_action' column found — "
              "sheet migration not applied. Equity cannot separate executed "
              "trades from paper ones.")
        meta["error"] = "missing_discretionary_action_column"
        return None, meta

    equity = float(config.ACCOUNT_START_USD)
    stale_after = int(getattr(config, "EQUITY_STALE_WARN_DAYS", 10))
    counted = {a.upper() for a in getattr(config, "EQUITY_COUNTED_ACTIONS", ("TAKEN", "MODIFIED"))}
    excluded = {a.upper() for a in getattr(config, "EQUITY_EXCLUDED_ACTIONS", ("VETOED",))}
    count_unmarked = bool(getattr(config, "EQUITY_COUNT_UNMARKED", False))

    for r in rows:
        r_real = _to_float(r.get("r_realized"))
        risk = _to_float(r.get("risk_usd"))
        outcome = str(r.get("outcome", "")).strip()
        hold = _to_float(r.get("hold_days"))
        ticker = r.get("ticker", "?")

        # Normalise the action the same way header names are normalised, so
        # "Vetoed", " vetoed ", "not-taken" all resolve. Anything not
        # recognised is treated as unmarked, never as executed.
        action = re.sub(r"[^A-Z0-9]+", "_", str(r.get("discretionary_action", "")).upper()).strip("_")

        if r_real is not None and risk is not None:
            if action in counted:
                equity += r_real * risk
                meta["closed_rows"] += 1
            elif action in excluded:
                # Paper row on the statistical track. Expected — no warning.
                meta["vetoed_rows"] += 1
            elif not action:
                if count_unmarked:
                    equity += r_real * risk
                    meta["closed_rows"] += 1
                else:
                    meta["unmarked_rows"].append(ticker)
            else:
                # Recognised as neither. Do NOT guess — a typo'd action must
                # not be allowed to move real capital.
                meta["unmarked_rows"].append(f"{ticker}({action})")
            continue

        if r_real is not None and risk is None:
            # Realised but un-sizeable: its P&L is invisible to equity.
            meta["skipped_no_risk"] += 1
            continue

        # Not closed. Flag if it has been open suspiciously long — a stale
        # row means realised P&L is missing and sizing runs off an old base.
        if not outcome and hold is not None and hold > stale_after:
            meta["stale_rows"].append((ticker, hold))

    return round(equity, 4), meta


# ============================================================
# PHASE 2.6 — PENDING ENTRIES (two-stage entry, cross-day state)
# ============================================================

def read_pending(statuses=("PENDING",)):
    """
    Return near-miss rows matching `statuses` as list of dicts, each
    including "_row" = the 1-based sheet row number (needed to write back).

    Returns None on failure — caller must NOT treat that as "nothing
    pending", or a committed near-miss would be silently dropped.

    PHASE 2.6.1: `statuses` was previously hard-coded to PENDING. Stage 0c
    needs to re-read FALLBACK rows to pick up the hand-entered actual_entry
    price, which is the only way the recomputed stop/TP ever reaches the
    shadow log.
    """
    wanted = {str(s).strip().upper() for s in statuses}
    try:
        ws = _worksheet(tab=config.PENDING_ENTRIES_TAB, header=PENDING_HEADER)
        if ws is None:
            return None
        _ensure_header(ws, PENDING_HEADER)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return []
        head = [_normalize(h) for h in values[0]]

        # ------------------------------------------------------------------
        # PHASE 2.6.3 — SHORT-HEADER SELF-HEAL (bug B2).
        #
        # _ensure_header() is a no-op whenever row 1 has any content, so a tab
        # created with the ORIGINAL 10-column PENDING_HEADER keeps that header
        # forever. append_pending() meanwhile writes 11 values (A..K) in
        # PENDING_HEADER order, so `bar_date` lands in column K with no header
        # above it. zip(head, raw_row) then stops at the shorter of the two and
        # DROPS it — the value is written every run and read back never.
        #
        # The symptom is invisible: the resolver silently falls back to the
        # legacy ">= signal_date" anchor, which is the exact defect bar_date
        # was added to fix. Worse, the documented watch instruction ("check
        # bar_date is populated") passes, because the CELL is populated — it is
        # the HEADER that is missing.
        #
        # Writes are positional and PENDING_HEADER is strictly append-only, so
        # position i is PENDING_HEADER[i] by construction. Backfilling the
        # missing tail names is therefore safe and deterministic. We do NOT
        # rewrite row 1 (that is what caused the round-1 duplicate-header bug);
        # we only fix our own in-memory view, and warn loudly.
        # ------------------------------------------------------------------
        canonical = [_normalize(h) for h in PENDING_HEADER]
        if len(head) < len(canonical) and head == canonical[:len(head)]:
            missing = canonical[len(head):]
            print(f"[sheets_logger] WARNING: pending tab header has "
                  f"{len(head)} columns, code writes {len(canonical)}. "
                  f"Backfilling {missing} positionally. ADD THESE HEADER "
                  f"CELLS BY HAND — the self-heal is a safety net, not a fix.")
            head = head + missing
        elif len(head) < len(canonical):
            print(f"[sheets_logger] WARNING: pending tab header ({head}) does "
                  f"not prefix-match PENDING_HEADER ({canonical}). NOT "
                  f"backfilling — positional assumptions are unsafe here.")

        out = []
        for idx, raw_row in enumerate(values[1:], start=2):
            row = {}
            for k, c in zip(head, raw_row):
                # first-wins: the code-written block is leftmost, so a
                # hand-added column that normalises to the same name must not
                # shadow it.
                if k and k not in row:
                    row[k] = c
            if str(row.get("status", "")).strip().upper() in wanted:
                row["_row"] = idx
                out.append(row)
        return out
    except Exception as e:
        print(f"[sheets_logger] pending read failed: {e}")
        return None


def _canon_date(v):
    """
    PHASE 2.6.3 (bug B13) — canonicalise a date cell to ISO YYYY-MM-DD.

    update_shadow_rows() matches rows on (ticker, date). The date side comes
    from ws.get_all_values(), which returns the cell's DISPLAY STRING, not its
    underlying value. append_shadow_rows() writes with USER_ENTERED, so
    "2026-08-11" is parsed by Sheets into a real date and re-rendered using
    whatever format the column carries. Today that round-trips as ISO; the
    moment anyone applies a date format to the column it becomes "8/11/2026"
    and EVERY write-back silently misses — entry_type stays "strict" and
    fallback geometry is never corrected, which is precisely the state Bug 3
    was fixed to escape.

    Comparing canonical forms instead of raw strings removes the dependency on
    the sheet's display format entirely.

    Ambiguity note: M/D/YYYY is assumed over D/M/YYYY, consistent with the
    sheet's confirmed US number locale (period as decimal separator) that
    _to_float() already relies on. Anything unparseable is returned stripped
    and lowercased, so an exact string match still works as a last resort.
    """
    s = str(v or "").strip()
    if not s:
        return ""
    # already ISO (possibly with a time suffix)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # M/D/YYYY or M-D-YYYY (US locale)
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        mo, d, y = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return s.lower()


def update_shadow_rows(updates):
    """
    PHASE 2.6.1 — write specific fields back into existing shadow_log rows.

    This module was WRITE-ONLY at the row level: append_shadow_rows() could
    create a row, but nothing could ever amend one. That is why
    `_shadow_entry_type` was frozen at its write-time guess of "strict"
    forever, and why a Stage-2 fallback trade kept EMA9-derived stop/TP in
    the log while being executed at a different price. The column said
    "strict", the geometry said EMA9, and the trade was neither.

    updates: list of dicts
        {"ticker": "AAPL", "signal_date": "2026-08-11",
         "fields": {"entry_type": "fallback", "stop": 123.45, ...}}

    Field keys are NORMALISED names (i.e. "entry_type", not
    "_shadow_entry_type") so they match whatever naming convention the live
    header actually uses — the sheet mixes three of them (Phase 2.6 §7.3).

    Matches on (ticker, date). If a pair is ambiguous or absent the row is
    reported in the return value and NOT written — a wrong-row write would
    corrupt a real trade's geometry, which is strictly worse than a missing
    annotation.

    Returns {"updated": int, "missed": [str], "error": str|None}.
    Never raises.
    """
    out = {"updated": 0, "missed": [], "error": None}
    if not updates:
        return out
    try:
        ws = _worksheet()
        if ws is None:
            out["error"] = "not configured"
            return out
        values = ws.get_all_values()
        if not values or len(values) < 2:
            out["error"] = "shadow log empty"
            return out

        head = [_normalize(h) for h in values[0]]
        # PHASE 2.6.3 (B14) — FIRST-WINS. A dict comprehension is last-wins, so
        # a hand-added column normalising to an existing name (e.g. a manual
        # "Stop" next to the code-written "_shadow_stop") would silently
        # redirect writes into the wrong column. The code-written block is
        # leftmost, so first occurrence is the authoritative one.
        col_of = {}
        dupes = []
        for i, name in enumerate(head):
            if not name:
                continue
            if name in col_of:
                dupes.append(name)
                continue
            col_of[name] = i
        if dupes:
            print(f"[sheets_logger] WARNING: duplicate normalised header "
                  f"name(s) {sorted(set(dupes))} — writing to the LEFTMOST "
                  f"occurrence of each. Rename the manual column to remove "
                  f"the ambiguity.")

        if "ticker" not in col_of or "date" not in col_of:
            out["error"] = "shadow log missing ticker/date column"
            return out

        t_idx, d_idx = col_of["ticker"], col_of["date"]

        # (ticker, date) -> [row numbers]. Built once for all updates.
        index = {}
        for row_no, raw in enumerate(values[1:], start=2):
            if len(raw) <= max(t_idx, d_idx):
                continue
            key = (str(raw[t_idx]).strip().upper(), _canon_date(raw[d_idx]))
            index.setdefault(key, []).append(row_no)

        cells = []
        for upd in updates:
            ticker = str(upd.get("ticker", "")).strip().upper()
            sig_date = _canon_date(upd.get("signal_date", ""))
            fields = upd.get("fields") or {}
            matches = index.get((ticker, sig_date), [])

            if len(matches) != 1:
                out["missed"].append(
                    f"{ticker}@{sig_date}"
                    f"{' (no row)' if not matches else f' ({len(matches)} rows)'}"
                )
                continue

            row_no = matches[0]
            for key, val in fields.items():
                key_n = _normalize(key)
                if key_n not in col_of:
                    out["missed"].append(f"{ticker}@{sig_date} (no column '{key_n}')")
                    continue
                cells.append(gspread.Cell(row_no, col_of[key_n] + 1, val))
            out["updated"] += 1

        if cells:
            ws.update_cells(cells, value_input_option="USER_ENTERED")
            print(f"[sheets_logger] amended {out['updated']} shadow row(s), "
                  f"{len(cells)} cell(s).")
        return out
    except Exception as e:
        out["error"] = str(e)[:200]
        print(f"[sheets_logger] shadow row update failed (non-blocking): {e}")
        return out


def append_pending(rows):
    """
    Append near-miss rows to the pending tab. rows: list[dict] keyed by
    PENDING_HEADER. Non-blocking — returns False on failure.
    """
    try:
        ws = _worksheet(tab=config.PENDING_ENTRIES_TAB, header=PENDING_HEADER)
        if ws is None:
            return False
        _ensure_header(ws, PENDING_HEADER)
        payload = [[r.get(c, "") for c in PENDING_HEADER] for r in rows]
        if not payload:
            return True
        existing = ws.get_all_values()
        next_row = len(existing) + 1
        end_row = next_row + len(payload) - 1
        end_col_a1 = gspread.utils.rowcol_to_a1(1, len(PENDING_HEADER))
        end_col_letters = "".join(ch for ch in end_col_a1 if ch.isalpha())
        ws.update(f"A{next_row}:{end_col_letters}{end_row}", payload,
                  value_input_option="USER_ENTERED")
        print(f"[sheets_logger] queued {len(payload)} near-miss row(s).")
        return True
    except Exception as e:
        print(f"[sheets_logger] pending append failed: {e}")
        return False


def update_pending_status(row_number: int, status: str, execute_date: str = "", note: str = ""):
    """
    Mark one pending row EXECUTED or EXPIRED.

    Writes only the status/execute_date/note cells, leaving `actual_entry`
    untouched — that column is filled by hand with the real fill price.
    Non-blocking; returns False on failure.
    """
    try:
        ws = _worksheet(tab=config.PENDING_ENTRIES_TAB, header=PENDING_HEADER)
        if ws is None:
            return False
        col_status = PENDING_HEADER.index("status") + 1
        col_exec = PENDING_HEADER.index("execute_date") + 1
        col_note = PENDING_HEADER.index("note") + 1
        ws.update_cell(row_number, col_status, status)
        if execute_date:
            ws.update_cell(row_number, col_exec, execute_date)
        if note:
            ws.update_cell(row_number, col_note, note)
        return True
    except Exception as e:
        print(f"[sheets_logger] pending status update failed: {e}")
        return False
