#!/usr/bin/env python3
"""One-time builder for btc_demo/lab/backtest_reference.json (protocol §7).

Produces the frozen-schema rolling-window return reference distribution that
btc_lab_review.py consumes for the "Backtest 參考分佈對照" section:
for each candidate, the distribution of 30/90/180-day window returns
(stepped daily, i.e. every 24 hourly bars) over the candidate's already
independently audited historical equity curve.

STATIC ARTIFACT -- generate once, commit, done. This is deliberately NOT part
of the hourly lab workflow: the underlying history (BTC_1h.csv.gz, frozen
research data ending 2026-09-17) does not change, so the reference must not
be recomputed on live runs. Regenerate ONLY if the frozen historical data
source itself is ever replaced, in which case rerun this script by hand and
commit the diff.

Reuse, not re-derivation: the strategy equity curves come from the two
independently verified audit reimplementations (imported as modules, their
own load/feature/simulate functions called unchanged):

  Candidate A  sma_1d_30d_v25_v1
    ~/projects/project_codex/btc_rsr_swing_campaign_audit_20260920/
        independent_reimplementation.py
  Candidate B  rvol2_breakout24h_v25_v1
    ~/projects/project_codex/btc_factor_study_audit_20260920/
        independent_h1_reimplementation.py

Before building the reference, this script re-runs each reimplementation on
its audited development/validation/test splits and asserts the audited
headline numbers still reproduce (guards against silent drift in the reused
code/data). The reference distribution itself is then computed from ONE
continuous 2017-01-01..2026-09-17 run per candidate -- concatenating the
three audited splits would inject forced-liquidation artifacts into windows
straddling the split boundaries, so per-split stats can differ slightly from
the continuous run; that is expected and correct.

Window convention: return = equity[t + N*24] / equity[t] - 1, t stepped by
24 bars; percentiles via numpy default (linear) interpolation.

Requires the research venv (pandas/numpy): source ~/projects/quant_env/bin/activate
"""
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "btc_demo" / "lab" / "backtest_reference.json"

AUDIT_A = Path.home() / ("projects/project_codex/btc_rsr_swing_campaign_audit_20260920/"
                         "independent_reimplementation.py")
AUDIT_B = Path.home() / ("projects/project_codex/btc_factor_study_audit_20260920/"
                         "independent_h1_reimplementation.py")

FULL_RANGE = ("2017-01-01", "2026-09-17")
WINDOW_DAYS = (30, 90, 180)
STEP_BARS = 24  # daily step over hourly equity

# Audited headline numbers the reused code must still reproduce (CAGR%, MDD%).
EXPECTED_A = {"development": (33.59, 28.90), "validation": (60.73, 12.91),
              "test": (3.78, 21.80)}
EXPECTED_B = {"development": (9.57, 13.03), "validation": (14.19, 15.75),
              "test": (4.73, 10.65)}
TOL_PP = 0.05


def _import(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check(label, period, got_cagr, got_mdd, expected):
    e_cagr, e_mdd = expected[period]
    print(f"  {label} {period:12s} CAGR {got_cagr:7.2f}% (audited {e_cagr:7.2f}%)  "
          f"MDD {got_mdd:6.2f}% (audited {e_mdd:6.2f}%)")
    assert abs(got_cagr - e_cagr) < TOL_PP and abs(got_mdd - e_mdd) < TOL_PP, \
        f"{label}/{period}: reused code no longer reproduces audited numbers"


def equity_candidate_a(mod):
    d = mod.load()
    w = mod.compute_weight_series(d)
    for period, (start, end) in mod.PERIODS.items():
        r = mod.simulate(d, w, start, end)
        _check("A", period, r["cagr_pct"], r["mdd_pct"], EXPECTED_A)
    full = mod.simulate(d, w, *FULL_RANGE)
    return np.asarray(full["equity"], dtype=float)


def equity_candidate_b(mod):
    import math
    rows = mod.load()
    f = mod.features(rows)
    for period, lim in mod.PERIODS.items():
        ev, ab = mod.schedule(f, lim)
        gated = [i for i in ev if f["rvol"][i] is not None and f["rvol"][i] >= 2.0]
        curve, trades = mod.simulate(f, gated, ab, 12)
        m = mod.metrics(curve, f["ts"][ab[0]:ab[1]])
        _check("B", period, m["cagr_pct"], m["mdd_pct"], EXPECTED_B)
    ev, ab = mod.schedule(f, FULL_RANGE)
    gated = [i for i in ev if f["rvol"][i] is not None and f["rvol"][i] >= 2.0]
    curve, _ = mod.simulate(f, gated, ab, 12)
    return np.asarray(curve, dtype=float)


def rolling_reference(equity):
    out = {}
    for nd in WINDOW_DAYS:
        span = nd * 24
        starts = range(0, len(equity) - span, STEP_BARS)
        rets = np.array([(equity[t + span] / equity[t] - 1.0) * 100.0 for t in starts])
        pct = np.percentile(rets, [5, 25, 50, 75, 95])
        out[str(nd)] = {
            "n_windows": int(len(rets)),
            "return_pct_percentiles": {k: round(float(v), 4) for k, v in
                                       zip(("p5", "p25", "p50", "p75", "p95"), pct)},
        }
        print(f"    {nd:4d}d  n={len(rets):5d}  p5={pct[0]:8.2f}  p50={pct[2]:8.2f}  "
              f"p95={pct[4]:8.2f}")
    return out


def main():
    mod_a = _import(AUDIT_A, "audit_a")
    mod_b = _import(AUDIT_B, "audit_b")
    data_a = mod_a.HIST / "BTC_1h.csv.gz"
    data_b = mod_b.DATA

    print("== Reproduction check against audited split numbers ==")
    eq_a = equity_candidate_a(mod_a)
    eq_b = equity_candidate_b(mod_b)

    print("== Rolling-window reference (continuous %s..%s run) ==" % FULL_RANGE)
    strategies = {}
    print("  sma_1d_30d_v25_v1")
    strategies["sma_1d_30d_v25_v1"] = {"window_days": rolling_reference(eq_a)}
    print("  rvol2_breakout24h_v25_v1")
    strategies["rvol2_breakout24h_v25_v1"] = {"window_days": rolling_reference(eq_b)}

    ref = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": (
            "build_backtest_reference.py (one-time static build; regenerate only if the "
            "frozen historical data changes). Equity curves reused from the independently "
            "audited reimplementations: A=%s (data %s sha256=%s), B=%s (data %s sha256=%s). "
            "Continuous %s..%s run per candidate; window return = eq[t+N*24]/eq[t]-1, "
            "t stepped 24 hourly bars (daily); numpy linear-interpolation percentiles; "
            "audited dev/val/test headline numbers re-verified before build."
            % (AUDIT_A, data_a, _sha256(data_a), AUDIT_B, data_b, _sha256(data_b),
               *FULL_RANGE)
        ),
        "strategies": strategies,
    }
    OUT.write_text(json.dumps(ref, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
