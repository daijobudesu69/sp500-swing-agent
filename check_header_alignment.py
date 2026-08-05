"""
check_header_alignment.py — Phase 2.6.1 / re-created 2.6.3

WHY THIS EXISTS
===============
append_shadow_rows() and append_pending() write by POSITION, not by name.
Neither ever reads the header. So if a hand-added column sits anywhere inside
the code-written block, every subsequent write lands one column to the right,
silently and forever.

This nearly happened once already: the operator's first shadow_log layout put
"Available in Pluang?" and "Remarks" at positions 31-32, inside the range the
code writes. The next run would have written `entry_type` into the Pluang
column and `force_closed` into Remarks, with no error and no log line.

Eyeballing a 45-column sheet does not catch this. This script does, by reading
row 1 live and comparing it position by position against the code's own HEADER
constants.

PHASE 2.6.3 NOTE — this file was listed as "New" in the Phase 2.6.1 summary
(§9) and named as a BLOCKING pre-flight action (§10 item 2), but was absent
from the repository. Re-created here so that action can actually be performed.
It is also the only automated check for the `pending_entries` short-header bug
(B2), which the documented watch instruction ("confirm bar_date is populated")
cannot detect — the CELL is populated; it is the HEADER that is missing.

USAGE
=====
    GSHEET_SA_JSON='<json>' GSHEET_ID='<id>' python check_header_alignment.py

Exit codes:
    0 = safe to run the pipeline
    1 = DO NOT RUN — a misalignment would corrupt data on the next write
    2 = could not check (not configured / API error). NOT a pass.

Read-only. Writes nothing to the sheet under any circumstance.
"""
import sys

try:
    from src import config
    from src import sheets_logger as sl
except Exception as e:                                   # pragma: no cover
    print(f"FATAL: cannot import src modules: {e}")
    sys.exit(2)


def _fmt(idx: int) -> str:
    """0-based index -> A1 column letters (0 -> A, 26 -> AA)."""
    letters = ""
    n = idx
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


def check_tab(tab_label: str, ws, expected: list) -> bool:
    """
    Compare row 1 of `ws` against `expected`, position by position, using the
    same normalisation the reader uses. Returns True if the code-written block
    is safe to write.
    """
    print(f"\n=== {tab_label} ===")
    try:
        row1 = ws.row_values(1)
    except Exception as e:
        print(f"  ERROR: could not read row 1: {e}")
        return False

    if not row1:
        print("  Row 1 is EMPTY — the first write will create the header. OK.")
        return True

    want = [sl._normalize(h) for h in expected]
    got = [sl._normalize(h) for h in row1]

    print(f"  Code writes  : {len(want)} columns (A..{_fmt(len(want) - 1)})")
    print(f"  Sheet header : {len(got)} columns (A..{_fmt(len(got) - 1)})")

    ok = True

    # 1. Header shorter than the code-written block. This is bug B2: writes
    #    still land in the unlabelled columns, but every READ silently drops
    #    them, because reads zip() header against row and stop at the shorter.
    if len(got) < len(want):
        ok = False
        print(f"  ✗ HEADER TOO SHORT — {len(want) - len(got)} column(s) are "
              f"written by code but have no header cell:")
        for i in range(len(got), len(want)):
            print(f"      {_fmt(i)}{'':<4} expected '{expected[i]}'  (MISSING)")
        print("    Effect: the values ARE written and NEVER read back.")
        print("    Fix: type the missing name(s) into row 1 by hand.")

    # 2. Position-by-position mismatch inside the code-written block. Anything
    #    here means the next write goes into the wrong column.
    mismatches = []
    for i in range(min(len(want), len(got))):
        if want[i] != got[i]:
            mismatches.append((i, expected[i], row1[i]))

    if mismatches:
        ok = False
        print(f"  ✗ {len(mismatches)} MISALIGNED column(s) inside the "
              f"code-written block:")
        for i, exp, act in mismatches:
            print(f"      {_fmt(i)}{'':<4} expected '{exp}'  ->  found '{act}'")
        print("    Effect: the NEXT write puts each field in the wrong column,")
        print("    silently, and overwrites whatever is there.")

    # 3. Duplicate normalised names anywhere. Reads resolve by normalised name
    #    and now take the LEFTMOST occurrence; a duplicate makes that choice
    #    implicit rather than intended.
    seen, dupes = set(), set()
    for name in got:
        if not name:
            continue
        if name in seen:
            dupes.add(name)
        seen.add(name)
    if dupes:
        print(f"  ⚠ Duplicate normalised header name(s): {sorted(dupes)}")
        print("    Reads take the LEFTMOST. Rename the manual column to be "
              "explicit. Not fatal — not blocking this check.")

    # 4. Manual columns to the right. Expected and healthy — report so the
    #    operator can confirm nothing they rely on has moved.
    if len(got) > len(want):
        extra = row1[len(want):]
        print(f"  ℹ {len(extra)} manual column(s) to the right of the "
              f"code-written block (never touched by writes):")
        print(f"      {', '.join(str(x) for x in extra)}")

    if ok and not mismatches and len(got) >= len(want):
        print(f"  ✓ ALL {len(want)} COLUMNS ALIGNED")
    return ok


def main() -> int:
    print("Header alignment check — read-only, writes nothing.")

    shadow_ws = sl._worksheet()
    if shadow_ws is None:
        print("\nERROR: Sheets not configured (missing libs or "
              "GSHEET_SA_JSON / GSHEET_ID). Cannot check.")
        print("This is NOT a pass — exit 2.")
        return 2

    ok_shadow = check_tab("shadow_log", shadow_ws, sl.HEADER)

    ok_pending = False
    try:
        pending_ws = sl._worksheet(tab=config.PENDING_ENTRIES_TAB,
                                   header=sl.PENDING_HEADER)
        if pending_ws is None:
            print("\n=== pending_entries ===\n  ERROR: tab unreachable.")
        else:
            ok_pending = check_tab("pending_entries", pending_ws,
                                   sl.PENDING_HEADER)
    except Exception as e:
        print(f"\n=== pending_entries ===\n  ERROR: {e}")

    print("\n" + "=" * 52)
    if ok_shadow and ok_pending:
        print("RESULT: SAFE TO RUN")
        return 0
    print("RESULT: DO NOT RUN — fix the header(s) above first.")
    print("Do not delete or reorder columns to fix this: deletion shifts rows")
    print("and breaks the R Calculator formulas. Add or rename cells only.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
