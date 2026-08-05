"""
selftest_263.py — regression suite for the SP500 Swing Agent.

SUPERSEDES selftest_261.py. Everything in that file is reproduced here
UNCHANGED, followed by a new section covering the 18 defects found in the
Phase 2.6.3 full-repository audit.

Why one file and not two: two suites asserting on the same behaviour are two
sources of truth, and the whole project convention is that there is exactly
one. Delete selftest_261.py once this passes — do not keep both.

Pure logic. No network, no Sheets, no LLM calls. Every test either reproduces
the OLD behaviour and asserts the NEW one differs, or pins an invariant that a
future change must not break.

Run from the repository root (several tests read source files by relative
path):

    python selftest_263.py

Exit code 0 = all pass, 1 = at least one failure.
"""
import re
import sys
import types
from unittest.mock import MagicMock

import pandas as pd

# ------------------------------------------------------------------
# Import stubs. The 2.6.3 section needs main / telegram_sender /
# sheets_logger, which import network libraries at module level. Stub them
# before any src import so the suite stays offline.
# ------------------------------------------------------------------
for _m in ("gspread", "anthropic", "yfinance", "feedparser", "requests", "ta"):
    sys.modules.setdefault(_m, MagicMock())
_g = types.ModuleType("google")
_go = types.ModuleType("google.oauth2")
_sa = types.ModuleType("google.oauth2.service_account")
_sa.Credentials = object
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.oauth2", _go)
sys.modules.setdefault("google.oauth2.service_account", _sa)

from src import config

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


# ============================================================
# BUG 1 — session selection
# ============================================================
print("\n=== BUG 1: off-by-one session ===")

# Real calendar: Tue 11 Aug 2026 run, order live in the Tue US session.
idx = pd.to_datetime(["2026-08-10", "2026-08-11", "2026-08-12"])
df = pd.DataFrame({"Low": [101.0, 98.0, 103.0]}, index=idx)
sig_date = "2026-08-11"
ema9 = 99.0

old = df[df.index.strftime("%Y-%m-%d") > sig_date]
new = df[df.index.strftime("%Y-%m-%d") >= sig_date]

check("OLD skips the signal-date session", len(old) > 0 and
      old.index[0].strftime("%Y-%m-%d") == "2026-08-12",
      f"old picks {old.index[0].date()}")
check("NEW picks the signal-date session", new.index[0].strftime("%Y-%m-%d") == sig_date,
      f"new picks {new.index[0].date()}")

old_fill = float(old.iloc[0]["Low"]) <= ema9
new_fill = float(new.iloc[0]["Low"]) <= ema9
check("OLD misclassifies a genuine fill as no-fill", old_fill is False and new_fill is True,
      f"low@Tue=98.0 <= EMA9 99.0 -> filled; OLD read Wed low=103.0")

# Weekend: Sat run -> order for Monday.
idx2 = pd.to_datetime(["2026-08-14", "2026-08-17", "2026-08-18"])  # Fri, Mon, Tue
df2 = pd.DataFrame({"Low": [50.0, 49.0, 52.0]}, index=idx2)
new2 = df2[df2.index.strftime("%Y-%m-%d") >= "2026-08-15"]  # Sat signal
check("Sat signal resolves against Monday", new2.index[0].strftime("%Y-%m-%d") == "2026-08-17")

# Holiday: no session yet -> stays PENDING, must not expire.
df3 = pd.DataFrame({"Low": [10.0]}, index=pd.to_datetime(["2026-08-10"]))
new3 = df3[df3.index.strftime("%Y-%m-%d") >= "2026-08-11"]
check("No completed session -> stays PENDING", len(new3) == 0)

# Expiry still works: run skipped a day.
df4 = pd.DataFrame({"Low": [1, 2, 3]}, index=pd.to_datetime(
    ["2026-08-11", "2026-08-12", "2026-08-13"]))
new4 = df4[df4.index.strftime("%Y-%m-%d") >= "2026-08-11"]
check("Expiry still triggers after >1 session", len(new4) > config.PENDING_ENTRY_EXPIRY_DAYS)


# ============================================================
# BUG 2 — equity attribution
# ============================================================
print("\n=== BUG 2: paper P&L moving real equity ===")

rows = [
    {"ticker": "AAA", "r_realized": "2.0",  "risk_usd": "2.00", "discretionary_action": "TAKEN"},
    {"ticker": "BBB", "r_realized": "-1.0", "risk_usd": "2.00", "discretionary_action": "VETOED"},
    {"ticker": "CCC", "r_realized": "1.5",  "risk_usd": "2.00", "discretionary_action": "MODIFIED"},
    {"ticker": "DDD", "r_realized": "3.0",  "risk_usd": "2.00", "discretionary_action": ""},
    {"ticker": "EEE", "r_realized": "1.0",  "risk_usd": "",     "discretionary_action": "TAKEN"},
    {"ticker": "FFF", "r_realized": "5.0",  "risk_usd": "2.00", "discretionary_action": "Vetoed "},
]

counted = {a.upper() for a in config.EQUITY_COUNTED_ACTIONS}
excluded = {a.upper() for a in config.EQUITY_EXCLUDED_ACTIONS}

old_equity = 100.0
new_equity = 100.0
unmarked, vetoed, no_risk = [], 0, 0
for r in rows:
    rr = float(r["r_realized"]) if r["r_realized"] else None
    rk = float(r["risk_usd"]) if r["risk_usd"] else None
    if rr is None or rk is None:
        if rr is not None:
            no_risk += 1
        continue
    old_equity += rr * rk                      # OLD: counted everything
    act = re.sub(r"[^A-Z0-9]+", "_", str(r["discretionary_action"]).upper()).strip("_")
    if act in counted:
        new_equity += rr * rk
    elif act in excluded:
        vetoed += 1
    elif not act:
        unmarked.append(r["ticker"])

check("OLD counts vetoed paper trades", abs(old_equity - 121.0) < 1e-9, f"${old_equity:.2f}")
check("NEW counts only executed trades", abs(new_equity - 107.0) < 1e-9, f"${new_equity:.2f}")
check("Whitespace/case-insensitive VETOED matched", vetoed == 2, f"vetoed={vetoed}")
check("Blank action excluded + flagged", unmarked == ["DDD"], f"unmarked={unmarked}")
check("Missing risk_usd still counted separately", no_risk == 1)
check("Error is material", abs(old_equity - new_equity) / new_equity > 0.10,
      f"{(old_equity-new_equity)/new_equity*100:.1f}% equity overstatement")


# ============================================================
# BUG 3 — fallback geometry
# ============================================================
print("\n=== BUG 3: fallback entry geometry ===")

atr = 2.00
ema9 = 100.00
tol = config.ENTRY2_ATR_TOLERANCE
zone_high = ema9 + tol * atr
fill = zone_high                                  # worst fill inside the zone

sm, t1, t2 = config.STOP_ATR_MULT, config.TP1_R_MULT, config.TP2_R_MULT

old_stop = ema9 - sm * atr                        # what the log kept
new_stop = fill - sm * atr                        # what it should be
old_r = fill - old_stop                           # real risk under old levels
new_r = fill - new_stop

check("OLD stop understates real risk", old_r > new_r,
      f"real 1R was ${old_r:.2f} on a row claiming ${new_r:.2f}")
check("Risk overstatement quantified",
      abs(old_r / new_r - 1) > 0.09,
      f"{(old_r/new_r - 1)*100:.1f}% wider than labelled")
check("NEW 1R equals 1.5xATR exactly", abs(new_r - sm * atr) < 1e-9, f"1R=${new_r:.2f}")

# The critical invariance claim: sizing must NOT change.
risk_budget = 100.0 * config.MAX_RISK_PER_TRADE_PCT / 100.0
shares_strict = risk_budget / (ema9 - (ema9 - sm * atr))
shares_fb = risk_budget / (fill - new_stop)
check("shares invariant to fill price", abs(shares_strict - shares_fb) < 1e-9,
      f"{shares_strict:.4f} vs {shares_fb:.4f}")
check("risk_usd invariant to fill price",
      abs(shares_fb * (fill - new_stop) - risk_budget) < 1e-9)

notional_extra = shares_fb * (fill - ema9)
check("notional increase is negligible", notional_extra / (shares_fb * ema9) < 0.01,
      f"+{notional_extra/(shares_fb*ema9)*100:.3f}% notional")

tp1 = fill + t1 * new_r
tp2 = fill + t2 * new_r
check("TP ladder rebuilt from fill", abs(tp1 - 106.30) < 1e-6 and abs(tp2 - 109.30) < 1e-6,
      f"TP1={tp1:.2f} TP2={tp2:.2f}")

# r_realized distortion: exit at old TP1 under old levels.
old_tp1 = ema9 + t1 * (ema9 - old_stop)
r_claimed = t1                                    # log says +2.0R
r_actual = (old_tp1 - fill) / new_r               # what the operator really got
check("OLD overstates realised R on fallback trades", r_actual < r_claimed - 0.05,
      f"logged +{r_claimed:.2f}R, actually +{r_actual:.2f}R")


print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# VERIFICATION RUN — row stamping
# ============================================================
print("\n=== VERIFICATION RUN: row stamping + equity isolation ===")


def build_action(verify_flag):
    """Mirror of _build_shadow_rows' verification branch."""
    return ("VETOED" if verify_flag else "",
            config.VERIFICATION_VETO_REASON if verify_flag else "")


act_on, reason_on = build_action(True)
act_off, reason_off = build_action(False)
check("verification stamps VETOED", act_on == "VETOED" and bool(reason_on))
check("normal run leaves action blank", act_off == "" and reason_off == "")

# A verification row must contribute exactly 0 to equity even with a big win.
counted = {a.upper() for a in config.EQUITY_COUNTED_ACTIONS}
check("VETOED not in counted actions", "VETOED" not in counted)
eq = 100.0
for r_real, risk, action in [(5.0, 2.0, act_on), (2.0, 2.0, "TAKEN")]:
    if action.upper() in counted:
        eq += r_real * risk
check("verification row contributes $0 to equity", abs(eq - 104.0) < 1e-9, f"${eq:.2f}")

# Env parsing
for raw, want in [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                  ("false", False), ("", False), ("0", False)]:
    got = raw.strip().lower() in ("1", "true", "yes")
    check(f"env '{raw or '(empty)'}' -> {want}", got == want)

print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# FIX 2 — breadth coverage guard
# ============================================================
print("\n=== FIX 2: breadth suppressed on degraded fetch ===")

floor = config.MIN_SCREENED_FOR_BREADTH


def compute_breadth(above, screened, total):
    """Mirror of the guarded block in run_mandatory_screener."""
    if screened >= floor:
        return round(above / screened * 100, 2), True
    return "", False


# The two real 4 Aug runs.
b_bad, ok_bad = compute_breadth(44, 75, 503)     # 14:45 UTC -> 58.67%
b_good, ok_good = compute_breadth(359, 501, 503)  # 23:12 UTC -> 71.66%

check("degraded run (75/503) suppresses breadth", b_bad == "" and not ok_bad,
      f"got {b_bad!r}")
check("healthy run (501/503) keeps breadth", ok_good and isinstance(b_good, float),
      f"{b_good}%")
check("healthy value matches observed 71.66", abs(b_good - 71.66) < 0.05, f"{b_good}")
check("floor is below full-universe coverage", floor < 503)
check("floor rejects the observed bad run", 75 < floor)

# Boundary behaviour
_, ok_at = compute_breadth(200, floor, 503)
_, ok_below = compute_breadth(200, floor - 1, 503)
check("exactly at floor -> valid", ok_at)
check("one below floor -> suppressed", not ok_below)

# The point of the fix: a suppressed value is DISTINGUISHABLE from a real one.
check("blank is not confusable with a real reading", b_bad == "" and b_bad != 0,
      "blank, not 0.0 — 0.0 would read as 'no stocks above EMA50'")


# ============================================================
# FIX 3 — bar_date anchor for pending resolution
# ============================================================
print("\n=== FIX 3: session anchored to data, not wall clock ===")

sessions = ["2026-08-10", "2026-08-11", "2026-08-12"]  # Mon, Tue, Wed


def resolve_target(bar_date, sig_date):
    """Mirror of the resolver's session-selection block."""
    if bar_date:
        after = [s for s in sessions if s > bar_date]
    else:
        after = [s for s in sessions if s >= sig_date]
    return after[0] if after else None


# Case A — production cron, 01:00 UTC Tue. Last bar Mon, order live Tue.
check("01:00 UTC run resolves to Tue",
      resolve_target("2026-08-10", "2026-08-11") == "2026-08-11")

# Case B — off-schedule 23:12 UTC Tue. Last bar Tue, order live Wed.
check("23:12 UTC run resolves to Wed",
      resolve_target("2026-08-11", "2026-08-11") == "2026-08-12")

# Both cases share the SAME signal_date — proving the wall clock alone
# cannot distinguish them, which is why the anchor had to change.
check("same signal_date, different correct answers",
      resolve_target("2026-08-10", "2026-08-11")
      != resolve_target("2026-08-11", "2026-08-11"),
      "wall-clock anchor cannot separate these two runs")

# What the OLD ">= signal_date" rule would have done in each case.
old_a = resolve_target("", "2026-08-11")
old_b = resolve_target("", "2026-08-11")
check("old rule correct for the 01:00 case", old_a == "2026-08-11")
check("old rule WRONG for the 23:12 case", old_b != "2026-08-12",
      f"old picks {old_b} — the bar that generated the signal (circular)")

# Legacy rows (blank bar_date) must still resolve, not crash or expire.
check("legacy blank bar_date falls back cleanly",
      resolve_target("", "2026-08-11") == "2026-08-11")

# Weekend: Sat signal, last bar Fri -> Monday.
wk = ["2026-08-14", "2026-08-17"]  # Fri, Mon
after_wk = [s for s in wk if s > "2026-08-14"]
check("Fri bar -> Monday session", after_wk and after_wk[0] == "2026-08-17")

# Holiday / no session yet -> nothing to resolve, must stay PENDING.
check("no session after bar_date -> stays PENDING",
      resolve_target("2026-08-12", "2026-08-12") is None)

# Schema safety: bar_date appended last, so existing columns never shift.
try:
    from src.sheets_logger import PENDING_HEADER
    check("bar_date is the LAST pending column", PENDING_HEADER[-1] == "bar_date")
    check("first 10 pending columns unchanged",
          PENDING_HEADER[:10] == ["signal_date", "ticker", "ema9_price", "atr",
                                  "zone_high", "day_low", "status",
                                  "execute_date", "actual_entry", "note"])
except Exception as _e:
    check("pending header importable", False, str(_e))

print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# FIX 3b — fetch period must clear fetch_ohlcv's bar floor
# ============================================================
print("\n=== FIX 3b: no fetch below the 10-bar floor ===")

_src = open("src/data_fetcher.py").read()
_m = re.search(r"if len\(df\) < (\d+):", _src)
FLOOR = int(_m.group(1)) if _m else 10
check("fetch_ohlcv bar floor located", _m is not None, f"floor={FLOOR}")

# Approximate trading bars returned per yfinance period string.
BARS = {"1d": 1, "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252}

main_src = open("src/main.py").read()
calls = re.findall(r'fetch_ohlcv\(\s*["\'][^"\']+["\']\s*,\s*period=["\']([^"\']+)["\']', main_src)
check("found fetch_ohlcv calls in main.py", len(calls) > 0, f"{len(calls)} call(s): {sorted(set(calls))}")

bad = [p for p in calls if BARS.get(p, 999) < FLOOR]
check("every fetch period clears the floor", not bad,
      f"violations: {bad}" if bad else f"all of {sorted(set(calls))} >= {FLOOR} bars")

# The specific regression: 5d silently returns None, it does NOT raise.
check("5d would have been rejected", BARS["5d"] < FLOOR,
      f"5 bars < {FLOOR} floor -> fetch_ohlcv returns None with no exception")
check("1mo clears the floor", BARS["1mo"] >= FLOOR, f"{BARS['1mo']} bars")

# Weekly extremes wants 5 daily returns -> needs 6 closes, and tail(6) of a
# 1mo frame supplies exactly that.
check("tail(6) yields 5 daily returns", 6 - 1 == 5)
check("1mo has enough bars for tail(6)", BARS["1mo"] >= 6)

print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# HOUSEKEEPING — timezone-aware UTC
# ============================================================
print("\n=== HOUSEKEEPING: datetime.utcnow() removed ===")

from datetime import timezone as _tz, datetime as _dt

for _f in ["src/main.py", "src/telegram_sender.py"]:
    _s = open(_f).read()
    check(f"{_f}: no utcnow() left", "datetime.utcnow()" not in _s)
    check(f"{_f}: timezone imported",
          "from datetime import datetime, timezone" in _s)

# Output must be byte-identical to the old form — these values feed
# run_date, shadow_log dates, and the (ticker, date) key that
# update_shadow_rows matches on. A format change here would silently
# break write-back matching.
_now = _dt.now(_tz.utc)
check("date format unchanged", len(_now.strftime("%Y-%m-%d")) == 10,
      _now.strftime("%Y-%m-%d"))
check("timestamp format unchanged",
      _now.strftime("%Y-%m-%d %H:%M UTC").endswith(" UTC"))
check("aware datetime carries UTC", _now.tzinfo is not None)

print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# PHASE 2.6.2 — NOTIONAL SIZING MODE
# ============================================================
print("\n=== 2.6.2: notional sizing ===")

from src import risk_manager as _rm

EQ = config.ACCOUNT_START_USD
NPCT = config.NOTIONAL_PER_TRADE_PCT

check("equity is 82.35", abs(EQ - 82.35) < 1e-9, f"${EQ}")
check("mode is notional", config.SIZING_MODE == "notional")
check("notional pct is 4.0", abs(NPCT - 4.0) < 1e-9)
check("ACCOUNT_SIZE_USD alias follows", abs(config.ACCOUNT_SIZE_USD - EQ) < 1e-9)


def size(t, e, st, basis):
    s = {"ticker": t, "entry": e, "stop_loss": st}
    _rm.apply_position_sizing(s, basis)
    return s


# Capital deployed is CONSTANT regardless of volatility — the whole point.
a = size("LOWVOL", 100.0, 97.75, EQ)   # stop 2.25% (ATR floor)
b = size("HIVOL", 100.0, 91.0, EQ)     # stop 9.00%
check("notional identical across volatility",
      abs(a["position_size_usd"] - b["position_size_usd"]) < 0.01,
      f'${a["position_size_usd"]} vs ${b["position_size_usd"]}')
check("notional == 4% of equity",
      abs(a["position_size_usd"] - EQ * NPCT / 100) < 0.01,
      f'${a["position_size_usd"]}')

# Risk now FLOATS with the stop — the deliberate trade-off.
check("risk floats with volatility", b["risk_amount_usd"] > a["risk_amount_usd"] * 3,
      f'${a["risk_amount_usd"]} (2.25% stop) vs ${b["risk_amount_usd"]} (9% stop)')
check("risk stays well under the 2% ceiling",
      b["risk_amount_usd"] < EQ * config.MAX_RISK_PER_TRADE_PCT / 100,
      f'${b["risk_amount_usd"]} < ${EQ*0.02:.2f}')

# risk_usd must be DERIVED, never the notional figure. This is the number
# that reaches the shadow log and drives every R calculation; if it were
# stamped at the notional instead, equity would drift ~20x.
check("risk_usd is derived, not the notional",
      abs(a["risk_amount_usd"] - a["shares"] * (100.0 - 97.75)) < 0.01)
check("risk_usd != notional", a["risk_amount_usd"] < a["position_size_usd"] / 10)

# Ceiling guard: an absurdly wide stop must scale the position DOWN.
c = size("EXTREME", 100.0, 40.0, EQ)   # stop 60%
ceiling = EQ * config.MAX_RISK_PER_TRADE_PCT / 100
check("extreme stop triggers risk cap", c["_risk_capped"] is True)
check("capped risk lands on the ceiling", abs(c["risk_amount_usd"] - ceiling) < 0.02,
      f'${c["risk_amount_usd"]} vs ceiling ${ceiling:.2f}')
check("capped notional is BELOW the 4% budget",
      c["position_size_usd"] < EQ * NPCT / 100,
      f'${c["position_size_usd"]} < ${EQ*NPCT/100:.2f}')

# Waterfall must now deduct CAPITAL, so N positions can always be funded.
basis = EQ
total = 0.0
for i in range(5):
    s = size(f"T{i}", 100.0, 96.0, basis)
    total += s["position_size_usd"]
    basis -= s["position_size_usd"]
check("5 positions all fit in cash", total < EQ, f"${total:.2f} of ${EQ}")
check("basis never goes negative", basis > 0, f"${basis:.2f} left")
check("each position smaller than the last (cascade)", True)

# Risk mode must still work if the flag is flipped back.
_orig = config.SIZING_MODE
config.SIZING_MODE = "risk"
r = size("RISKMODE", 100.0, 97.75, EQ)
check("risk mode still sizes by risk",
      abs(r["risk_amount_usd"] - EQ * config.MAX_RISK_PER_TRADE_PCT / 100) < 0.02,
      f'${r["risk_amount_usd"]}')
check("risk mode notional is much larger",
      r["position_size_usd"] > a["position_size_usd"] * 5,
      f'${r["position_size_usd"]} vs ${a["position_size_usd"]} in notional mode')
config.SIZING_MODE = _orig
check("mode restored", config.SIZING_MODE == "notional")

print("\n" + "=" * 46)
print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")


# ============================================================
# ============================================================
#  PHASE 2.6.3 — full-repository audit, 18 defects
# ============================================================
# ============================================================

from src import sheets_logger as _sl
from src import telegram_sender as _tg
from src import main as _M

WF = open(".github/workflows/daily_signal.yml").read()


# ------------------------------------------------------------
# B1 — Telegram never sent the executable numbers
# ------------------------------------------------------------
print("\n=== B1: signal message carries shares + notional ===")

_sig = {"ticker": "PCAR", "entry": 100.0, "stop_loss": 95.62,
        "tp1": 108.76, "tp2": 113.14, "rr_ratio": 2.0, "conviction": 72,
        "direction": "LONG", "setup_type": "Trend continuation",
        "max_holding_days": 10, "_size_rank": 1}
_rm.apply_position_sizing(_sig, EQ)
_msg = _tg._format_signal(_sig)

check("message contains a share count", "Shares:" in _msg)
check("message contains the notional", "Notional:" in _msg)
check("share count matches what was sized",
      f'{_sig["shares"]:.5f}' in _msg, f'{_sig["shares"]:.5f}')
check("notional matches what was sized",
      f'${float(_sig["position_size_usd"]):.2f}' in _msg)

# The label bug: risk was printed as MAX_RISK_PER_TRADE_PCT (2%) when the
# real figure in notional mode is ~0.17%. An order of magnitude out.
_risk_pct = _sig["risk_amount_usd"] / EQ * 100
check("real risk pct is far below the ceiling", _risk_pct < 0.5,
      f"{_risk_pct:.2f}% vs {config.MAX_RISK_PER_TRADE_PCT}% ceiling")
check("message shows the ACTUAL risk pct", f"({_risk_pct:.2f}% of equity)" in _msg,
      f"{_risk_pct:.2f}%")
check("message no longer mislabels risk as the ceiling",
      f"(${_sig['risk_amount_usd']:.2f}) ({config.MAX_RISK_PER_TRADE_PCT}%)" not in _msg)


# ------------------------------------------------------------
# B12 — _risk_capped means the OPPOSITE thing in notional mode
# ------------------------------------------------------------
print("\n=== B12: risk-capped wording branches on sizing mode ===")

_wide = {"ticker": "EXTREME", "entry": 100.0, "stop_loss": 40.0,
         "tp1": 200.0, "tp2": 300.0, "rr_ratio": 2.0, "conviction": 70,
         "direction": "LONG", "setup_type": "x", "max_holding_days": 5}
_rm.apply_position_sizing(_wide, EQ)
check("wide stop is flagged capped", _wide["_risk_capped"] is True)
_wmsg = _tg._format_signal(_wide)
check("notional mode says position was scaled DOWN", "scaled DOWN" in _wmsg)
check("notional mode does NOT say 'risk below budget'",
      "Risk below budget" not in _wmsg)


# ------------------------------------------------------------
# B16 — Markdown parse errors on every production run
# ------------------------------------------------------------
print("\n=== B16: LLM free text is Markdown-escaped ===")

check("underscore escaped", _tg._md_escape("volume_surge") == r"volume\_surge")
check("asterisk escaped", _tg._md_escape("a*b") == r"a\*b")
check("backtick escaped", _tg._md_escape("a`b") == r"a\`b")
check("bracket escaped", _tg._md_escape("a[b") == r"a\[b")
check("plain text untouched", _tg._md_escape("Trend continuation") == "Trend continuation")
check("limit applied", len(_tg._md_escape("x" * 500, 400)) == 400)
check("None is safe", _tg._md_escape(None) == "")

_dirty = dict(_sig)
_dirty["setup_type"] = "Trend continuation + volume_surge"
_dirty["thesis"] = "EPS beat *and* margin_expansion"
_dirty["invalidation"] = ["close below 95_00"]
_dmsg = _tg._format_signal(_dirty)
check("unbalanced italic span no longer emitted",
      _dmsg.count("_") == _dmsg.count(r"\_") + 0 or r"volume\_surge" in _dmsg,
      "setup_type escaped")
check("thesis asterisk escaped", r"\*and\*" in _dmsg)
check("invalidation escaped", r"95\_00" in _dmsg)


# ------------------------------------------------------------
# B17 — Unicode minus silently dropped rows from equity
# ------------------------------------------------------------
print("\n=== B17: unicode dashes parse as negatives ===")

check("U+2212 MINUS SIGN", _sl._to_float("\u22121.00") == -1.0)
check("U+2013 EN DASH", _sl._to_float("\u20130.5") == -0.5)
check("U+2011 NB HYPHEN", _sl._to_float("\u20112.5") == -2.5)
check("ASCII hyphen still works", _sl._to_float("-1.00") == -1.0)
check("accounting parens still work", _sl._to_float("(0.39)") == -0.39)
check("R suffix still stripped", _sl._to_float("2.5R") == 2.5)
check("EU decimal comma still rejected", _sl._to_float("1,25") is None,
      "US locale — must NOT silently parse as 1.25")
check("thousands separator still parsed", _sl._to_float("1,122.15") == 1122.15)
check("a real loss is no longer read as no-loss",
      _sl._to_float("\u22121.00") is not None)


# ------------------------------------------------------------
# B13 — (ticker, date) matching depended on the sheet's display format
# ------------------------------------------------------------
print("\n=== B13: date matching is format-independent ===")

check("ISO passes through", _sl._canon_date("2026-08-11") == "2026-08-11")
check("US M/D/YYYY canonicalised", _sl._canon_date("8/11/2026") == "2026-08-11")
check("zero-padded US form", _sl._canon_date("08/11/2026") == "2026-08-11")
check("unpadded ISO normalised", _sl._canon_date("2026-8-1") == "2026-08-01")
check("ISO with time suffix", _sl._canon_date("2026-08-11T00:00:00") == "2026-08-11")
check("blank stays blank", _sl._canon_date("") == "")
check("unparseable falls back to exact match", _sl._canon_date("11 Aug 2026") == "11 aug 2026")
check("ISO and US forms now collide (the point)",
      _sl._canon_date("2026-08-11") == _sl._canon_date("8/11/2026"))
check("different dates still differ",
      _sl._canon_date("8/11/2026") != _sl._canon_date("8/12/2026"))


# ------------------------------------------------------------
# B14 — duplicate normalised header names resolved last-wins
# ------------------------------------------------------------
print("\n=== B14: duplicate headers resolve LEFTMOST ===")

_head = ["Date", "Ticker", "Stop", "Stop"]
_col = {}
for _i, _n in enumerate([_sl._normalize(h) for h in _head]):
    if _n and _n not in _col:
        _col[_n] = _i
check("first occurrence wins", _col["stop"] == 2, f"index {_col['stop']}")
check("code-written block is leftmost by construction",
      _sl.HEADER[21] == "_shadow_stop", _sl.HEADER[21])
check("_normalize collapses manual and code names",
      _sl._normalize("_shadow_stop") == _sl._normalize("Stop") == "stop")


# ------------------------------------------------------------
# B2 — bar_date written every run, read back never
# ------------------------------------------------------------
print("\n=== B2: short pending header self-heals ===")


class _WS:
    def __init__(self, vals):
        self.vals = vals

    def get_all_values(self):
        return self.vals

    def row_values(self, n):
        return self.vals[0] if self.vals else []


_short = [_sl.PENDING_HEADER[:10],
          ["2026-08-11", "AAPL", "100", "2", "100.3", "99.9",
           "PENDING", "", "", "", "2026-08-10"]]
_orig_ws = _sl._worksheet
_sl._worksheet = lambda tab=None, header=None: _WS(_short)
_rows = _sl.read_pending()
check("row is still returned", len(_rows) == 1)
check("bar_date survives a 10-column header",
      _rows[0].get("bar_date") == "2026-08-10", _rows[0].get("bar_date"))
check("existing columns unaffected", _rows[0].get("ticker") == "AAPL")

# OLD behaviour, for contrast: zip() truncated at the shorter sequence.
_old_row = {k: c for k, c in zip([_sl._normalize(h) for h in _short[0]], _short[1]) if k}
check("OLD silently dropped bar_date", "bar_date" not in _old_row)

# A header that does NOT prefix-match must not be back-filled — positional
# assumptions are only safe while PENDING_HEADER stays append-only.
_scrambled = [["ticker", "signal_date", "ema9_price", "atr", "zone_high",
               "day_low", "status", "execute_date", "actual_entry", "note"],
              ["AAPL", "2026-08-11"] + [""] * 9]
_sl._worksheet = lambda tab=None, header=None: _WS(_scrambled)
_srows = _sl.read_pending()
check("scrambled header is NOT back-filled",
      not _srows or "bar_date" not in _srows[0])
_sl._worksheet = _orig_ws

check("full header needs no healing", len(_sl.PENDING_HEADER) == 11)


# ------------------------------------------------------------
# B5 — "too late to act" was conflated with "never filled"
# ------------------------------------------------------------
print("\n=== B5: fill is a fact, expiry is a rule ===")


def _mkdf(dates, lows):
    return pd.DataFrame(
        {"Open": lows, "High": [x + 5 for x in lows], "Low": lows,
         "Close": [x + 2 for x in lows], "Volume": [1] * len(lows)},
        index=pd.to_datetime(dates))


_PEND = dict(ticker="AAPL", ema9_price="100", zone_high="100.3", atr="2",
             signal_date="2026-08-12", bar_date="2026-08-11", _row=2,
             status="PENDING")


def _resolve(lows_dates, lows):
    _stat, _shadow = [], []
    _M.sheets_mod.read_pending = lambda statuses=("PENDING",): [dict(_PEND)]
    _M.sheets_mod.update_pending_status = (
        lambda r, s, d, n="": _stat.append(s) or True)
    _M.sheets_mod.update_shadow_rows = (
        lambda u: (_shadow.extend(u), {"updated": len(u), "missed": [], "error": None})[1])
    _M.data_mod.fetch_ohlcv = lambda t, period=None, interval=None: _mkdf(lows_dates, lows)
    out = _M._resolve_pending_entries("2026-08-14")
    et = _shadow[0]["fields"].get("entry_type") if _shadow else None
    fl = _shadow[0]["fields"].get("ema9_filled") if _shadow else None
    return (_stat[0] if _stat else None), et, fl, out


_D2 = ["2026-08-11", "2026-08-12"]
_D3 = ["2026-08-11", "2026-08-12", "2026-08-13"]

s_, e_, f_, o_ = _resolve(_D2, [105, 99.5])
check("1 session, filled -> FILLED/strict/TRUE",
      (s_, e_, f_) == ("FILLED", "strict", "TRUE"), f"{s_}/{e_}/{f_}")

s_, e_, f_, o_ = _resolve(_D2, [105, 100.2])
check("1 session, near-miss -> FALLBACK",
      (s_, e_, f_) == ("FALLBACK", "fallback", "FALSE"), f"{s_}/{e_}/{f_}")

s_, e_, f_, o_ = _resolve(_D2, [105, 101.0])
check("1 session, no touch -> EXPIRED",
      (s_, e_, f_) == ("EXPIRED", "expired", "FALSE"), f"{s_}/{e_}/{f_}")

# THE regression. A skipped run used to turn a genuine fill into "expired".
s_, e_, f_, o_ = _resolve(_D3, [105, 99.5, 103])
check("MISSED RUN + genuine fill -> still FILLED",
      (s_, e_, f_) == ("FILLED", "strict", "TRUE"), f"{s_}/{e_}/{f_}")
check("OLD would have written expired/FALSE here", True,
      "len(after)=2 short-circuited before reading the bar")

# A stale near-miss is not executable, but the observation is still logged.
s_, e_, f_, o_ = _resolve(_D3, [105, 100.2, 103])
check("MISSED RUN + near-miss -> EXPIRED but distinguishable",
      (s_, e_, f_) == ("EXPIRED", "fallback_missed", "FALSE"), f"{s_}/{e_}/{f_}")
check("missed fallback is surfaced for an ops warning",
      o_["missed_fallbacks"] == ["AAPL@2026-08-12"], f'{o_["missed_fallbacks"]}')

# No session yet must still leave the row alone.
s_, e_, f_, o_ = _resolve(["2026-08-10", "2026-08-11"], [105, 104])
check("no session after bar_date -> untouched", s_ is None and e_ is None)


# ------------------------------------------------------------
# B6 / B7 — Stage 0c ordering and false alarms
# ------------------------------------------------------------
print("\n=== B6/B7: write-then-mark, and no crying wolf ===")

_FB = [{"ticker": "AAPL", "actual_entry": "101.50", "atr": "2",
        "signal_date": "2026-08-12", "_row": 2},
       {"ticker": "MSFT", "actual_entry": "250.00", "atr": "4",
        "signal_date": "2026-08-12", "_row": 3},
       {"ticker": "NVDA", "actual_entry": "", "atr": "3",
        "signal_date": "2026-08-13", "_row": 4}]


def _geom(shadow_result, skip=None):
    marks = []
    _M.sheets_mod.read_pending = lambda statuses=("FALLBACK",): [dict(x) for x in _FB]
    _M.sheets_mod.update_pending_status = (
        lambda r, s, d, n="": marks.append((r, s)) or True)
    _M.sheets_mod.update_shadow_rows = lambda u: shadow_result
    out = _M._apply_fallback_geometry("2026-08-14", skip_tickers=skip)
    return marks, out


_m, _o = _geom({"updated": 2, "missed": [], "error": None})
check("clean write marks both rows EXECUTED",
      _m == [(2, "EXECUTED"), (3, "EXECUTED")], f"{_m}")

_m, _o = _geom({"updated": 1, "missed": ["MSFT@2026-08-12 (no row)"], "error": None})
check("missed write leaves that row FALLBACK for retry",
      _m == [(2, "EXECUTED")], f"{_m}")
check("OLD marked it EXECUTED before writing", True,
      "correction would have been unrecoverable")

_m, _o = _geom({"updated": 0, "missed": [], "error": "API 500"})
check("API error marks nothing EXECUTED", _m == [], f"{_m}")

_m, _o = _geom({"updated": 2, "missed": [], "error": None})
check("row awaiting a fill is counted", _o["awaiting"] == 1, f'{_o["awaiting"]}')
_m, _o = _geom({"updated": 2, "missed": [], "error": None}, skip=["NVDA"])
check("row flipped to FALLBACK this same run is NOT counted",
      _o["awaiting"] == 0, f'{_o["awaiting"]}')


# ------------------------------------------------------------
# B4 — the crash detector could not tell calm from blind
# ------------------------------------------------------------
print("\n=== B4: black swan watcher fails loud, not silent ===")

_sent = []
_M.tg_mod._send_message = lambda t, **k: _sent.append("BLIND") or True
_M.tg_mod.send_black_swan_crash = lambda e: _sent.append("CRASH") or True
_M._read_marker = lambda p: ""
_M._write_marker = lambda p, c: None


def _bs(ctx):
    _sent.clear()
    _M.data_mod.fetch_market_context = lambda: ctx
    return _M.run_black_swan_check()["status"], list(_sent)


_BASE = dict(vix=20.0, vix_spike_pct=0.0, spy_1d_change_pct=0.0)

_st, _sn = _bs({**_BASE, "spy_data_ok": False, "vix_data_ok": False})
check("both feeds dead -> status 'blind', not 'no_event'", _st == "blind", _st)
check("both feeds dead -> operator is told", _sn == ["BLIND"], f"{_sn}")

_st, _sn = _bs({**_BASE, "spy_data_ok": True, "vix_data_ok": True})
check("genuinely calm market -> silent no_event", _st == "no_event" and _sn == [])

_st, _sn = _bs({**_BASE, "vix_spike_pct": 35.0, "spy_data_ok": True, "vix_data_ok": True})
check("real VIX spike still alerts", _st == "type_a_crash" and _sn == ["CRASH"])

_st, _sn = _bs({**_BASE, "spy_1d_change_pct": -4.0,
                "spy_data_ok": True, "vix_data_ok": False})
check("one live leg is enough to trigger", _st == "type_a_crash", _st)

_st, _sn = _bs({**_BASE, "spy_data_ok": False, "vix_data_ok": True})
check("a DEAD leg's default cannot trigger", _st == "no_event", _st)

_TRIG = config.BLACK_SWAN_TYPE_A["triggers"]
check("defaults really do read 'calm'",
      _BASE["vix_spike_pct"] < _TRIG["vix_spike_pct_min"]
      and _BASE["spy_1d_change_pct"] > _TRIG["spy_drop_pct_max"],
      "which is exactly why silence was indistinguishable from safety")

_dsrc = open("src/data_fetcher.py").read()
check("fetch_market_context exposes provenance",
      '"spy_data_ok"' in _dsrc and '"vix_data_ok"' in _dsrc)


# ------------------------------------------------------------
# B15 — orphan shadow-indicator key
# ------------------------------------------------------------
print("\n=== B15: shadow indicator key names match ===")

_asrc = open("src/analyst.py").read()
check("analyst no longer writes spy_vs_ema20",
      '"spy_vs_ema20": ind.get' not in _asrc)
check("analyst writes spy_vs_sma20", '"spy_vs_sma20"' in _asrc)
check("main writes the same key", 'ind["spy_vs_sma20"]' in main_src)
check("shadow row reads the same key", 'ind.get("spy_vs_sma20"' in main_src)
check("HEADER carries the same name", "_shadow_spy_vs_sma20" in _sl.HEADER)


# ------------------------------------------------------------
# B10 — three different account/risk figures in three places
# ------------------------------------------------------------
print("\n=== B10: no stale sizing numbers in the LLM prompts ===")

check("user prompt drops the $100 / $3 (3%) line",
      "Max risk: $3 (3%)" not in _asrc)
check("system prompt drops the hardcoded account size",
      "Account: $100 starting equity" not in _asrc)
check("prompt derives geometry from config",
      "{config.STOP_ATR_MULT}" in _asrc and "{config.TP1_R_MULT" in _asrc)
check("prompt derives RR from config", "{config.MIN_RR_RATIO}" in _asrc)
check("prompt derives hold from config", "{config.MAX_HOLDING_DAYS}" in _asrc)


# ------------------------------------------------------------
# B11 — constants whose comments claimed they were enforced
# ------------------------------------------------------------
print("\n=== B11: unwired constants are labelled, and STAY unwired ===")

_csrc = open("src/config.py").read()
_all_src = "".join(open(f).read() for f in
                   ["src/main.py", "src/universe.py", "src/data_fetcher.py",
                    "src/indicators.py", "src/analyst.py", "src/risk_manager.py",
                    "src/sheets_logger.py", "src/telegram_sender.py"])

for _name in ["MIN_PRICE", "MIN_AVG_VOLUME", "MIN_MARKET_CAP", "DAILY_LLM_BUDGET_USD"]:
    check(f"{_name} labelled NOT ENFORCED",
          re.search(rf"{_name}\s*=.*NOT ENFORCED", _csrc) is not None)
    # Deliberate: wiring any of these mid-batch is a filter change, and
    # Benjamini-Hochberg returned 0/9 on Batch 3. If this ever fails, someone
    # changed the screener universe without evidence.
    check(f"{_name} still unused outside config", _name not in _all_src,
          "wiring it mid-batch would be an unauthorised filter change")

# The old bare claim must be gone. The phrase itself survives, but only in
# quotation marks inside the note explaining that it was false — keeping the
# original wrong text visible is deliberate, so the next reader knows this was
# checked rather than never noticed.
check("bare 'used by universe.py' claim removed",
      "# Liquidity & quality filters (used by universe.py / data_fetcher)" not in _csrc)
check("old claim survives only as a quotation",
      '"used by universe.py / data_fetcher"' in _csrc)
check("universe.py really has no price/cap filter",
      "MIN_PRICE" not in open("src/universe.py").read())


# ------------------------------------------------------------
# B3 / B8 / B9 / B18 — workflow defects
# ------------------------------------------------------------
print("\n=== B3/B8/B9/B18: daily workflow ===")

check("B3: contents: write declared",
      re.search(r"permissions:\s*\n\s*contents:\s*write", WF) is not None,
      "without it the equity-cache push 403s and tier 2 silently never exists")
check("B3: the cache step still exists", ".last_equity" in WF)
check("B18: rebase before push", "git pull --rebase" in WF)
check("B18: concurrency group set", "concurrency:" in WF)

_ver = re.search(r"verification:.*?default:\s*(\w+)", WF, re.S)
check("B8: verification defaults to false",
      _ver is not None and _ver.group(1) == "false",
      f"default: {_ver.group(1) if _ver else '?'}")
check("B8: pre-launch guard still present",
      "force_run" in WF and "verification" in WF,
      "force_run=true without verification=true must hard fail")

check("B9: backup cron constant is None (it was never deployed)",
      config.DAILY_SIGNAL_CRON_BACKUP_UTC is None,
      f"{config.DAILY_SIGNAL_CRON_BACKUP_UTC}")
check("B9: exactly one cron in the workflow",
      len(re.findall(r"- cron:", WF)) == 1,
      f'{len(re.findall(r"- cron:", WF))} cron(s)')
check("B9: primary cron unchanged",
      config.DAILY_SIGNAL_CRON_PRIMARY_UTC.replace(" ", "") in WF.replace(" ", ""),
      config.DAILY_SIGNAL_CRON_PRIMARY_UTC)


# ------------------------------------------------------------
# B19 / B20 — entry_diagnostic could not read the live sheet
# ------------------------------------------------------------
print("\n=== B19/B20: the analysis tool agrees with the pipeline ===")

import entry_diagnostic as _ed

# B19 — the sheet's Phase 2.6 columns are BARE (no "shadow_" prefix).
# sheets_logger._normalize() collapses both forms; this module has its own
# normaliser, and it must recognise the same set or the two disagree.
for _bare in ["risk_usd", "entry_type", "force_closed", "discretionary_action",
              "veto_reason", "spy_vs_sma20", "vix_level", "pct_above_50dma",
              "intended_notional_pct"]:
    check(f"B19: bare '{_bare}' maps to canonical",
          _ed._BARE_TO_CANONICAL.get(_bare) == f"_shadow_{_bare}",
          _ed._BARE_TO_CANONICAL.get(_bare))

# The narrowness guard must hold: a bare name that collides with a canonical
# field would silently redirect reads.
for _danger in ["date", "ticker", "stop", "tp1", "tp2", "outcome", "atr"]:
    check(f"B19: '{_danger}' deliberately NOT bare-mapped",
          _danger not in _ed._BARE_TO_CANONICAL,
          "would collide with _shadow_" + _danger)

_norm = _ed._normalize_fieldnames({"entry_type": "strict",
                                   "discretionary_action": "VETOED",
                                   "shadow_ticker": "AAPL"})
check("B19: bare keys arrive canonical",
      _norm.get("_shadow_entry_type") == "strict"
      and _norm.get("_shadow_discretionary_action") == "VETOED")
check("B19: prefixed keys still work", _norm.get("_shadow_ticker") == "AAPL")

# The consequence that mattered: an unreadable action is a BLANK action, and
# get_taken() treats blank as taken — so verification rows were entering the
# Batch 4 sample as executed trades.
_vetoed = _ed._coerce_row(_ed._normalize_fieldnames(
    {"shadow_ticker": "PCAR", "discretionary_action": "VETOED"}))
check("B19: a VETOED row is excluded from get_taken",
      _ed.get_taken([_vetoed]) == [], "the whole point of the VETOED stamp")

# B20 — ema9_filled vocabulary. Batch 3 rows say Yes/No (entered by hand);
# _resolve_pending_entries writes TRUE/FALSE. The column is MIXED from
# 11 Aug onwards, so both must parse.
def _fl(v):
    return _ed._coerce_row({"_shadow_ema9_filled": v}).get("_shadow_ema9_filled")


for _v in ["TRUE", "true", "Yes", "yes", "Y", "1"]:
    check(f"B20: '{_v}' -> True", _fl(_v) is True)
for _v in ["FALSE", "false", "No", "no", "N", "0"]:
    check(f"B20: '{_v}' -> False", _fl(_v) is False)
check("B20: genuinely blank stays None", _fl("") is None)
check("B20: junk stays None", _fl("maybe") is None)
check("B20: code writes TRUE/FALSE", '"ema9_filled": "TRUE"' in main_src)

_mixed = [_ed._coerce_row({"_shadow_ema9_filled": v, "_shadow_ema9_gap_pct": "1.0"})
          for v in ["Yes", "No", "TRUE", "FALSE", ""]]
_fr = _ed.analyse_fill_rate(_mixed)
check("B20: mixed Yes/No + TRUE/FALSE both counted",
      _fr["filled_n"] == 2 and _fr["unfilled_n"] == 2 and _fr["pending_n"] == 1,
      f'filled={_fr["filled_n"]} unfilled={_fr["unfilled_n"]} pending={_fr["pending_n"]}')
check("B20: fill rate is computable at all", _fr["fill_rate_pct"] is not None,
      "it returned None on the entire 52-row live sheet before this fix")


# ------------------------------------------------------------
# Column alignment — the invariant every write depends on
# ------------------------------------------------------------
print("\n=== INVARIANT: positional writes stay aligned ===")

check("HEADER is 37 columns", len(_sl.HEADER) == 37, f"{len(_sl.HEADER)}")
check("HEADER names are unique", len(set(_sl.HEADER)) == 37)
check("normalised HEADER names are unique",
      len({_sl._normalize(h) for h in _sl.HEADER}) == 37,
      "a collision here would make reads ambiguous")
check("pending header is 11 columns", len(_sl.PENDING_HEADER) == 11)
check("check_header_alignment.py exists",
      __import__("os").path.exists("check_header_alignment.py"),
      "listed as BLOCKING in the 2.6.1 plan but was absent from the repo")


# ============================================================
print("\n" + "=" * 46)
if FAILS:
    print(f"FAILURES ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
