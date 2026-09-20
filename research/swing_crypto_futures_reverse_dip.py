#!/usr/bin/env python3
"""
================================================================================
  EMA(12,26) REVERSE-DIP  +  ADAPTIVE-PERCENTILE RSI(14)  -- the only two
  signals that survived the stock swing campaign's control/churn gauntlet --
  applied to TWO universes this exact mechanism has never touched:
    PART 1: 37 real cryptocurrencies, daily bars (btc_demo/history/*_1d.csv.gz)
    PART 2: 11 CME micro futures, daily bars (futures_lab.py's own UNIVERSE)
    PART 3 (bonus): BTC/ETH/ADA/DOGE hourly bars, the intraday variant
================================================================================
  WHY THESE TWO SIGNALS (context, already established, not re-litigated).
  Across the whole stock swing campaign (research/stock_swing_rsi_ema.py ->
  stock_swing_pit_roster.py -> ema_reverse_dip_pit.py ->
  rsi_adaptive_percentile_pit.py -> swing_recent_intraday.py ->
  rsi_adaptive_percentile_intraday.py), six methods were tested and only two
  ever beat a matched-frequency ~40-trial random-timing control AND kept that
  edge at zero cost (the "zero-cost churn decomposition" check that killed
  RSI-fixed/MACD/EMA-trend/Donchian repeatedly):
    1. EMA(12,26) REVERSE-DIP -- long while EMA(12) < EMA(26): buy when the
       fast average dips below the slow one, flat again when it recovers
       above ("buy the dip, sell the tip" -- the mirror image of textbook
       trend-following). Reused here via research/swing_recent_intraday.py's
       strat_ema_cross_reverse(), UNCHANGED.
    2. ADAPTIVE-PERCENTILE RSI(14) -- RSI(14) unchanged; buy when RSI <= the
       20th percentile of its OWN trailing 252-bar (daily, ~1 year)
       distribution ending at bar i-1, exit when RSI >= the trailing 80th
       percentile, sticky in between. The THRESHOLD drifts with the regime;
       the RULE (252 bars, 20/80) never does. Reused here via
       research/rsi_adaptive_percentile_intraday.py's
       strat_rsi14_adaptive_percentile_intraday(), UNCHANGED (generic despite
       its name -- takes {period, lookback, lo_pct, hi_pct}).
  Both still LOST to plain buy-and-hold on the stock basket tested -- this
  project's pre-declared bar is "beats its own matched-frequency random
  control and the edge survives zero cost", NOT "beats buy & hold" (the
  buy-and-hold comparison is still computed and reported per instrument,
  because hiding it would be dishonest, but it is not the pass/fail line).

  ★ PRE-REGISTERED PARAMS (stated before any result below was computed; no
  grid, no neighbors, no alternates run):
    EMA fast/slow = 12/26.  RSI period = 14.  Percentiles = 20/80.
    Daily lookback = 252 bars.  Hourly lookback = the 21-TRADING-DAY
    equivalent via the SAME bars_per_session = n_bars/n_sessions calendar-
    time conversion research/swing_recent_intraday.py's Donchian arm and
    research/rsi_adaptive_percentile_intraday.py already established --
    measured off each coin's own kept hourly bars, arithmetic printed at
    runtime. For 24/7 crypto that lands near round(21*24)=504 bars.
    ★ DISCLOSED UP FRONT: the task brief assumed the crypto hourly cache was
    "~7 months deep like the stock intraday data". It is NOT -- BTC/ETH
    hourly reach back to 2016 (~87k bars), ADA/DOGE ~5 years (~46-48k), so
    the 252-day-equivalent (~6,048 bars) WOULD now fit. It is deliberately
    NOT run: the task pre-registered "same as was done for the 8 stock
    tickers" (the 21-day variant), and running a second lookback after
    seeing the data is exactly the two-variant search this repo forbids.
    The ~1-year semantic is not lost -- PART 1 runs the 252-bar daily rule
    on these same 4 coins. Unlike the stock intraday study, the hourly depth
    here DOES allow a sealed split, so PART 3 gets the same sealed/presealed
    treatment as the daily parts (the stock version skipped it only because
    7 months cannot be split).

  SEALED-HOLDOUT CONVENTION -- futures_lab.sealed()/presealed(), UNCHANGED,
  years=2 for every instrument in all three parts. Warmup is per-ARM, sized
  from each rule's own mechanical requirement (same reasoning
  research/rsi_adaptive_percentile_pit.py's WARMUP ADAPTATION section
  already established, decided before looking at results):
    EMA arm      : warmup=60 (futures_lab's own default -- EMA(26) is warm
                   well inside 60 bars)
    adaptive arm : warmup=period+lookback+54 (=320 daily, mirrors that
                   script's ADAPTIVE_WARMUP=320 formula exactly; hourly uses
                   the same formula with the converted lookback)
  CONSEQUENCE, DISCLOSED: the two arms' "sealed" slices therefore start at
  DIFFERENT bars (the adaptive arm's slice carries ~260 extra warmup bars in
  front, in which the rule is structurally flat). Per-arm slices are used
  and EVERY comparator -- the matched keep% random control, the buy-and-hold
  benchmark, the zero-cost decomposition -- is computed on the EXACT slice
  that arm ran on, never shared across arms. Comparing arm A's strategy to
  arm B's buy&hold would contaminate the verdict with window-length
  difference; that cross-arm comparison is never made here. (futures_lab's
  own sealed() convention measures metrics over the whole slice INCLUDING
  warmup bars; kept unchanged, so the adaptive arm's "sealed" numbers cover
  ~2y + ~260 flat-ish warmup bars. Same on both sides of every comparison.)

  CONTROL TEST -- matched-frequency ~40-trial random-timing gate at the real
  arm's own in-market %, plus zero-cost churn decomposition, reused
  UNCHANGED from research/swing_recent_intraday.py (control_test_hourly()
  and donchian_churn_decomposition() -- both generic over (bars, want)
  despite their names; the latter's genericity is already documented in
  research/rsi_adaptive_percentile_intraday.py's docstring). DISCLOSED
  DEVIATION from the task's "reuse futures_lab's control-test functions":
  futures_lab.control_test() recomputes dual_sma_voltarget INTERNALLY and
  ignores any caller-provided signal -- it structurally cannot test these
  two arms. What IS reused from futures_lab, unchanged: UNIVERSE,
  load_universe() (cache-only here, no re-fetch), prepare_instrument()
  (roll-gap detect + Panama back-adjust), sealed(), presealed().
  Subject indices for control seeds come from a fixed literal enumeration
  (universe list order x signal x segment, bases 100/500/700 for parts
  1/2/3), pure-integer seeds (sri.SEED_BASE=71330), no hash() -- bit-for-bit
  reproducible without PYTHONHASHSEED, and non-overlapping with the stock
  studies' already-recorded subject indices 0..47.

  COST MODEL -- 10bp fee + 5bp slip, this repo's standard, used by EVERY
  prior crypto study here (xsec_run/apply_all/vol_control, the live BTC
  paper bot) and by futures_lab. Kept for comparability. DISCLOSED: retail
  spot crypto taker fees at major venues can run 40-60bp for small accounts,
  so PART 1's real-cost numbers are optimistic for a small retail account;
  the zero-cost decomposition brackets the other side.

  ★ FUTURES ROLL-GAP LANDMINE (read btc_demo/history/DATA_CATALOG.md's
  2026-09-19 incident note before trusting any futures number). This repo's
  own back_adjust() once flattened a REAL -37.6% SIL crash (2026-01-30,
  cross-verified vs SLV -28.5% same day) into a fake "roll splice",
  inflating SIL's computed buy&hold ~3.5x. Everything in PART 2 runs on the
  SAME adjusted series on both sides of every comparison (strategy, control,
  buy&hold), so within-path comparisons are internally consistent -- but:
  (a) NO futures CAGR below is real-world total return (adjusted-series
      arithmetic only, per DATA_CATALOG / results_gld_slv_vs_mgc_sil_audit);
  (b) sharper, mean-reversion-specific flag: a path where a real crash was
      surgically removed makes a DIP-BUYING rule look better than reality --
      the one time buying the dip would have been punished hardest never
      happens on the adjusted series. SIL (and MGC, less severely) verdicts
      are optimistic FOR THESE SPECIFIC RULES, not just for buy&hold math.
  Every flagged roll-gap's date and raw one-day move is printed per
  instrument below so this is auditable, not a black box.

  Never uses `except Exception` to hide a bug.

  Run:  source ~/projects/quant_env/bin/activate  &&  \\
        python3 research/swing_crypto_futures_reverse_dip.py
  (needs pandas/numpy for the rolling-percentile math, same as the other
  adaptive-percentile scripts; ~5-15 min, the hourly BTC/ETH histories are
  ~87k bars x 40 control trials each.)
================================================================================
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# ------------------------------------------------------------------------------
# Reuse research/rsi_adaptive_percentile_intraday.py UNCHANGED (it itself
# imports research/swing_recent_intraday.py the same way -- main() guarded in
# both, so these imports only define functions/constants). See module
# docstring for the exact reuse map.
# ------------------------------------------------------------------------------
_spec_rapi = importlib.util.spec_from_file_location(
    "rsi_adaptive_percentile_intraday",
    os.path.join(HERE, "rsi_adaptive_percentile_intraday.py"))
rapi = importlib.util.module_from_spec(_spec_rapi)
_spec_rapi.loader.exec_module(rapi)

sri = rapi.sri                      # research/swing_recent_intraday.py module
bt = rapi.bt                        # btc_backtest
control_test_hourly = sri.control_test_hourly            # generic over (bars, want)
churn_decomposition = sri.donchian_churn_decomposition   # generic over (bars, want)
strat_ema_reverse = sri.strat_ema_cross_reverse          # EMA(12,26) reverse-dip
strat_adaptive = rapi.strat_rsi14_adaptive_percentile_intraday  # generic adaptive RSI
trailing_percentile_thresholds = rapi.trailing_percentile_thresholds
fmt = sri.fmt
median = sri.median
COST = sri.COST                     # dict(fee_bps=10.0, slip_bps=5.0)

import futures_lab as fl            # noqa: E402  UNIVERSE/load_universe/prepare_instrument/
                                    # sealed/presealed reused unchanged. NOTE: importing this
                                    # sets bt.BARS_PER_DAY=1 module-wide; nothing in this
                                    # script's pipeline (Indicators.ema/rsi, backtest, metrics)
                                    # reads BARS_PER_DAY, verified by reading those functions.

# ------------------------------------------------------------------------------
# Universes -- fixed literal lists (order matters: subject indices for the
# random-control seeds are derived from list position).
# ------------------------------------------------------------------------------
CRYPTO_DAILY = ["AAVE", "ADA", "ALGO", "ATOM", "AVAX", "AXS", "BAT", "BCH", "BTC",
                "CHZ", "COMP", "CRV", "DASH", "DOGE", "DOT", "ENJ", "EOS", "ETC",
                "ETH", "FIL", "GRT", "HBAR", "ICP", "LINK", "LTC", "MANA", "MKR",
                "NEAR", "SAND", "SNX", "SOL", "UNI", "VET", "XLM", "XRP", "XTZ", "ZEC"]
# Deliberately EXCLUDED from the same cache folder (stated, not silent):
#   MSTR (a stock, not a coin), ZC/ZS/ZW (CBOT grain futures that share the
#   folder by ticker-symbol collision -- they belong to oil_food_leadlag.py).
FUTURES = list(fl.UNIVERSE)         # MES MNQ MYM M2K MGC SIL CL HG M6E M6A M6B
CRYPTO_HOURLY = ["BTC", "ETH", "ADA", "DOGE"]
# The cache also holds LTC/LINK/XRP/SOL hourly files (see DATA_CATALOG.md);
# the task pre-registered the bonus on these 4 only -- disclosed, not hidden.

SEALED_YEARS = 2
EMA_WARMUP = 60                     # futures_lab.sealed()'s own default
RSI_PERIOD = 14
DAILY_LOOKBACK = 252
DAILY_ADAPTIVE_PARAMS = {"period": RSI_PERIOD, "lookback": DAILY_LOOKBACK,
                         "lo_pct": 20.0, "hi_pct": 80.0}
DAILY_ADAPTIVE_WARMUP = RSI_PERIOD + DAILY_LOOKBACK + 54   # = 320, same formula as
                                                           # rsi_adaptive_percentile_pit.py

SIGNALS = ["ema12_26_reverse_dip", "rsi14_adaptive_pctl"]
SEGMENTS = ["presealed", "sealed"]
SUBJECT_BASE = {"crypto_daily": 100, "futures": 500, "crypto_hourly": 700}


def subject_index(part, inst_list, inst, signal, segment):
    return (SUBJECT_BASE[part]
            + inst_list.index(inst) * 4
            + SIGNALS.index(signal) * 2
            + SEGMENTS.index(segment))


def ts_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def run_arm(bars_seg, want, sidx):
    """Control test + buy&hold + zero-cost churn decomposition, ALL on the
    exact same slice (see docstring: comparators are never shared across
    arms with different warmups)."""
    res = control_test_hourly(bars_seg, want, sidx)
    eq_bh, tr_bh = bt.backtest(bars_seg, [True] * len(bars_seg), **COST)
    mm_bh = bt.metrics(bars_seg, eq_bh, tr_bh, want=[True] * len(bars_seg))
    dc = churn_decomposition(bars_seg, sidx, want)
    gap_cost = (dc["real_cost"] - dc["rand_med_cost"]
                if dc["real_cost"] is not None and dc["rand_med_cost"] is not None else None)
    gap_zero = (dc["real_zero"] - dc["rand_med_zero"]
                if dc["real_zero"] is not None and dc["rand_med_zero"] is not None else None)
    mm = res["real_mm"]
    beats_bh = (mm is not None and mm_bh is not None
                and mm["cagr_pct"] > mm_bh["cagr_pct"])
    return {
        "mm": mm, "bh": mm_bh, "res": res, "dc": dc,
        "beats_ctrl": bool(res["beats_median"]),
        "beats_bh": bool(beats_bh),
        "gap_cost": gap_cost, "gap_zero": gap_zero,
        "zc_survives": (gap_zero is not None and gap_zero > 0),
        "n_bars": len(bars_seg),
        "start": ts_date(bars_seg[0][0]), "end": ts_date(bars_seg[-1][0]),
    }


HDR = ("%-5s %8s %9s %8s %6s %6s | %8s %5s | %9s %6s | %7s %7s %5s"
       % ("inst", "calmar", "cagr%", "mdd%", "trds", "keep%",
          "rndCmed", "beats", "bh_cagr%", "bt_BH", "gapCost", "gapZero", "zcOK"))


def print_row(inst, a):
    mm = a["mm"]
    print("%-5s %8s %9s %8s %6s %6s | %8s %5s | %9s %6s | %7s %7s %5s" % (
        inst,
        fmt(mm["calmar"]) if mm else "n/a",
        fmt(mm["cagr_pct"], 1) if mm else "n/a",
        fmt(mm["max_dd_pct"], 1) if mm else "n/a",
        a["res"]["real_trades"],
        fmt(a["res"]["keep_pct"], 1),
        fmt(a["res"]["rand_calmar_med"]),
        "YES" if a["beats_ctrl"] else "no",
        fmt(a["bh"]["cagr_pct"], 1) if a["bh"] else "n/a",
        "YES" if a["beats_bh"] else "no",
        ("%+.2f" % a["gap_cost"]) if a["gap_cost"] is not None else "n/a",
        ("%+.2f" % a["gap_zero"]) if a["gap_zero"] is not None else "n/a",
        "YES" if a["zc_survives"] else "no"))


def pooled(rows_by_inst):
    """rows_by_inst: {inst: arm-dict}. Returns pooled summary dict."""
    rows = [(k, v) for k, v in rows_by_inst.items() if v["mm"] is not None]
    calmars = [v["mm"]["calmar"] for _, v in rows]
    n = len(rows_by_inst)
    return {
        "n": n, "n_scored": len(rows),
        "med_calmar": median(calmars) if calmars else None,
        "n_beats_ctrl": sum(1 for _, v in rows if v["beats_ctrl"]),
        "n_beats_bh": sum(1 for _, v in rows if v["beats_bh"]),
        "n_zc": sum(1 for _, v in rows if v["beats_ctrl"] and v["zc_survives"]),
        "worst": min(rows, key=lambda kv: kv[1]["mm"]["calmar"])[0] if rows else None,
        "best": max(rows, key=lambda kv: kv[1]["mm"]["calmar"])[0] if rows else None,
    }


def print_pooled(label, p):
    print("  %s: median Calmar %s | beats control %d/%d | beats-control-AND-"
          "zero-cost-gap-survives %d/%d | beats buy&hold %d/%d | best %s, worst %s"
          % (label, fmt(p["med_calmar"]), p["n_beats_ctrl"], p["n"],
             p["n_zc"], p["n"], p["n_beats_bh"], p["n"], p["best"], p["worst"]))


def run_universe(part, inst_list, all_bars, adaptive_params_by_inst,
                 adaptive_warmup_by_inst):
    """Runs both signals x both segments for one universe. Returns
    {signal: {segment: {inst: arm}}}."""
    out = {s: {g: {} for g in SEGMENTS} for s in SIGNALS}
    for signal in SIGNALS:
        for segment in SEGMENTS:
            print("\n" + "-" * 118)
            print("%s -- %s -- %s  (per-arm slice; every comparator computed on "
                  "this exact slice)" % (part, signal, segment))
            print("-" * 118)
            print(HDR)
            for inst in inst_list:
                bars = all_bars[inst]
                if signal == "ema12_26_reverse_dip":
                    warm = EMA_WARMUP
                else:
                    warm = adaptive_warmup_by_inst[inst]
                if segment == "sealed":
                    bars_seg = fl.sealed(bars, years=SEALED_YEARS, warmup=warm)
                else:
                    bars_seg = fl.presealed(bars, years=SEALED_YEARS)
                if len(bars_seg) < 30:
                    print("%-5s  SKIPPED -- only %d bars in this slice" % (inst, len(bars_seg)))
                    continue
                ind = bt.Indicators(bars_seg)
                if signal == "ema12_26_reverse_dip":
                    want = strat_ema_reverse(ind)
                else:
                    want = strat_adaptive(ind, adaptive_params_by_inst[inst])
                sidx = subject_index(part, inst_list, inst, signal, segment)
                a = run_arm(bars_seg, want, sidx)
                out[signal][segment][inst] = a
                print_row(inst, a)
            wins = [v for v in out[signal][segment].values()]
            if wins:
                spans = sorted(set((v["start"], v["end"]) for v in wins))
                print("  slice windows in this table: %s%s"
                      % ("; ".join("%s->%s" % s for s in spans[:4]),
                         (" ... (%d distinct)" % len(spans)) if len(spans) > 4 else ""))
    return out


# ==============================================================================
def main():
    print("=" * 118)
    print("EMA(12,26) REVERSE-DIP + ADAPTIVE-PERCENTILE RSI(14) on 37 CRYPTOCURRENCIES (daily),")
    print("11 CME MICRO FUTURES (daily), and BTC/ETH/ADA/DOGE (hourly bonus)")
    print("=" * 118)
    print("PRE-REGISTERED RULES (stated before any result below was computed):")
    print("  1. EMA reverse-dip: long while EMA(12) < EMA(26), flat on recovery. Textbook 12/26,")
    print("     unchanged from research/ema_reverse_dip_pit.py / swing_recent_intraday.py.")
    print("  2. Adaptive RSI: buy when RSI(14) <= trailing 252-bar 20th pctl (ending at bar i-1),")
    print("     exit when >= trailing 80th pctl, sticky. Hourly bonus uses the 21-trading-day-")
    print("     equivalent lookback (bars_per_session conversion, arithmetic printed below).")
    print("  Cost 10bp fee + 5bp slip (repo standard -- see docstring for the crypto-retail-fee")
    print("  caveat). Sealed holdout = futures_lab.sealed()/presealed(), years=2, per-arm warmup")
    print("  (EMA 60 / adaptive period+lookback+54). 40-trial matched-frequency random control +")
    print("  zero-cost churn decomposition per instrument, all on the arm's exact slice.")
    print("  PASS BAR (this project's, pre-declared): beats own random control AND the gap")
    print("  survives at zero cost. Beats-buy&hold is reported but is NOT the bar.")

    # --------------------------------------------------------------------
    # CAUSALITY SPOT-CHECK -- the threshold function was already verified in
    # 2 sibling scripts (3-check gauntlet each); re-assert the warmup
    # boundary here at THIS study's lookbacks, cheap and loud.
    # --------------------------------------------------------------------
    rng = np.random.default_rng(20260920)
    for lb in (DAILY_LOOKBACK, 504):
        n_synth = RSI_PERIOD + lb + 40
        synth = [None] * RSI_PERIOD + [float(x) for x in rng.uniform(10, 90, n_synth - RSI_PERIOD)]
        lo, hi = trailing_percentile_thresholds(synth, lb, 20.0, 80.0)
        first = next(i for i, v in enumerate(lo) if v is not None)
        assert first == RSI_PERIOD + lb, (
            "warmup boundary wrong at lookback=%d: first valid %d, expected %d"
            % (lb, first, RSI_PERIOD + lb))
    print("\nCAUSALITY SPOT-CHECK: first valid threshold lands EXACTLY at period+lookback for")
    print("lookback 252 and 504 (full 3-check gauntlet already on record in")
    print("rsi_adaptive_percentile_pit.py / _intraday.py for this same function) -- PASS")

    # ==================================================================
    # PART 1 -- 37 crypto, daily
    # ==================================================================
    print("\n\n" + "=" * 118)
    print("PART 1 -- 37 CRYPTOCURRENCIES, DAILY BARS")
    print("=" * 118)
    print("Load: bt.load_history(gran=86400) + longest_continuous(gran=86400, max_gap_hours=7)")
    print("(the 7-day gap-limit convention research/xsec2.py already uses for these exact files;")
    print("this is what handles XRP's 905-day SEC-delisting hole).")
    print("Excluded from the same folder, stated not silent: MSTR (a stock), ZC/ZS/ZW (grain")
    print("futures, ticker-symbol collision).")

    crypto_bars = {}
    print("\n%-5s %7s %7s %11s %11s %8s | %8s %8s %8s %10s" %
          ("coin", "bars", "dropped", "start", "end", "years",
           "rsi_min", "rsi_max", "%d<30", "bh_mult"))
    fun = {}
    for c in CRYPTO_DAILY:
        raw = bt.load_history(symbol=c, gran=86400)
        kept, dropped = bt.longest_continuous(raw, gran=86400, max_gap_hours=7)
        crypto_bars[c] = kept
        yrs = (kept[-1][0] - kept[0][0]) / (365.0 * 86400)
        ind = bt.Indicators(kept)
        rsi = [v for v in ind.rsi(RSI_PERIOD) if v is not None]
        pct_below30 = 100.0 * sum(1 for v in rsi if v < 30.0) / len(rsi)
        bh_mult = kept[-1][4] / kept[0][4]
        fun[c] = {"rsi_min": min(rsi), "rsi_max": max(rsi),
                  "pct_below30": pct_below30, "bh_mult": bh_mult, "years": yrs}
        print("%-5s %7d %7d %11s %11s %8.1f | %8.1f %8.1f %8.1f %10.2f" %
              (c, len(kept), dropped, ts_date(kept[0][0]), ts_date(kept[-1][0]), yrs,
               fun[c]["rsi_min"], fun[c]["rsi_max"], pct_below30, bh_mult))
    print("  (rsi_min/max + %days<30 computed over each coin's FULL kept history -- the 'does a")
    print("  fixed RSI 30 floor even exist for this asset' diagnostic; bh_mult = raw last/first")
    print("  close, no cost, full kept history -- context only, not a segment benchmark.)")

    crypto_res = run_universe(
        "crypto_daily", CRYPTO_DAILY, crypto_bars,
        {c: DAILY_ADAPTIVE_PARAMS for c in CRYPTO_DAILY},
        {c: DAILY_ADAPTIVE_WARMUP for c in CRYPTO_DAILY})

    # ==================================================================
    # PART 2 -- 11 futures, daily
    # ==================================================================
    print("\n\n" + "=" * 118)
    print("PART 2 -- 11 CME MICRO FUTURES, DAILY BARS (futures_lab UNIVERSE, cache-only load,")
    print("roll-gap back-adjusted via prepare_instrument -- READ THE LANDMINE NOTE BELOW)")
    print("=" * 118)
    raw_universe = fl.load_universe(force=False)
    fut_bars, n_gaps = {}, {}
    print("\nROLL-GAP AUDIT (every flagged splice, date + raw one-day log return -- these moves")
    print("are REMOVED from the adjusted series every strategy/control/B&H below trades on):")
    for k in FUTURES:
        kept = raw_universe[k]
        flagged, log_ret = fl.detect_roll_gaps(kept)
        adj = fl.back_adjust(kept, flagged)
        fut_bars[k] = adj
        n_gaps[k] = sum(flagged)
        gap_strs = ["%s %+.1f%%" % (ts_date(kept[i][0]), (np.expm1(log_ret[i])) * 100.0)
                    for i in range(len(kept)) if flagged[i]]
        print("  %-4s %5d bars %s -> %s | %d flagged: %s"
              % (k, len(adj), ts_date(adj[0][0]), ts_date(adj[-1][0]), n_gaps[k],
                 ("; ".join(gap_strs) if gap_strs else "none")))
    print("\n*** LANDMINE (DATA_CATALOG.md 2026-09-19 incident): the flags above include SIL's")
    print("*** REAL 2026-01-30 crash (~-37.6%, cross-verified vs SLV -28.5% same day) treated as")
    print("*** a roll splice and FLATTENED. Consequences for THIS study specifically:")
    print("***  (a) no futures CAGR below is real-world total return -- adjusted-series")
    print("***      arithmetic only, both sides of every comparison;")
    print("***  (b) sharper, mean-reversion-specific: a dip-buying rule on a path where the")
    print("***      worst real crash was surgically removed never gets punished for buying THE")
    print("***      dip -- SIL (and MGC, less severely) verdicts are OPTIMISTIC for these two")
    print("***      rules, not just for buy&hold math. See results_gld_slv_vs_mgc_sil_audit.txt.")

    fut_res = run_universe(
        "futures", FUTURES, fut_bars,
        {k: DAILY_ADAPTIVE_PARAMS for k in FUTURES},
        {k: DAILY_ADAPTIVE_WARMUP for k in FUTURES})

    # ==================================================================
    # PART 3 -- BTC/ETH/ADA/DOGE hourly bonus
    # ==================================================================
    print("\n\n" + "=" * 118)
    print("PART 3 (BONUS) -- BTC/ETH/ADA/DOGE, HOURLY BARS")
    print("=" * 118)
    print("★ DATA-DEPTH CORRECTION, disclosed up front: the task brief assumed ~7 months of")
    print("hourly depth (like the stock intraday cache). Actual depth is 5-10 YEARS -- so unlike")
    print("the stock intraday studies, a sealed 2y split IS possible here and is applied. The")
    print("252-day-equivalent lookback (~6,048 bars) would also now fit but is deliberately NOT")
    print("run -- the task pre-registered the 21-trading-day-equivalent variant ('same as was")
    print("done for the 8 stock tickers'), and adding a second lookback after seeing the data")
    print("is the exact two-variant search this repo forbids. The ~1-year semantic is already")
    print("covered by PART 1's daily 252-bar rule on these same 4 coins.")
    print("(LTC/LINK/XRP/SOL hourly files also exist in the cache; the bonus was pre-registered")
    print("on these 4 only -- stated, not hidden.)")

    hourly_bars, hourly_params, hourly_warmups = {}, {}, {}
    print("\nLOOKBACK CONVERSION (same bars_per_session methodology as swing_recent_intraday.py's")
    print("Donchian arm / rsi_adaptive_percentile_intraday.py, measured per coin off its own kept")
    print("bars -- crypto trades 24/7 so this lands near 24 bars/session):")
    for c in CRYPTO_HOURLY:
        raw = bt.load_history(symbol=c, gran=3600)
        kept, dropped = bt.longest_continuous(raw)   # gran=3600, max_gap_hours=48: the
                                                     # module's own hourly-crypto defaults
        hourly_bars[c] = kept
        n_sess = sri.session_count(kept)
        bps = len(kept) / n_sess
        lb = round(21 * bps)
        hourly_params[c] = {"period": RSI_PERIOD, "lookback": lb, "lo_pct": 20.0, "hi_pct": 80.0}
        hourly_warmups[c] = RSI_PERIOD + lb + 54
        print("  %-4s %6d bars (%d dropped) %s -> %s | bars/session = %d/%d = %.4f |"
              " lookback = round(21 x %.4f) = %d | adaptive warmup = %d"
              % (c, len(kept), dropped, ts_date(kept[0][0]), ts_date(kept[-1][0]),
                 len(kept), n_sess, bps, bps, lb, hourly_warmups[c]))

    hourly_res = run_universe("crypto_hourly", CRYPTO_HOURLY, hourly_bars,
                              hourly_params, hourly_warmups)

    # ==================================================================
    # POOLED SUMMARY -- the honest distribution, no cherry-picking
    # ==================================================================
    print("\n\n" + "=" * 118)
    print("POOLED SUMMARY -- full distributions (the task's own requirement: report N/37 and")
    print("M/37 style counts, never one lucky coin as a headline)")
    print("=" * 118)
    all_pooled = {}
    for label, res, nlist in (("crypto_daily(37)", crypto_res, CRYPTO_DAILY),
                              ("futures(11)", fut_res, FUTURES),
                              ("crypto_hourly(4)", hourly_res, CRYPTO_HOURLY)):
        print("\n%s:" % label)
        for signal in SIGNALS:
            for segment in SEGMENTS:
                p = pooled(res[signal][segment])
                all_pooled[(label, signal, segment)] = p
                print_pooled("%-24s %-9s" % (signal, segment), p)

    # ==================================================================
    # FUN FINDINGS -- computed, not vibes (full-history crypto diagnostics)
    # ==================================================================
    print("\n" + "=" * 118)
    print("FUN FINDINGS (computed from the PART 1 full-history diagnostics table)")
    print("=" * 118)
    never30 = [c for c in CRYPTO_DAILY if fun[c]["pct_below30"] == 0.0]
    rarest = min(CRYPTO_DAILY, key=lambda c: fun[c]["pct_below30"])
    dippiest = max(CRYPTO_DAILY, key=lambda c: fun[c]["pct_below30"])
    best_bh = max(CRYPTO_DAILY, key=lambda c: fun[c]["bh_mult"])
    worst_bh = min(CRYPTO_DAILY, key=lambda c: fun[c]["bh_mult"])
    n_bh_loss = sum(1 for c in CRYPTO_DAILY if fun[c]["bh_mult"] < 1.0)
    print("  coins whose daily RSI(14) NEVER printed below 30 in their whole kept history: %s"
          % (", ".join(never30) if never30 else "none -- every coin visits oversold eventually"))
    print("  least oversold-prone: %s (%.2f%% of days below RSI 30, min RSI %.1f)"
          % (rarest, fun[rarest]["pct_below30"], fun[rarest]["rsi_min"]))
    print("  most oversold-prone:  %s (%.2f%% of days below RSI 30, min RSI %.1f)"
          % (dippiest, fun[dippiest]["pct_below30"], fun[dippiest]["rsi_min"]))
    print("  best raw buy&hold over kept history: %s (%.2fx over %.1fy) | worst: %s (%.3fx"
          " over %.1fy) | %d/37 coins are BELOW their first kept close (buy&hold lost money)"
          % (best_bh, fun[best_bh]["bh_mult"], fun[best_bh]["years"],
             worst_bh, fun[worst_bh]["bh_mult"], fun[worst_bh]["years"], n_bh_loss))
    doge = fun["DOGE"]
    print("  DOGE check: %.1fy kept history, RSI range %.1f-%.1f, %.2f%% of days oversold,"
          " buy&hold %.3fx" % (doge["years"], doge["rsi_min"], doge["rsi_max"],
                               doge["pct_below30"], doge["bh_mult"]))

    # ==================================================================
    # HONEST VERDICT
    # ==================================================================
    print("\n" + "=" * 118)
    print("HONEST VERDICT -- read this before reading anything above as a signal")
    print("=" * 118)
    tot = {"n": 0, "ctrl": 0, "zc": 0, "bh": 0}
    for (label, signal, segment), p in all_pooled.items():
        if segment != "sealed":
            continue
        tot["n"] += p["n"]
        tot["ctrl"] += p["n_beats_ctrl"]
        tot["zc"] += p["n_zc"]
        tot["bh"] += p["n_beats_bh"]
    print("Across ALL sealed-window arms (both signals, all three universes, %d instrument-arms"
          % tot["n"])
    print("total): beats own random control %d/%d | beats control AND zero-cost gap survives"
          % (tot["ctrl"], tot["n"]))
    print("%d/%d | beats buy&hold %d/%d." % (tot["zc"], tot["n"], tot["bh"], tot["n"]))
    print("""
Epistemic status: FIRST-PASS ONLY. No parameter-neighbor / rolling-window /
2x-cost stress gauntlet was run on anything here (same posture as every other
first pass in this line) -- nothing below is a confirmed edge, and nothing is
deployable. No live bot was touched or built; deploy decisions belong to a
future conversation. The futures numbers carry the roll-adjustment caveat in
PART 2 (SIL/MGC especially). The crypto real-cost numbers assume institutional-
grade 10bp+5bp costs (repo standard); a small retail spot account pays more.
""")
    print("DONE")


if __name__ == "__main__":
    main()
