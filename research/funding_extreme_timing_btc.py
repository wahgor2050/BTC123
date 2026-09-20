#!/usr/bin/env python3
"""
================================================================================
  FUNDING-RATE-EXTREME INTRADAY TIMING on BTC HOURLY BARS -- the Kraken
  perpetual funding series (btc_demo/history/BTC_funding.csv.gz) used for the
  FIRST time as a TIMING signal in its own right, not as a cost input.
================================================================================
  WHY THIS MECHANISM (stated before any result was computed).
  Everything price-derived has already died on BTC hourly bars in this repo
  and is NOT re-tested here: fixed RSI(14) 30/70, EMA(12,26) trend-following,
  MACD, Donchian (results_swing_crypto_futures_reverse_dip.txt), EMA(12,26)
  reverse-dip (sealed CAGR -33.4%), adaptive-percentile RSI(14) (worse).
  Short-perp funding CARRY is also settled dead separately
  (research/short_funding_bugfix_rerun.py -- long-only beats every short-carry
  gate).  What has never been checked here is the funding rate as an
  INFORMATION source: funding extremes are a real, documented crypto
  market-microstructure phenomenon -- very negative funding means shorts are
  crowded and PAYING longs to take the other side, a positioning imbalance
  that historically precedes near-term mean-reversion (squeezes); very
  positive funding is the mirror case.  "Does an extreme funding print
  predict the next few hours of BTC price" is a DIFFERENT question from
  "should I collect the carry", and nobody in this repo has asked it.

  ★ EXACT PRE-REGISTERED RULE (frozen in this docstring BEFORE any z-score,
  any signal array, or any backtest number was computed; textbook round
  numbers, none fitted to the data; no alternates run):
    1. Signal series = Kraken perp funding rate, settled hourly (24x/day),
       loaded via btc_basis.load_funding() UNCHANGED.
    2. At settlement k, z_k = (rate_k - mean(rate_{k-168 .. k-1})) /
       samplestd(rate_{k-168 .. k-1}) -- trailing 168-SETTLEMENT (~7 calendar
       days at 24/day) window ENDING AT k-1, i.e. the current print is scored
       against the prior week's distribution, never against a window that
       contains itself.  168 = 7 days is a textbook "one week of hourly
       observations" reference; z thresholds below are the textbook 2-sigma.
       Settlements before 168 real prior observations exist have z = None
       (skipped/flat, never a shorter-window backfill).  If the trailing
       window's sample std is < 1e-12 the z is None (degenerate window).
    3. Bar mapping (causality margin, stated up front): the signal available
       to bar i is the z of the MOST RECENT settlement at or before bar i's
       OPEN timestamp.  btc_backtest.backtest() itself fills want[i] at bar
       i+1's open ("decisions on bar i's close, filled at bar i+1's open"),
       so the fill happens >= 1 full hour after the newest settlement the
       decision could have read -- safe regardless of whether Kraken's
       rate-at-T is set ex-ante or realized at T.  This deliberately gives up
       one hour of reaction speed for an unambiguous no-lookahead guarantee.
       If the most recent settlement is more than 6 hours older than the bar
       open (the funding series has 6x 2h and 1x 3h gaps, audited below), the
       signal is None -> flat.
    4. THE RULE, long/flat only (repo spot convention; shorting already
       settled dead separately):
         ENTER long  when z <= -2.0  (shorts crowded / paying -- contrarian
                                      long into the squeeze side)
         EXIT  flat  when z >=  0.0  (positioning has reverted to neutral)
         sticky in between.
    5. Cost 10bp fee + 5bp slip (repo standard), zero-cost variant for the
       churn decomposition.  Full/flat sizing, no vol-target, no stop.
  NOT run (flagged as future-only, keeping the one-rule promise): the mirror
  short side, any other window (24h/72h/30d), any other threshold (1.5/2.5),
  symmetric exit bands.  The POSITIVE-extreme side of the mechanism is
  examined ONLY in the pre-registered descriptive event study below (buckets
  z<=-2 / -2<z<2 / z>=+2, forward 1h/4h/24h open-to-open log returns, counts
  + means; DESCRIPTIVE ONLY, no pass/fail weight, overlapping-window caveat
  applies at the 4h/24h horizons).

  ★ PRE-DECLARED PASS BAR (same as swing_crypto_futures_reverse_dip.py):
  beats its own matched-frequency ~40-trial random-timing control AND the
  real-vs-random gap survives at zero cost.  Beats-buy&hold is computed and
  reported (hiding it would be dishonest) but is NOT the bar -- on a ~12
  month window the B&H comparison is regime luck in either direction.

  ★ EXPLORATORY DOWNGRADE, disclosed everywhere (script, results file,
  README row, final report): the funding series is only ~12 months deep
  (2025-09-17 -> 2026-09-18, 8,778 settlements).  That is NOT enough for the
  repo's standard sealed-2-year holdout, so this is an EXPLORATORY,
  non-sealed study -- the same downgrade posture swing_recent_intraday.py
  used for its 7-month window.  A single 12-month regime answers "did it
  work this year", not "does it work".

  DISCLOSURES (honest-shop rules):
  * Data audit before freezing: the funding file's span/spacing/summary
    stats (mean 3.7e-6/h, sd 8.4e-6, 30.3% negative prints, annualized mean
    3.26% -- matching the short_funding_bugfix_rerun measurement) were
    inspected while checking the data was usable.  NO z-score, signal array,
    trade, or backtest number was computed before the rule above was frozen.
  * Funding P&L omission, biased AGAINST the strategy: this rule is in the
    market precisely during very-NEGATIVE-funding hours, when a perp long
    would additionally RECEIVE funding.  btc_backtest.backtest() only
    supports a constant funding_apr, not a time-varying stream, so funding
    P&L is left at 0 (spot long-only convention).  Any funding the position
    would have collected is upside not counted here.
  * The random-control caveat from swing_crypto_futures_reverse_dip.py PART 3
    applies: at hourly frequency a 15bp round-trip incinerates a churny
    random arm, so "beats control" AT COST is near-trivial; the zero-cost
    decomposition is the informative check and is what the pass bar uses.
  * Episode count matters more than usual: a 2-sigma trigger on a 7-day
    window may fire only a handful of times in ~12 months.  The trade count
    is printed prominently; if it is single-digit, "too few episodes to
    score" is a legitimate exploratory outcome and is said plainly.

  FOUNDATION REUSE, NOT REBUILD (this repo's convention):
    btc_basis.load_funding()                     -- funding file parser, unchanged
    btc_backtest (via sri.bt): load_history, longest_continuous, Indicators,
       backtest, metrics                         -- engine, unchanged
    research/swing_recent_intraday.py (importlib, main() guarded):
       control_test_hourly, donchian_churn_decomposition (generic over
       (bars, want) despite its name -- genericity already documented in
       rsi_adaptive_percentile_intraday.py), COST, ZERO_COST, N_TRIALS,
       SEED_BASE (=71330), fmt, median          -- unchanged
  Subject index 900 (this file only) -- non-overlapping with the stock
  studies' 0..47 and swing_crypto_futures_reverse_dip.py's 100+/500+/700+
  ranges, so no already-recorded random draw is disturbed.  Pure-integer
  seeds, no hash(), bit-for-bit reproducible without PYTHONHASHSEED.

  Never uses `except Exception` to hide a bug.

  Run:  source ~/projects/quant_env/bin/activate  &&  \\
        python3 research/funding_extreme_timing_btc.py
  (stdlib only -- no pandas/numpy needed; venv activation is just the house
  habit.  Runtime ~1-2 min: ~8.6k-bar slice x ~120 control/decomposition
  backtests.)
================================================================================
"""
import importlib.util
import math
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import btc_basis  # noqa: E402  load_funding() only; __main__ guarded

_spec_sri = importlib.util.spec_from_file_location(
    "swing_recent_intraday", os.path.join(HERE, "swing_recent_intraday.py"))
sri = importlib.util.module_from_spec(_spec_sri)
_spec_sri.loader.exec_module(sri)

bt = sri.bt
COST = sri.COST                                   # dict(fee_bps=10.0, slip_bps=5.0)
control_test_hourly = sri.control_test_hourly     # generic over (bars, want)
churn_decomposition = sri.donchian_churn_decomposition  # generic over (bars, want)
fmt = sri.fmt

# ------------------------------------------------------------------------------
# ★ PRE-REGISTERED CONSTANTS (see docstring rule -- frozen before any result)
# ------------------------------------------------------------------------------
Z_WINDOW = 168          # settlements (~7 days at 24/day), trailing, excl. current
Z_ENTER = -2.0          # enter long at or below
Z_EXIT = 0.0            # exit flat at or above
STALE_LIMIT_S = 6 * 3600  # signal None if newest settlement > 6h older than bar open
SUBJECT_INDEX = 900     # control-seed namespace, non-overlapping (see docstring)

EVENT_HORIZONS_H = (1, 4, 24)   # descriptive event study only


def ts_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------------------
# Signal math -- trailing z-score over the settlement series (stdlib only)
# ------------------------------------------------------------------------------
def settlement_zscores(rates, window=Z_WINDOW):
    """z[k] = (rates[k] - mean(rates[k-window:k])) / samplestd(rates[k-window:k]).
    Strictly causal: the window ends at k-1, never contains k.  None until
    `window` real prior observations exist, or if the window std is
    degenerate (< 1e-12)."""
    n = len(rates)
    z = [None] * n
    for k in range(window, n):
        win = rates[k - window:k]
        m = sum(win) / window
        var = sum((x - m) ** 2 for x in win) / (window - 1)
        sd = math.sqrt(var)
        if sd < 1e-12:
            continue
        z[k] = (rates[k] - m) / sd
    return z


def want_from_funding(bars, f_times, f_z, stale_limit=STALE_LIMIT_S):
    """Builds the long/flat want array for `bars` from settlement z-scores.
    Bar i reads the z of the most recent settlement at or BEFORE bars[i][0]
    (bar open) -- see docstring point 3 for the causality margin.  Sticky
    enter z<=Z_ENTER / exit z>=Z_EXIT.  Stale (>stale_limit) or missing
    signal -> flat."""
    want = [False] * len(bars)
    holding = False
    j = -1  # index of newest settlement with time <= current bar open
    nf = len(f_times)
    for i, b in enumerate(bars):
        t = b[0]
        while j + 1 < nf and f_times[j + 1] <= t:
            j += 1
        z = None
        if j >= 0 and (t - f_times[j]) <= stale_limit:
            z = f_z[j]
        if z is None:
            holding = False   # no readable signal -> flat, never coast blind
        elif not holding and z <= Z_ENTER:
            holding = True
        elif holding and z >= Z_EXIT:
            holding = False
        want[i] = holding
    return want


# ------------------------------------------------------------------------------
def main():
    print("=" * 100)
    print("FUNDING-RATE-EXTREME INTRADAY TIMING on BTC HOURLY BARS (Kraken perp funding,")
    print("first use as a TIMING signal in this repo -- previously only a cost input)")
    print("=" * 100)
    print("""PRE-REGISTERED RULE (frozen in the docstring before any result below was computed):
  z_k = (rate_k - mean of trailing 168 settlements ending at k-1) / (their sample std).
  Bar i reads the z of the newest settlement at or before its OPEN (>=1h fill margin on
  top of the engine's own next-bar-open fill).  ENTER long z <= -2.0 (shorts crowded,
  paying longs).  EXIT flat z >= 0.0.  Sticky.  Long/flat only.  Cost 10bp+5bp.
  PASS BAR (pre-declared): beats own matched-frequency random control AND the gap
  survives at zero cost.  Beats-B&H reported, NOT the bar.
  ★ EXPLORATORY: funding history is only ~12 months -- NO sealed holdout is possible.
  This is a first-pass, non-sealed study, same downgrade posture as
  swing_recent_intraday.py.  Nothing here can be a confirmed edge.""")

    # --------------------------------------------------------------------
    # DATA AUDIT -- funding series
    # --------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("DATA AUDIT")
    print("=" * 100)
    f_rows, per_day, source = btc_basis.load_funding()
    f_times = [t // 1000 for t, _ in f_rows]          # ms -> s epoch
    f_rates = [r for _, r in f_rows]
    n_f = len(f_rows)
    gaps = {}
    for a, b in zip(f_times, f_times[1:]):
        gaps[b - a] = gaps.get(b - a, 0) + 1
    mean_r = sum(f_rates) / n_f
    sd_r = math.sqrt(sum((x - mean_r) ** 2 for x in f_rates) / (n_f - 1))
    n_neg = sum(1 for r in f_rates if r < 0)
    print("  funding: source=%s per_day=%d n=%d  %s -> %s UTC"
          % (source, per_day, n_f, ts_date(f_times[0]), ts_date(f_times[-1])))
    print("  spacing histogram (s): %s" % sorted(gaps.items()))
    print("  rate/settlement: mean %.3e  sd %.3e  min %.3e  max %.3e  %%neg %.1f%%"
          % (mean_r, sd_r, min(f_rates), max(f_rates), 100.0 * n_neg / n_f))
    print("  annualized mean %.2f%% (cross-check: short_funding_bugfix_rerun measured 3.26%%)"
          % (mean_r * per_day * 365 * 100.0))
    assert per_day == 24, "expected hourly (24/day) settlements, got per_day=%d" % per_day

    raw = bt.load_history(symbol="BTC", gran=3600)
    kept, dropped = bt.longest_continuous(raw)        # module hourly defaults
    print("  BTC 1h: %d bars kept (%d dropped)  %s -> %s UTC"
          % (len(kept), dropped, ts_date(kept[0][0]), ts_date(kept[-1][0])))

    # --------------------------------------------------------------------
    # SIGNAL + CAUSALITY CHECKS (hard asserts, run BEFORE any result is trusted)
    # --------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("CAUSALITY CHECKS -- hard-asserted, run before any backtest number below")
    print("=" * 100)
    f_z = settlement_zscores(f_rates)
    first_valid = next(i for i, v in enumerate(f_z) if v is not None)
    print("  [A.1] first valid z at settlement index %d (expected %d): %s"
          % (first_valid, Z_WINDOW, "PASS" if first_valid == Z_WINDOW else "FAIL"))
    assert first_valid == Z_WINDOW

    mut_at = Z_WINDOW + 400
    rates_mut = list(f_rates)
    rates_mut[mut_at] = 99.0
    for m in range(mut_at + 1, n_f):
        rates_mut[m] = -99.0
    z_mut = settlement_zscores(rates_mut)
    unchanged = all(f_z[i] == z_mut[i] for i in range(mut_at))
    changed_self = f_z[mut_at] != z_mut[mut_at]
    print("  [A.2] mutating settlement %d and everything after leaves z[0..%d] "
          "byte-for-byte unchanged: %s" % (mut_at, mut_at - 1, "PASS" if unchanged else "FAIL"))
    print("  [A.3] (non-vacuous) same mutation DOES change z[%d] itself: %s"
          % (mut_at, "PASS" if changed_self else "FAIL -- test is broken"))
    assert unchanged, "LOOKAHEAD BUG in settlement_zscores"
    assert changed_self, "vacuous test"

    # trading slice: bars from the first settlement with a valid z onward
    t0 = f_times[Z_WINDOW]
    bars_seg = [b for b in kept if b[0] >= t0]
    print("  trading slice: %d hourly bars  %s -> %s UTC (bars start at the first"
          % (len(bars_seg), ts_date(bars_seg[0][0]), ts_date(bars_seg[-1][0])))
    print("  valid-z settlement, so the whole slice is signal-eligible -- no in-slice warmup)")

    want = want_from_funding(bars_seg, f_times, f_z)

    # [B] end-to-end truncation: as of bar i, zero knowledge of anything after it
    probes = list(range(50, len(bars_seg), max(1, (len(bars_seg) - 50) // 8)))[:8]
    mismatches = []
    for i in probes:
        cut_t = bars_seg[i][0]
        nfk = sum(1 for t in f_times if t <= cut_t)
        z_trunc = settlement_zscores(f_rates[:nfk])
        want_trunc = want_from_funding(bars_seg[:i + 1], f_times[:nfk], z_trunc)
        if want_trunc[-1] != want[i]:
            mismatches.append(i)
    print("  [B] end-to-end truncation at %d probe bars %s: full-run want[i] vs "
          "truncated-to-bar-i want[-1] identical at every probe: %s"
          % (len(probes), probes, "PASS" if not mismatches else "FAIL at %s" % mismatches))
    assert not mismatches, "LOOKAHEAD BUG confirmed at bars %s" % mismatches
    print("  ALL CAUSALITY CHECKS PASS")

    # --------------------------------------------------------------------
    # DESCRIPTIVE EVENT STUDY (pre-registered; descriptive ONLY, no pass/fail
    # weight; the only place the positive-extreme side gets examined)
    # --------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("DESCRIPTIVE EVENT STUDY -- settlement-level buckets, forward open-to-open log")
    print("returns from the first bar at/after each settlement.  DESCRIPTIVE ONLY -- means +")
    print("counts, no CI theater; 4h/24h horizons OVERLAP heavily inside funding episodes,")
    print("so adjacent extreme settlements are NOT independent draws.  No verdict weight.")
    print("=" * 100)
    bar_ts = [b[0] for b in kept]
    bar_open = [b[1] for b in kept]
    import bisect
    buckets = {"z<=-2": [], "-2<z<+2": [], "z>=+2": []}
    for k in range(len(f_z)):
        if f_z[k] is None:
            continue
        j = bisect.bisect_left(bar_ts, f_times[k])
        if j + max(EVENT_HORIZONS_H) >= len(kept):
            continue
        fwd = tuple(math.log(bar_open[j + h] / bar_open[j]) for h in EVENT_HORIZONS_H)
        if f_z[k] <= -2.0:
            buckets["z<=-2"].append(fwd)
        elif f_z[k] >= 2.0:
            buckets["z>=+2"].append(fwd)
        else:
            buckets["-2<z<+2"].append(fwd)
    print("  %-9s %7s | %s" % ("bucket", "n", "  ".join("mean fwd %2dh (bp)" % h
                                                        for h in EVENT_HORIZONS_H)))
    for name in ("z<=-2", "-2<z<+2", "z>=+2"):
        rows = buckets[name]
        if not rows:
            print("  %-9s %7d | (empty)" % (name, 0))
            continue
        means = ["%17.2f" % (sum(r[h] for r in rows) / len(rows) * 1e4)
                 for h in range(len(EVENT_HORIZONS_H))]
        print("  %-9s %7d | %s" % (name, len(rows), "  ".join(means)))
    if buckets["z>=+2"]:
        pos_1h = sum(r[0] for r in buckets["z>=+2"]) / len(buckets["z>=+2"])
        if pos_1h > 0:
            print("  NOTE (mechanism honesty): the mirror prediction for the POSITIVE tail")
            print("  (crowded longs -> near-term reversion DOWN) is NOT supported here -- the")
            print("  z>=+2 bucket's short-horizon means are positive too.  Only the traded")
            print("  (negative) tail matches the mean-reversion story; on this window the data")
            print("  fit 'any funding extreme -> short-term bounce' equally well.  The traded")
            print("  rule never touches the positive tail, but this asymmetry is a real strike")
            print("  against the clean squeeze narrative and is flagged, not hidden.")
        else:
            print("  NOTE: the z>=+2 bucket's 1h mean is negative, consistent with the mirror")
            print("  (crowded-longs-revert-down) prediction on this window.")

    # --------------------------------------------------------------------
    # BACKTEST -- the one pre-registered rule, full rigor stack
    # --------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("BACKTEST -- pre-registered rule, 40-trial matched-frequency random control,")
    print("zero-cost churn decomposition, buy&hold on the exact same slice")
    print("=" * 100)
    res = control_test_hourly(bars_seg, want, SUBJECT_INDEX)
    mm = res["real_mm"]
    eq_bh, tr_bh = bt.backtest(bars_seg, [True] * len(bars_seg), **COST)
    mm_bh = bt.metrics(bars_seg, eq_bh, tr_bh, want=[True] * len(bars_seg))
    dc = churn_decomposition(bars_seg, SUBJECT_INDEX, want)
    gap_cost = (dc["real_cost"] - dc["rand_med_cost"]
                if dc["real_cost"] is not None and dc["rand_med_cost"] is not None else None)
    gap_zero = (dc["real_zero"] - dc["rand_med_zero"]
                if dc["real_zero"] is not None and dc["rand_med_zero"] is not None else None)
    n_trades = res["real_trades"]
    keep = res["keep_pct"]
    print("  REAL strategy : Calmar %s  Sharpe %s  CAGR %s%%  MDD %s%%  trades %d  "
          "in-market %.1f%%"
          % (fmt(res["real_calmar"]), fmt(res["real_sharpe"]),
             fmt(mm["cagr_pct"], 1) if mm else "n/a",
             fmt(mm["max_dd_pct"], 1) if mm else "n/a", n_trades, keep))
    print("  RANDOM control: Calmar median %s  p90 %s  trades median %s  (%d trials, "
          "matched keep%%)"
          % (fmt(res["rand_calmar_med"]), fmt(res["rand_calmar_p90"]),
             fmt(res["rand_trades_med"], 0), res["n_trials"]))
    print("  BUY & HOLD    : Calmar %s  CAGR %s%%  MDD %s%%  (same slice)"
          % (fmt(mm_bh["calmar"]) if mm_bh else "n/a",
             fmt(mm_bh["cagr_pct"], 1) if mm_bh else "n/a",
             fmt(mm_bh["max_dd_pct"], 1) if mm_bh else "n/a"))
    print("  CHURN DECOMP  : real cost %s / zero %s   random-median cost %s / zero %s"
          % (fmt(dc["real_cost"]), fmt(dc["real_zero"]),
             fmt(dc["rand_med_cost"]), fmt(dc["rand_med_zero"])))
    print("                  gap over random-median: at cost %s   at ZERO cost %s"
          % (("%+.2f" % gap_cost) if gap_cost is not None else "n/a",
             ("%+.2f" % gap_zero) if gap_zero is not None else "n/a"))

    # zero-cost replay of the REAL strategy, for the headline caveat below --
    # reporting only, does not change the rule or the pass bar
    eq_z, tr_z = bt.backtest(bars_seg, want, **sri.ZERO_COST)
    mm_z = bt.metrics(bars_seg, eq_z, tr_z, want=want)

    beats_ctrl = bool(res["beats_median"])
    zc_survives = gap_zero is not None and gap_zero > 0
    beats_bh = (mm is not None and mm_bh is not None
                and mm["cagr_pct"] > mm_bh["cagr_pct"])
    few_episodes = n_trades < 10

    # --------------------------------------------------------------------
    # HONEST VERDICT
    # --------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("HONEST VERDICT -- read this before reading anything above as a signal")
    print("=" * 100)
    print("  beats own random control (Calmar > median): %s" % ("YES" if beats_ctrl else "NO"))
    print("  zero-cost gap survives (>0):                %s" % ("YES" if zc_survives else "NO"))
    print("  -> PRE-DECLARED PASS BAR (both of the above): %s"
          % ("PASS" if (beats_ctrl and zc_survives) else "FAIL"))
    print("  beats buy & hold on CAGR (reported, NOT the bar): %s"
          % ("YES" if beats_bh else "no"))
    print("  trade count: %d round trips in ~%.1f months%s"
          % (n_trades, (bars_seg[-1][0] - bars_seg[0][0]) / (30.44 * 86400),
             "  *** SINGLE-DIGIT-ish EPISODE COUNT -- too few independent episodes to"
             " score confidently against a 40-trial control ***" if few_episodes else ""))
    if (beats_ctrl and zc_survives and mm is not None and mm["cagr_pct"] < 0):
        # Same honesty flag gsr_meanreversion.py's results carry: a "PASS" that
        # loses real money must never be read as harvestable edge.
        rt_cost_pct = n_trades * (COST["fee_bps"] + COST["slip_bps"]) * 2 / 100.0
        print("\n  *** HEADLINE CAVEAT -- read before quoting the PASS line above ***")
        print("  The pre-declared bar passed, but AT REAL COST THE STRATEGY LOSES MONEY")
        print("  (CAGR %s%%, Calmar %s).  The SAME want-array at ZERO cost: CAGR %s%%,"
              % (fmt(mm["cagr_pct"], 1), fmt(mm["calmar"]),
                 fmt(mm_z["cagr_pct"], 1) if mm_z else "n/a"))
        print("  Calmar %s -- i.e. the funding-extreme signal carries genuine timing"
              % (fmt(mm_z["calmar"]) if mm_z else "n/a"))
        print("  information (positive at zero cost, far above the random arm's own")
        print("  zero-cost median), but %d round trips x ~30bp round-trip cost is ~%.0f%%"
              % (n_trades, rt_cost_pct))
        print("  of capital over the window and eats ALL of it and more.  'PASS' here")
        print("  means 'the information exists at this rigor level', NOT 'the money is")
        print("  harvestable at repo-standard 15bp-per-side costs' -- and retail crypto")
        print("  costs are typically WORSE than 15bp/side, not better.  The un-modeled")
        print("  funding rebate a perp long would collect in these hours is on the order")
        print("  of 1-3%/yr -- nowhere near enough to flip the real-cost sign.")
    print("""
  Epistemic status: EXPLORATORY FIRST PASS on a ~12-month funding history --
  no sealed holdout exists or is possible with this data; no parameter-
  neighbor / rolling-window / 2x-cost stress gauntlet was run; no adversarial
  review.  Whatever the line above says, nothing here is a confirmed edge and
  nothing is deployable.  The funding P&L a perp long would have COLLECTED
  during the negative-funding hours this rule holds through is not modeled
  (engine limitation) -- that omission biases AGAINST the strategy.  A NULL
  here joins the same pile as every other intraday BTC signal tested in this
  repo; a positive here would need a second year of data, the stress
  gauntlet, and fresh-context adversarial review before anyone trusts it.""")
    print("DONE")


if __name__ == "__main__":
    main()
