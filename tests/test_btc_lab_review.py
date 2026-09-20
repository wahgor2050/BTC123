"""Tests for btc_lab_review.py (checkpoint review exporter) and the run-health
machinery added to btc_forward_lab.py. Deterministic fixtures, no network."""
import csv
import json
import os
import tempfile
import unittest

import btc_forward_lab as lab
import btc_lab_review as rev

HOUR = 3600
DAY = 86400
T0 = 1_790_000_000 - (1_790_000_000 % HOUR)


def order_row(sid, side, ts, qty_before, qty_after, realized_delta,
              fill=100.0, mid=100.0, qty=1.0, fee=0.1, gross=100.0,
              diag_fill=None, diag_fee=None):
    return {"order_id": "%s:%d:%s" % (sid, ts, side), "event_id": "%s:%d" % (sid, ts),
            "strategy": sid, "side": side, "decision_ts": str(ts),
            "request_ts": str(float(ts)), "quote_exchange_ts": str(ts),
            "bid": "99.9", "ask": "100.1", "mid": str(mid), "quote_size": "",
            "fill_price": str(fill), "qty": str(qty), "gross_notional": str(gross),
            "fee": str(fee), "cash_before": "0", "qty_before": str(qty_before),
            "cash_after": "0", "qty_after": str(qty_after), "basis_after": "0",
            "realized_pnl_delta": str(realized_delta), "reason": "t",
            "signal_ts": str(ts), "exit_due_ts": "", "lateness_sec": "0",
            "diag_fill_price": str(diag_fill if diag_fill is not None else fill),
            "diag_fee": str(diag_fee if diag_fee is not None else fee), "schema": "1"}


def eq_row(sid, ts, nav, mark=100.0, exposure=50.0, fees=1.0, status="ok"):
    return {"ts": str(ts), "time": rev.iso(ts), "strategy": sid, "cash": "0",
            "qty": "0", "mark": str(mark), "mark_source": "quote_mid",
            "cost_basis": "0", "realized_pnl": "0", "unrealized_pnl": "0",
            "fees_cum": str(fees), "nav": str(nav), "exposure_pct": str(exposure),
            "peak_nav": str(nav), "drawdown_pct": "0", "data_status": status,
            "schema": "1"}


def write_fixture(d, activation_ts, accounts, orders, equity, opps=()):
    state = {"schema_version": 1, "mode": "PAPER", "activation_ts": activation_ts,
             "activation_utc": rev.iso(activation_ts),
             "venue_epoch": {"venue": "bitstamp", "product": "btcusd",
                             "quote_ccy": "USD", "epoch_start_ts": activation_ts,
                             "epoch_start_utc": rev.iso(activation_ts)},
             "accounts": accounts, "last_run": {}, "updated_utc": rev.iso(activation_ts)}
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    for name, fields, rows in (("orders_x.csv", lab.ORDER_FIELDS, orders),
                               ("opportunities_x.csv", lab.OPP_FIELDS, opps),
                               ("equity_x.csv", lab.EQ_FIELDS, equity)):
        if rows:
            with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
    return state


def acct(sid, kind="sma_trend"):
    return {"strategy_id": sid, "version": "v1", "params": {"kind": kind},
            "param_hash": "x", "paused": False, "start_cash": 100000.0,
            "pending": []}


class TestEpisodes(unittest.TestCase):
    def test_flat_to_flat_pairing_with_partial_sells(self):
        sid = "s"
        orders = [order_row(sid, "BUY", T0, 0, 2, 0),
                  order_row(sid, "SELL", T0 + HOUR, 2, 1, 50.0),      # partial
                  order_row(sid, "SELL", T0 + 2 * HOUR, 1, 0, -20.0),  # closes
                  order_row(sid, "BUY", T0 + 3 * HOUR, 0, 1, 0)]       # still open
        eps, has_open = rev.episodes_from_orders(orders)
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual(eps[0]["pnl"], 30.0)
        self.assertTrue(has_open)

    def test_no_orders(self):
        eps, has_open = rev.episodes_from_orders([])
        self.assertEqual(eps, [])
        self.assertFalse(has_open)


class TestAnalyse(unittest.TestCase):
    def _review(self, days, navs=None, orders=(), sid="sma_1d_30d_v25_v1",
                marks=None, exposure=50.0):
        with tempfile.TemporaryDirectory() as d:
            n = max(2, int(days * 4))                     # sparse snapshots are fine
            step = int(days * DAY / (n - 1))
            navs = navs or [100000.0] * n
            marks = marks or [100.0] * len(navs)
            eq = [eq_row(sid, T0 + i * step, navs[i], mark=marks[i], exposure=exposure)
                  for i in range(len(navs))]
            kind = ("rvol_breakout" if sid.startswith("rvol")
                    else ("funding_limit" if sid.startswith("funding") else "sma_trend"))
            write_fixture(d, T0, {sid: acct(sid, kind=kind)}, list(orders), eq)
            r = rev.build_review(d)
            return r["results"][0]

    def test_mdd_and_return(self):
        navs = [100000, 110000, 88000, 99000]
        r = self._review(10, navs=navs)
        self.assertAlmostEqual(r["return_pct"], -1.0, places=3)
        self.assertAlmostEqual(r["mdd_pct"], (88000 / 110000 - 1) * 100, places=3)

    def test_verdict_gated_before_30_days(self):
        r = self._review(5)
        self.assertIn("未夠30日", r["verdict"]["checkpoint"])
        self.assertNotIn("escalate", r["verdict"])

    def test_r1_mdd_triggers_even_early(self):
        navs = [100000, 100000, 65000, 70000]
        r = self._review(5, navs=navs)
        self.assertIn("TRIGGERED", r["verdict"]["R1_mdd"])

    def test_escalation_all_met_at_90d(self):
        sid = "sma_1d_30d_v25_v1"
        orders = [order_row(sid, "BUY", T0, 0, 1, 0),
                  order_row(sid, "SELL", T0 + DAY, 1, 0, 8000.0)]
        navs = [100000.0 + i * 60 for i in range(360)]          # +21.5%, no dd
        marks = [100.0] * 360                                   # flat BTC → control 0
        r = self._review(91, navs=navs, orders=orders, marks=marks)
        self.assertEqual(r["verdict"]["checkpoint"], "T+90d")
        self.assertIn("ALL CONDITIONS MET", r["verdict"]["escalate"])
        self.assertIn("matched-control", r["verdict"]["escalate"])

    def test_escalation_blocked_by_drop_best_sign_flip(self):
        sid = "sma_1d_30d_v25_v1"
        orders = [order_row(sid, "BUY", T0, 0, 1, 0),
                  order_row(sid, "SELL", T0 + DAY, 1, 0, 5000.0)]   # best > total gain
        navs = [100000.0 + i * 5 for i in range(360)]               # +1.8% total
        r = self._review(91, navs=navs, orders=orders, marks=[100.0] * 360)
        self.assertIn("E5", r["verdict"]["escalate"])

    def test_b_needs_8_episodes_to_escalate(self):
        sid = "rvol2_breakout24h_v25_v1"
        orders = []
        for k in range(3):
            ts = T0 + k * 2 * DAY
            orders += [order_row(sid, "BUY", ts, 0, 1, 0),
                       order_row(sid, "SELL", ts + DAY, 1, 0, 500.0)]
        navs = [100000.0 + i * 10 for i in range(360)]
        r = self._review(91, navs=navs, orders=orders, marks=[100.0] * 360, sid=sid)
        self.assertIn("E4", r["verdict"]["escalate"])
        self.assertIn("3/8", r["verdict"]["escalate"])

    def test_b_retire_on_10_negative_episodes(self):
        sid = "rvol2_breakout24h_v25_v1"
        orders = []
        for k in range(10):
            ts = T0 + k * 2 * DAY
            orders += [order_row(sid, "BUY", ts, 0, 1, 0),
                       order_row(sid, "SELL", ts + DAY, 1, 0, -100.0)]
        navs = [100000.0 - i * 10 for i in range(360)]
        r = self._review(91, navs=navs, orders=orders, marks=[100.0] * 360, sid=sid)
        self.assertIn("TRIGGERED", r["verdict"]["R3_episodes"])

    def test_control_is_btc_move_times_mean_exposure(self):
        marks = [100.0, 105.0, 110.0, 110.0]
        r = self._review(10, navs=[100000.0] * 4, marks=marks, exposure=50.0)
        self.assertAlmostEqual(r["control_return_pct"], 10.0 * 0.5, places=3)


def opp_row(sid, ts, decision, reason="t"):
    return {"event_id": "%s:%d" % (sid, ts), "strategy": sid, "signal_ts": str(ts),
            "signal_time": rev.iso(ts), "observed_ts": str(ts), "observed_late": "False",
            "close": "", "high24": "", "sma_fast": "", "sma_slow": "", "sigma_ann": "",
            "rvol_num": "", "rvol_den": "", "rvol": "", "target_weight": "",
            "cooldown_ok": "", "gate": "", "decision": decision, "reason": reason,
            "data_status": "ok", "venue": "bitstamp:btcusd", "schema": "1"}


class TestFundingReview(unittest.TestCase):
    SID = "funding_z168_limit_entry_v1"

    def _review_c(self, days, orders=(), opps=(), act_offset_days=0, ref=False):
        with tempfile.TemporaryDirectory() as d:
            n = max(2, int(days * 4))
            act = T0 + act_offset_days * DAY
            step = int(days * DAY / (n - 1))
            eq = [eq_row(self.SID, act + i * step, 100000.0) for i in range(n)]
            a = acct(self.SID, kind="funding_limit")
            a["activation_ts"] = act               # own epoch, later than the lab's
            write_fixture(d, T0, {self.SID: a}, list(orders), eq, opps=list(opps))
            if ref:                                # reference exists for A/B only
                with open(os.path.join(d, "backtest_reference.json"), "w") as fh:
                    json.dump({"source": "stub", "generated_utc": "x",
                               "strategies": {}}, fh)
            return rev.build_review(d)["results"][0], rev.render_markdown(
                rev.build_review(d))

    def test_own_activation_clock_and_coverage(self):
        r, _ = self._review_c(10, act_offset_days=30)   # lab started 30d earlier
        self.assertAlmostEqual(r["forward_days"], 10.0, places=1)
        self.assertLessEqual(r["coverage_pct"], 100.0)  # no phantom missed hours

    def test_fill_funnel_counts_and_rate(self):
        opps = ([opp_row(self.SID, T0 + k * DAY, "resting_placed", "funding_z_entry")
                 for k in range(4)]
                + [opp_row(self.SID, T0 + 10 * DAY, "expired", "expired_unfilled"),
                   opp_row(self.SID, T0 + 11 * DAY, "cancelled", "cancelled_z_reverted")])
        orders = [order_row(self.SID, "BUY", T0 + DAY, 0, 1, 0)]
        orders[0]["reason"] = "limit_fill_entry"
        orders[0]["lateness_sec"] = "7200"
        r, md = self._review_c(35, orders=orders, opps=opps)
        ff = r["fill_funnel"]
        self.assertEqual((ff["placed"], ff["filled"], ff["expired"], ff["cancelled"]),
                         (4, 1, 1, 1))
        self.assertAlmostEqual(ff["fill_rate_pct"], 25.0, places=1)
        self.assertEqual(ff["median_place_to_fill_min"], 120)
        self.assertIn("成交漏斗", md)

    def test_r5c_fillrate_retire_at_180d(self):
        opps = [opp_row(self.SID, T0 + k * DAY, "resting_placed", "funding_z_entry")
                for k in range(12)]
        orders = [order_row(self.SID, "BUY", T0 + DAY, 0, 1, 0)]
        orders[0]["reason"] = "limit_fill_entry"        # 1/12 = 8.3% <= 25%
        r, _ = self._review_c(181, orders=orders, opps=opps)
        self.assertIn("TRIGGERED", r["verdict"]["R5c_fillrate"])
        # under 180d: rule not yet applicable
        r2, _ = self._review_c(91, orders=orders, opps=opps)
        self.assertNotIn("R5c_fillrate", r2["verdict"])

    def test_backtest_reference_is_na_by_design(self):
        r, md = self._review_c(35, ref=True)
        self.assertIn("N/A — by design", md)


class TestDeterminismAndReference(unittest.TestCase):
    def test_same_archives_same_markdown(self):
        sid = "sma_1d_30d_v25_v1"
        with tempfile.TemporaryDirectory() as d:
            eq = [eq_row(sid, T0 + i * HOUR, 100000 + i) for i in range(5)]
            write_fixture(d, T0, {sid: acct(sid)}, [], eq)
            a = rev.render_markdown(rev.build_review(d))
            b = rev.render_markdown(rev.build_review(d))
            self.assertEqual(a, b)
            self.assertIn("N/A — 未夠30日", a)

    def test_reference_section_na_when_file_missing(self):
        with tempfile.TemporaryDirectory() as d:
            txt = rev.backtest_reference_section(d, [])
            self.assertIn("N/A", txt)
            self.assertIn("backtest_reference.json", txt)

    def test_reference_section_places_forward_return(self):
        sid = "sma_1d_30d_v25_v1"
        with tempfile.TemporaryDirectory() as d:
            ref = {"generated_utc": "x", "source": "unit-test",
                   "strategies": {sid: {"window_days": {"30": {
                       "n_windows": 100, "return_pct_percentiles":
                       {"p5": -10, "p25": -3, "p50": 1, "p75": 5, "p95": 12}}}}}}
            with open(os.path.join(d, "backtest_reference.json"), "w") as fh:
                json.dump(ref, fh)
            res = [{"strategy_id": sid, "forward_days": 35.0, "return_pct": 4.0}]
            txt = rev.backtest_reference_section(d, res)
            self.assertIn("p50–p75", txt)
            self.assertIn("純定位", txt)


class TestCommittedBacktestReference(unittest.TestCase):
    """Validates the committed static btc_demo/lab/backtest_reference.json
    (built once by build_backtest_reference.py from the audited historical
    equity curves; regenerated only if the frozen history ever changes)."""
    LAB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "btc_demo", "lab")

    def _load(self):
        with open(os.path.join(self.LAB, "backtest_reference.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_schema_and_percentile_sanity(self):
        ref = self._load()
        for key in ("generated_utc", "source", "strategies"):
            self.assertIn(key, ref)
        with open(os.path.join(self.LAB, "state.json"), encoding="utf-8") as fh:
            live_ids = set(json.load(fh)["accounts"])
        self.assertEqual(set(ref["strategies"]), live_ids)
        for sid, strat in ref["strategies"].items():
            self.assertEqual(set(strat["window_days"]), {"30", "90", "180"},
                             sid)
            for nd, bucket in strat["window_days"].items():
                self.assertGreater(bucket["n_windows"], 0, (sid, nd))
                p = bucket["return_pct_percentiles"]
                vals = [p[k] for k in ("p5", "p25", "p50", "p75", "p95")]
                self.assertEqual(vals, sorted(vals), (sid, nd))

    def test_consumer_renders_real_file(self):
        ref = self._load()
        res = [{"strategy_id": sid, "forward_days": 200.0, "return_pct": 1.0}
               for sid in ref["strategies"]]
        txt = rev.backtest_reference_section(self.LAB, res)
        self.assertNotIn("未生成", txt)
        self.assertIn("純定位", txt)
        self.assertEqual(txt.count("前向 180 日窗口回報"), len(res))


class TestRunHealth(unittest.TestCase):
    def test_data_weather_needs_two_consecutive_runs(self):
        st = {"accounts": {}}                    # no 'health' key: live-state compat
        self.assertEqual(lab.evaluate_health(st, {"no_quote"}, T0), [])
        self.assertEqual(lab.evaluate_health(st, {"no_quote"}, T0 + HOUR), ["no_quote"])

    def test_serious_kind_fires_immediately_and_rate_limits(self):
        st = {"accounts": {}}
        self.assertEqual(lab.evaluate_health(st, {"reconcile_fail"}, T0), ["reconcile_fail"])
        self.assertEqual(lab.evaluate_health(st, {"reconcile_fail"}, T0 + HOUR), [])
        self.assertEqual(lab.evaluate_health(st, {"reconcile_fail"},
                                             T0 + lab.HEALTH_MIN_REPEAT_SEC + 1),
                         ["reconcile_fail"])

    def test_recovery_resets_consecutive_counter(self):
        st = {"accounts": {}}
        lab.evaluate_health(st, {"no_quote"}, T0)
        lab.evaluate_health(st, set(), T0 + HOUR)          # recovered
        self.assertEqual(lab.evaluate_health(st, {"no_quote"}, T0 + 2 * HOUR), [])

    def test_collect_run_issues(self):
        st = {"accounts": {"a": {"pending": [{"kind": "exit_unresolved"}]}}}

        class Eng:
            snapshots = [{"data_status": "RECONCILE_FAIL"}]
        issues = lab.collect_run_issues([], None, Eng(), st)
        self.assertEqual(issues, {"no_candles", "no_quote",
                                  "reconcile_fail", "exit_unresolved"})

    def test_health_state_survives_json_roundtrip(self):
        st = {"accounts": {}}
        lab.evaluate_health(st, {"no_quote"}, T0)
        st2 = json.loads(json.dumps(st))
        self.assertEqual(lab.evaluate_health(st2, {"no_quote"}, T0 + HOUR), ["no_quote"])


if __name__ == "__main__":
    unittest.main()
