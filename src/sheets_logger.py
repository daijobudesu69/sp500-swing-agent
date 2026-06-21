"""
src/sheets_logger.py  —  Phase 4a
-----------------------------------
Appends each approved signal's shadow-log row to a Google Sheet.
Mirrors shadow_log.csv exactly. Append-only. NON-BLOCKING:
any failure here must never stop signal delivery (same contract as the
existing _append_shadow_log() try/except in main.py).

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
"""
import os
import json

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _LIBS_OK = True
except Exception:                       # libs not installed -> degrade silently
    _LIBS_OK = False

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Must match the exact keys written by _append_shadow_log() in main.py
# (same dicts are passed to append_shadow_rows() — no renaming/adapter layer,
# so this list IS the single source of truth for shadow_log.csv column order
# AND the Sheet column order, for any tab that is genuinely empty when first
# written. Existing hand-curated headers are left untouched — see
# _ensure_header() docstring above).
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
]


def _worksheet():
    """Returns the target worksheet, or None if not configured / unavailable."""
    if not _LIBS_OK:
        return None
    raw = os.environ.get("GSHEET_SA_JSON", "")
    sheet_id = os.environ.get("GSHEET_ID", "")
    tab = os.environ.get("GSHEET_TAB", "shadow_log")
    if not raw or not sheet_id:
        return None
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab, rows=1000, cols=len(HEADER))


def _ensure_header(ws) -> None:
    """
    Write HEADER only if row 1 is genuinely empty (brand-new tab).

    Round-2 fix: this function used to compare row 1 against HEADER and
    INSERT a fresh header on any mismatch — which duplicated the user's
    own hand-curated header (different naming convention + extra columns)
    every run. It no longer validates or replaces an existing header at
    all. If row 1 already has anything in it, this is a no-op — whatever
    header the user has in place is trusted as-is.
    """
    try:
        current_row1 = ws.row_values(1)
    except Exception:
        current_row1 = []

    if current_row1:
        # Row 1 already has content (canonical or hand-curated) — leave it.
        return

    # Row 1 is genuinely empty — safe to write the canonical header.
    ws.append_row(HEADER, value_input_option="USER_ENTERED")


def append_shadow_rows(rows):
    """
    rows: list[dict] keyed by HEADER (the SAME dicts written to shadow_log.csv
    by _append_shadow_log() in main.py — pass them through unmodified).
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
        end_col_a1 = gspread.utils.rowcol_to_a1(1, len(HEADER))      # e.g. "AC1"
        end_col_letters = "".join(ch for ch in end_col_a1 if ch.isalpha())
        target_range = f"A{next_row}:{end_col_letters}{end_row}"

        ws.update(target_range, payload, value_input_option="USER_ENTERED")
        print(f"[sheets_logger] wrote {len(payload)} row(s) to Google Sheet starting at row {next_row}.")
        return True
    except Exception as e:                               # non-blocking by design
        print(f"[sheets_logger] write failed (non-blocking): {e}")
        return False
