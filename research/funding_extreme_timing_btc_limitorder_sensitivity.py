#!/usr/bin/env python3
"""
================================================================================
  LIMIT-ORDER COST-SENSITIVITY on funding_extreme_timing_btc.py -- does a
  maker/limit-order cost assumption flip the pre-registered rule from
  money-losing to money-making at real cost?
================================================================================
  This is a COST-ASSUMPTION SENSITIVITY CHECK, not a new study. The signal
  (z<=-2.0 enter / z>=0.0 exit on the trailing-168-settlement funding
  z-score) is untouched -- imported unmodified from funding_extreme_timing_btc.py
  via importlib (module loaded, main() NOT called, so nothing is recomputed
  or re-derived differently). Only the fee/slippage numbers going into
  bt.backtest() on the SAME 84 round trips change.

  Three cost scenarios on the identical `want` array:
    1. REAL (taker)   fee_bps=10.0 slip_bps=5.0  -- repo standard, on record:
       CAGR -12.6%  MDD 16.0%  Calmar -0.79  Sharpe -0.91
    2. LIMIT (maker)  fee_bps=2.0  slip_bps=0.0   -- this script's addition
    3. ZERO-COST      fee_bps=0.0  slip_bps=0.0   -- on record:
       CAGR +13.1%  Calmar +1.42

  fee_bps=2.0 is a Kraken Futures maker-fee ballpark (published maker fees
  on Kraken Futures sit in the 0.00-0.02% range depending on tier; 2bp is a
  conservative round number, not fitted). slip_bps=0.0 assumes the resting
  limit order fills exactly at the quoted price with no adverse slippage --
  the textbook maker assumption.

  ================================================================
  ★★★ THE CAVEAT THIS SCRIPT CANNOT REMOVE, STATED UP FRONT ★★★
  ================================================================
  This rerun assumes every one of the 84 round-trip fills happens AT THE
  SAME PRICE as the taker-cost version assumed (bar i+1's open, per
  bt.backtest()'s fill convention) -- just with a lower fee and zero
  slippage bolted on. A REAL limit order is not guaranteed to fill at all.
  Funding-extreme moments are exactly the kind of fast-moving, thin-liquidity
  moments where a passive resting order can miss the fill entirely, or only
  partially fill, or fill only after the favorable window has already
  passed -- none of which this backtest engine can model. A lower fee number
  is not the same thing as a lower REALIZED cost if the order sometimes does
  not execute. Read the numbers below as "if every fill happens as assumed,
  cost drops to X" -- not as "the strategy now works at X cost in live
  trading."

  Run:  source ~/projects/quant_env/bin/activate  &&  \\
        python3 research/funding_extreme_timing_btc_limitorder_sensitivity.py
================================================================================
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_spec = importlib.util.spec_from_file_location(
    "funding_extreme_timing_btc", os.path.join(HERE, "funding_extreme_timing_btc.py"))
fet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fet)   # module-level code only -- main() is __main__-guarded, NOT run

bt = fet.sri.bt
fmt = fet.fmt

LIMIT_COST = dict(fee_bps=2.0, slip_bps=0.0)


def rebuild_signal():
    """Reuses funding_extreme_timing_btc.py's own functions, unmodified, to
    rebuild the exact same `bars_seg` / `want` this script depends on --
    nothing here recomputes the signal differently."""
    import btc_basis
    f_rows, per_day, source = btc_basis.load_funding()
    f_times = [t // 1000 for t, _ in f_rows]
    f_rates = [r for _, r in f_rows]
    f_z = fet.settlement_zscores(f_rates)

    raw = bt.load_history(symbol="BTC", gran=3600)
    kept, dropped = bt.longest_continuous(raw)

    t0 = f_times[fet.Z_WINDOW]
    bars_seg = [b for b in kept if b[0] >= t0]
    want = fet.want_from_funding(bars_seg, f_times, f_z)
    return bars_seg, want, f_times, f_z


def eq_curve_index(equity, start_cash):
    """Equity series -> index level starting at 1.0, for charting."""
    return [e / start_cash for e in equity]


def main():
    print("=" * 100)
    print("LIMIT-ORDER COST-SENSITIVITY -- same pre-registered signal, three cost assumptions")
    print("=" * 100)

    bars_seg, want, f_times, f_z = rebuild_signal()
    print("  reused bars_seg (%d bars) and want array unmodified from "
          "funding_extreme_timing_btc.py -- signal NOT touched" % len(bars_seg))

    start_cash = 100_000.0
    scenarios = [
        ("REAL (taker)  fee=10bp slip=5bp", fet.COST),
        ("LIMIT (maker) fee=2bp  slip=0bp", LIMIT_COST),
        ("ZERO-COST     fee=0bp  slip=0bp", fet.sri.ZERO_COST),
    ]

    curves = {}
    print("\n%-38s %8s %8s %8s %8s %7s" % ("scenario", "CAGR%", "MDD%", "Calmar", "Sharpe", "trades"))
    for label, cost in scenarios:
        eq, tr = bt.backtest(bars_seg, want, start_cash=start_cash, **cost)
        mm = bt.metrics(bars_seg, eq, tr, start_cash=start_cash, want=want)
        print("%-38s %8s %8s %8s %8s %7d"
              % (label, fmt(mm["cagr_pct"], 1), fmt(mm["max_dd_pct"], 1),
                 fmt(mm["calmar"]), fmt(mm["sharpe"]), len(tr)))
        key = label.split()[0].lower().replace("(taker)", "").strip()
        curves[key] = eq_curve_index(eq, start_cash)

    eq_bh, tr_bh = bt.backtest(bars_seg, [True] * len(bars_seg), start_cash=start_cash, **fet.COST)
    mm_bh = bt.metrics(bars_seg, eq_bh, tr_bh, start_cash=start_cash, want=[True] * len(bars_seg))
    print("%-38s %8s %8s %8s %8s %7s"
          % ("BUY&HOLD (same slice, taker cost)", fmt(mm_bh["cagr_pct"], 1),
             fmt(mm_bh["max_dd_pct"], 1), fmt(mm_bh["calmar"]), fmt(mm_bh["sharpe"]), "n/a"))
    curves["buyhold"] = eq_curve_index(eq_bh, start_cash)

    limit_eq, limit_tr = bt.backtest(bars_seg, want, start_cash=start_cash, **LIMIT_COST)
    limit_mm = bt.metrics(bars_seg, limit_eq, limit_tr, start_cash=start_cash, want=want)

    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    if limit_mm["cagr_pct"] > 0:
        print("  Under the LIMIT-ORDER cost assumption, the SAME 84 trades turn CAGR-positive:")
        print("  %.1f%% CAGR, Calmar %.2f, vs REAL (taker) cost's %s%% CAGR."
              % (limit_mm["cagr_pct"], limit_mm["calmar"], fmt(fet.COST and -12.6, 1)))
    else:
        print("  Even under the LIMIT-ORDER cost assumption, the strategy still loses money:")
        print("  %.1f%% CAGR, Calmar %.2f." % (limit_mm["cagr_pct"], limit_mm["calmar"]))
    print("""
  *** BUT SEE THE CAVEAT AT THE TOP OF THIS SCRIPT'S DOCSTRING ***
  This number assumes every one of the 84 round-trip fills executes at the
  exact price the taker-cost version assumed, with zero non-fill risk. Real
  limit orders placed into fast-moving funding-extreme moments can miss
  fills entirely. This is a "cost accounting" sensitivity, not a demonstration
  that the strategy is executable at this cost in live trading. It remains an
  EXPLORATORY, non-sealed, non-adversarially-reviewed first pass on a single
  ~12-month window -- nothing here is a confirmed edge or deployable.
  DONE""")

    # ---- dump chart data ----
    dates = [fet.ts_date(b[0]) for b in bars_seg]
    out = {
        "start": dates[0], "end": dates[-1], "n_bars": len(bars_seg),
        "dates": dates,
        "curve_real": curves["real"], "curve_limit": curves["limit"],
        "curve_zero": curves["zero-cost"], "curve_buyhold": curves["buyhold"],
        "limit_cagr_pct": limit_mm["cagr_pct"], "limit_mdd_pct": limit_mm["max_dd_pct"],
        "limit_calmar": limit_mm["calmar"], "limit_sharpe": limit_mm["sharpe"],
        "limit_trades": len(limit_tr),
    }
    out_path = "/private/tmp/claude-501/-Users-wongmingwa/2312c3a2-66a0-4ae5-9dbe-30998b926bf9/scratchpad/chart_funding_cost_comparison.json"
    with open(out_path, "w") as fh:
        json.dump(out, fh)
    print("wrote %s (%d points/curve)" % (out_path, len(dates)))


if __name__ == "__main__":
    main()
