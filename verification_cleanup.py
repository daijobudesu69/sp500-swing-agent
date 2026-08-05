"""
verification_cleanup.py — retro-tag shadow_log rows that were never traded.

WHAT THIS IS FOR
================
Phase 2.6.1 added a write-time VETOED stamp: set VERIFICATION_RUN=true and
every row that run produces is born tagged, excluded from equity and from
Batch 4 statistics. That is the primary defence and this script does not
replace it.

This script is the RECOVERY path for the case the stamp did not happen:

  * a run that should have been tagged but the operator forgot the flag
  * rows written before the stamp existed (Batch 3 and earlier)
  * signals that were sent but deliberately not taken, and never marked

PHASE 2.6.3 MADE THIS MORE IMPORTANT, NOT LESS. The workflow's `verification`
input used to default to true. It now defaults to false (bug B8), because with
a single cron and no backup schedule, the only recovery path for a failed
scheduled run is a manual dispatch — and under the old default that recovery
run silently stamped every row VETOED, deleting real trades from equity.

Flipping the default fixed that, but it moved the risk: a deliberate test run
after 11 Aug that forgets to tick the box now produces UNTAGGED rows that look
exactly like real Batch 4 trades and were never executed. That contamination
is what this script cleans up. If you delete this file, reconsider flipping
`verification` back to `default: true` — do not take both.

WHAT IT CHANGES
===============
Exactly two columns, on rows you name:

    _shadow_discretionary_action  ->  VETOED
    _shadow_veto_reason           ->  your reason (or the config default)

Nothing else. It does not touch prices, outcomes, r_realized, hold_days or
risk_usd. It never deletes or reorders a row — deletion would shift every row
below it and break the R Calculator formulas.

DRY RUN BY DEFAULT. Nothing is written unless you pass --apply.

USAGE
=====
    # what would change, and what it does to equity — writes nothing
    python verification_cleanup.py --date 2026-08-05

    # a range of dates
    python verification_cleanup.py --from 2026-08-04 --to 2026-08-05

    # specific rows only
    python verification_cleanup.py --rows AAPL@2026-08-05 MSFT@2026-08-05

    # actually write
    python verification_cleanup.py --date 2026-08-05 --apply

Requires GSHEET_SA_JSON and GSHEET_ID in the environment, same as the pipeline.

SAFETY
======
  * dry run unless --apply
  * refuses to run without a date or row filter — there is no "tag everything"
  * skips rows already tagged VETOED (idempotent, safe to re-run)
  * WARNS LOUDLY on rows marked TAKEN or MODIFIED and skips them unless you
    pass --force, because tagging a genuinely executed trade as VETOED is the
    same corruption in the opposite direction
  * prints equity before and after so the effect is visible before you commit
"""
import argparse
import sys

from src import config
from src import sheets_logger as sl


# ------------------------------------------------------------------
# selection
# ------------------------------------------------------------------
def parse_row_specs(specs):
    """['AAPL@2026-08-05'] -> {('AAPL', '2026-08-05')}"""
    out = set()
    for spec in specs:
        if "@" not in spec:
            print(f"ERROR: '{spec}' is not TICKER@YYYY-MM-DD")
            sys.exit(2)
        tk, _, dt = spec.partition("@")
        out.add((tk.strip().upper(), sl._canon_date(dt)))
    return out


def select_rows(rows, args):
    """
    Return the subset of shadow rows the filters select.

    Dates are compared canonically (sl._canon_date), so this works whether the
    sheet renders dates as 2026-08-05 or 8/5/2026 — the same display-format
    trap that bug B13 fixed in update_shadow_rows.
    """
    wanted_specs = parse_row_specs(args.rows) if args.rows else None
    d_from = sl._canon_date(args.date or args.date_from or "")
    d_to = sl._canon_date(args.date or args.date_to or "")

    picked = []
    for r in rows:
        tk = str(r.get("ticker", "")).strip().upper()
        dt = sl._canon_date(r.get("date", ""))
        if not tk or not dt:
            continue
        if wanted_specs is not None:
            if (tk, dt) in wanted_specs:
                picked.append((tk, dt, r))
            continue
        if d_from and dt < d_from:
            continue
        if d_to and dt > d_to:
            continue
        picked.append((tk, dt, r))
    return picked


# ------------------------------------------------------------------
# equity impact
# ------------------------------------------------------------------
def equity_delta(rows_subset):
    """
    Dollar P&L currently attributed to the selected rows.

    This mirrors read_equity()'s attribution rule: a row contributes
    r_realized x risk_usd only when its action is in EQUITY_COUNTED_ACTIONS.
    Untagged rows contribute nothing under the current rule either — so for
    those the delta is $0 and the real damage is statistical, not monetary:
    they pollute Batch 4's win rate, expectancy and entry_type sample.
    """
    counted = {a.upper() for a in config.EQUITY_COUNTED_ACTIONS}
    total = 0.0
    for _, _, r in rows_subset:
        act = sl._normalize(r.get("discretionary_action", "")).upper()
        if act not in counted:
            continue
        rr = sl._to_float(r.get("r_realized"))
        rk = sl._to_float(r.get("risk_usd"))
        if rr is None or rk is None:
            continue
        total += rr * rk
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Retro-tag never-traded shadow_log rows as VETOED.")
    g = ap.add_argument_group("selection (at least one required)")
    g.add_argument("--date", help="single date, YYYY-MM-DD")
    g.add_argument("--from", dest="date_from", help="range start, inclusive")
    g.add_argument("--to", dest="date_to", help="range end, inclusive")
    g.add_argument("--rows", nargs="+", metavar="TICKER@DATE",
                   help="explicit rows, e.g. AAPL@2026-08-05")
    ap.add_argument("--reason", default=config.VERIFICATION_VETO_REASON,
                    help="veto_reason text to write")
    ap.add_argument("--force", action="store_true",
                    help="also tag rows currently marked TAKEN/MODIFIED "
                         "(dangerous — this erases real trades from equity)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this, nothing is changed.")
    args = ap.parse_args()

    if not (args.date or args.date_from or args.date_to or args.rows):
        ap.error("refusing to run with no filter — pass --date, --from/--to "
                 "or --rows. There is deliberately no 'tag everything' mode.")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"verification_cleanup — {mode}")
    print(f"reason: {args.reason}\n")

    rows = sl.read_shadow_rows()
    if rows is None:
        print("ERROR: could not read shadow_log (Sheets not configured or "
              "API error). Nothing done.")
        return 2
    print(f"read {len(rows)} shadow row(s)")

    picked = select_rows(rows, args)
    if not picked:
        print("No rows matched the filter. Nothing to do.")
        return 0

    counted = {a.upper() for a in config.EQUITY_COUNTED_ACTIONS}
    excluded = {a.upper() for a in config.EQUITY_EXCLUDED_ACTIONS}

    to_tag, already, protected = [], [], []
    for tk, dt, r in picked:
        act = sl._normalize(r.get("discretionary_action", "")).upper()
        if act in excluded:
            already.append((tk, dt, act))
        elif act in counted and not args.force:
            protected.append((tk, dt, act))
        else:
            to_tag.append((tk, dt, r, act or "(blank)"))

    print(f"\nmatched {len(picked)} row(s):")
    print(f"  {len(to_tag):>3} to tag")
    print(f"  {len(already):>3} already excluded (skipped — idempotent)")
    print(f"  {len(protected):>3} marked TAKEN/MODIFIED (skipped — use --force)")

    if protected:
        print("\n  ⚠ PROTECTED — these look like REAL trades:")
        for tk, dt, act in protected:
            print(f"      {tk}@{dt}  currently '{act}'")
        print("    Tagging these VETOED would delete genuine P&L from equity.")
        print("    Only use --force if you are certain they were never filled.")

    if not to_tag:
        print("\nNothing to change.")
        return 0

    print("\n  rows that WILL be tagged VETOED:")
    for tk, dt, r, act in to_tag:
        rr = r.get("r_realized", "") or "-"
        out = r.get("outcome", "") or "-"
        print(f"      {tk:<6} {dt}  action='{act}'  outcome={out}  R={rr}")

    delta = equity_delta([(t, d, r) for t, d, r, _ in to_tag])
    print(f"\n  equity currently attributed to these rows: ${delta:+.2f}")
    if abs(delta) < 0.005:
        print("    (they contribute $0 today — the contamination is "
              "STATISTICAL: they inflate the Batch 4 sample, win rate and "
              "entry_type counts without touching the balance)")
    else:
        print("    (this amount will be REMOVED from computed equity)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    updates = [{
        "ticker": tk,
        "signal_date": dt,
        "fields": {"discretionary_action": "VETOED", "veto_reason": args.reason},
    } for tk, dt, _, _ in to_tag]

    res = sl.update_shadow_rows(updates)
    print(f"\nupdated {res.get('updated', 0)} row(s)")
    if res.get("missed"):
        print(f"MISSED (unmatched or ambiguous): {res['missed']}")
        print("  These rows were NOT changed. A miss usually means the "
              "(ticker, date) key matched zero rows or more than one — check "
              "for duplicates before retrying.")
    if res.get("error"):
        print(f"ERROR: {res['error']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
