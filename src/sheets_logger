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
# AND the Sheet column order).
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


def append_shadow_rows(rows):
    """
    rows: list[dict] keyed by HEADER (the SAME dicts written to shadow_log.csv
    by _append_shadow_log() in main.py — pass them through unmodified).
    Returns True on success, False on any failure (never raises).
    """
    try:
        ws = _worksheet()
        if ws is None:
            print("[sheets_logger] not configured (missing libs/secrets) — skipped.")
            return False
        if not ws.get_all_values():                      # write header once
            ws.append_row(HEADER, value_input_option="USER_ENTERED")
        payload = [[r.get(c, "") for c in HEADER] for r in rows]
        ws.append_rows(payload, value_input_option="USER_ENTERED")
        print(f"[sheets_logger] appended {len(payload)} row(s) to Google Sheet.")
        return True
    except Exception as e:                               # non-blocking by design
        print(f"[sheets_logger] append failed (non-blocking): {e}")
        return False
