"""Behavior tests for btc_forward_lab.py — deterministic fixtures, no network.

Covers the pre-activation validation list from the forward-lab spec:
as-of/prefix invariance, closed-candle exclusion, RVOL denominator exclusion,
sample SD / SMA windows, exact 10%-of-NAV resize band, parent cooldown consumed
on gate rejection, gap handling, independent H1 conventions, affordability,
fee/basis allocation on partial trims, ledger-to-NAV reconciliation, 24h timed
exits (including late and quote-less), stale-data entry blocks, restart
recovery and duplicate dispatch, and the frozen fill model.
"""
import math
import statistics
import unittest

import btc_forward_lab as lab

HOUR = 3600
T0 = 1_790_000_000 - (1_790_000_000 % HOUR)   # aligned hour start


def mk_bars(n, price=100.0, vol=10.0, start=T0):
    return [[start + i * HOUR, price, price, price, price, vol] for i in range(n)]


def quote(bid=99.9, ask=100.1, ts=None, now=None):
    now = now if now is not None else 0
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
            "exchange_ts": ts or int(now), "request_ts": float(now),
            "receipt_ts": float(now), "quote_size": None}


def fresh_state(bars, now, q=None):
    st = lab.new_state(now, bars, q or quote(now=now))
    return st


class TestIndicators(unittest.TestCase):
    def test_sma_windows_include_current_bar(self):
        bars = mk_bars(800)
        for i, b in enumerate(bars):
            b[4] = 100.0 + i * 0.01
        f = lab.build_features(bars)
        i = 780
        self.assertAlmostEqual(f["sma_fast"][i], sum(b[4] for b in bars[i - 23:i + 1]) / 24, places=10)
        self.assertAlmostEqual(f["sma_slow"][i], sum(b[4] for b in bars[i - 719:i + 1]) / 720, places=10)
        self.assertIsNone(f["sma_slow"][718])

    def test_sigma_is_sample_std_ddof1_annualised(self):
        import random
        rng = random.Random(7)
        bars = mk_bars(900)
        px = 100.0
        for b in bars:
            px *= math.exp(rng.gauss(0, 0.01))
            b[4] = px
        f = lab.build_features(bars)
        i = 850
        rets = [math.log(bars[k][4] / bars[k - 1][4]) for k in range(i - 719, i + 1)]
        expect = statistics.stdev(rets) * math.sqrt(24 * 365.25)   # ddof=1
        self.assertAlmostEqual(f["sigma"][i], expect, places=12)
        # 0.25, not 25: weight formula
        self.assertAlmostEqual(f["weight"][i], min(1.0, 0.25 / expect), places=12)

    def test_prefix_invariance(self):
        import random
        rng = random.Random(3)
        bars = mk_bars(900)
        px = 100.0
        for b in bars:
            px *= math.exp(rng.gauss(0, 0.01))
            b[1] = b[2] = b[3] = b[4] = px
            b[5] = abs(rng.gauss(10, 3)) + 0.1
        full = lab.build_features(bars)
        for k in [750, 820, 880]:
            part = lab.build_features(bars[:k + 1])
            for col in ["sma_fast", "sma_slow", "sigma", "weight", "high24", "rvol"]:
                a, b_ = full[col][k], part[col][k]
                if a is None:
                    self.assertIsNone(b_, (col, k))
                else:
                    self.assertAlmostEqual(a, b_, places=12, msg=(col, k))
            self.assertEqual(full["breakout"][k], part["breakout"][k])

    def test_rvol_denominator_excludes_current_and_uses_28_prior_days(self):
        bars = mk_bars(700)
        i = 690
        bars[i][5] = 999.0                      # current volume must not enter the median
        prior = sorted(bars[i - 24 * k][5] for k in range(1, 29))
        f = lab.build_features(bars)
        self.assertAlmostEqual(f["rvol_den"][i], (prior[13] + prior[14]) / 2, places=12)
        self.assertAlmostEqual(f["rvol"][i], 999.0 / 10.0, places=12)

    def test_high24_excludes_current_bar(self):
        bars = mk_bars(800)
        i = 790
        bars[i][2] = 500.0                      # current high must be excluded
        f = lab.build_features(bars)
        self.assertAlmostEqual(f["high24"][i], 100.0, places=12)

    def test_gap_does_not_become_consecutive_bars(self):
        bars = mk_bars(900)
        del bars[850]                           # hole in the series
        tail = lab.contiguous_tail(bars)
        self.assertEqual(tail[0][0], T0 + 851 * HOUR)
        self.assertEqual(len(tail), 49)


class TestFillModel(unittest.TestCase):
    def test_min_half_spread_floor_and_wide_spread_override(self):
        q = quote(bid=99.99, ask=100.01)        # tight: floor binds
        buy, sell = lab.fill_prices(q)
        self.assertAlmostEqual(buy, 100.0 * 1.0002, places=10)
        self.assertAlmostEqual(sell, 100.0 * 0.9998, places=10)
        q = quote(bid=99.0, ask=101.0)          # wide: real spread overrides
        buy, sell = lab.fill_prices(q)
        self.assertAlmostEqual(buy, 101.0, places=10)
        self.assertAlmostEqual(sell, 99.0, places=10)


class TestAccounting(unittest.TestCase):
    def test_fee_in_basis_partial_trim_and_reconciliation(self):
        a = lab.new_account("sma_1d_30d_v25_v1")
        lab.apply_buy(a, 1.0, 100.0)
        self.assertAlmostEqual(a["cost_basis"], 100.1, places=9)     # fee inside basis
        self.assertAlmostEqual(a["cash"], 100000 - 100.1, places=9)
        gross, fee, delta = lab.apply_sell(a, 0.4, 110.0)
        self.assertAlmostEqual(fee, 44 * 0.001, places=12)
        self.assertAlmostEqual(delta, (44 - 0.044) - 100.1 * 0.4, places=9)
        self.assertAlmostEqual(a["cost_basis"], 100.1 * 0.6, places=9)
        ok, nav, unreal = lab.reconcile(a, 110.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(a["realized_pnl"] + unreal, nav - 100000, places=6)
        self.assertEqual(a["episodes"], 0)
        lab.apply_sell(a, a["qty"], 90.0)                            # full exit
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["cost_basis"], 0.0)
        self.assertEqual(a["episodes"], 1)
        ok, nav, unreal = lab.reconcile(a, 90.0)
        self.assertTrue(ok)
        self.assertEqual(unreal, 0.0)

    def test_cash_never_negative_on_entry(self):
        a = lab.new_account("sma_1d_30d_v25_v1")
        px = 100.0 * 1.0002
        qty = (1.0 * a["cash"]) / (px * (1 + lab.FEE_RATE))          # w=1 full entry
        lab.apply_buy(a, qty, px)
        self.assertGreaterEqual(a["cash"], -1e-9)


def trending_bars(n=900, base=100.0, step=0.05, vol=10.0):
    """Slow uptrend: SMA24 > SMA720, no 24h-high breakout unless spiked."""
    bars = mk_bars(n)
    for i, b in enumerate(bars):
        px = base + i * step
        b[1] = b[2] = b[3] = b[4] = px
        b[5] = vol
    return bars


class TestSmaCandidate(unittest.TestCase):
    SID = "sma_1d_30d_v25_v1"

    def engine(self, bars, now, q, state=None):
        st = state or fresh_state(bars[:-1], bars[-2][0] + HOUR, q)
        # activation happened at the second-to-last bar close; last bar is fresh
        return lab.LabEngine(st, now, bars, q), st

    def test_enter_from_flat_ignores_band_and_reserves_fees(self):
        bars = trending_bars()
        now = bars[-1][0] + HOUR + 60           # 1 min after close: fresh
        q = quote(bid=bars[-1][4] - .05, ask=bars[-1][4] + .05, now=now)
        eng, st = self.engine(bars, now, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 1)
        self.assertGreater(a["qty"], 0)
        self.assertGreaterEqual(a["cash"], 0)
        f = lab.build_features(bars)
        self.assertAlmostEqual(
            a["qty"] * eng.orders[0]["fill_price"] * (1 + lab.FEE_RATE),
            f["weight"][-1] * 100000, delta=0.02)

    def test_stale_entry_recorded_missed_not_backfilled(self):
        bars = trending_bars()
        now = bars[-1][0] + HOUR + 45 * 60      # 45 min after close: stale
        q = quote(bid=bars[-1][4] - .05, ask=bars[-1][4] + .05, now=now)
        eng, st = self.engine(bars, now, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0)
        rows = [o for o in eng.opps if o["strategy"] == self.SID]
        self.assertEqual(rows[-1]["decision"], "missed")
        self.assertIn("stale", rows[-1]["reason"])
        self.assertTrue(rows[-1]["observed_late"])

    def test_exit_to_zero_executes_even_late(self):
        bars = trending_bars()
        now0 = bars[-1][0] + HOUR + 60
        q0 = quote(bid=bars[-1][4] - .05, ask=bars[-1][4] + .05, now=now0)
        eng, st = self.engine(bars, now0, q0)
        eng.run()
        self.assertGreater(st["accounts"][self.SID]["qty"], 0)
        # trend collapses over 30 bars (SMA24 falls below SMA720); the run
        # arrives 50 minutes late and must still exit — acting once, on the
        # latest completed candle, never back-filling the 29 it slept through
        px = bars[-1][4] * 0.5
        crash = bars + [[bars[-1][0] + (k + 1) * HOUR, px, px, px, px, 10.0]
                        for k in range(30)]
        now1 = crash[-1][0] + HOUR + 50 * 60
        q1 = quote(bid=px - .05, ask=px + .05, now=now1)
        eng2 = lab.LabEngine(st, now1, crash, q1)
        eng2.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["episodes"], 1)
        sells = [o for o in eng2.orders if o["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "trend_down_exit")

    def test_resize_band_is_10pct_of_nav_exact_threshold(self):
        bars = trending_bars()
        now0 = bars[-1][0] + HOUR + 60
        mid0 = bars[-1][4]
        q0 = quote(bid=mid0 - .05, ask=mid0 + .05, now=now0)
        eng, st = self.engine(bars, now0, q0)
        eng.run()
        a = st["accounts"][self.SID]
        f = lab.build_features(bars)
        w = f["weight"][-1]
        # next candle: same weight; move the QUOTE so held drifts just under/over band
        nxt = bars + [[bars[-1][0] + HOUR, mid0, mid0, mid0, mid0, 10.0]]
        f2 = lab.build_features(nxt)
        w2 = f2["weight"][-1] if f2["sma_fast"][-1] > f2["sma_slow"][-1] else 0.0
        self.assertGreater(w2, 0)
        # engineer a mid such that |w2*nav - held| is just inside the band
        for scale, expect_orders in [(1.0, 0), (3.0, 1)]:
            st2 = {"accounts": {self.SID: dict(a)}}
            st2["accounts"][self.SID]["last_signal_ts"] = bars[-1][0]
            mid = mid0 * scale
            qn = quote(bid=mid - .05, ask=mid + .05, now=nxt[-1][0] + HOUR + 60)
            eng2 = lab.LabEngine(st2, nxt[-1][0] + HOUR + 60, nxt, qn)
            eng2.run_sma(st2["accounts"][self.SID])
            got = [o for o in eng2.orders]
            nav = st2["accounts"][self.SID]["cash"] + a["qty"] * mid
            drift = abs(w2 * nav - a["qty"] * mid)
            if drift > 0.10 * nav:
                self.assertEqual(len(got), 1, "drift %.2f nav %.2f should resize" % (drift, nav))
            else:
                self.assertEqual(len(got), 0, "drift %.2f nav %.2f should hold" % (drift, nav))

    def test_no_quote_blocks_action_and_flags(self):
        bars = trending_bars()
        now = bars[-1][0] + HOUR + 60
        eng, st = self.engine(bars, now, None)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0)
        rows = [o for o in eng.opps if o["strategy"] == self.SID]
        self.assertEqual(rows[-1]["reason"], "no_executable_quote")


def breakout_bars(n=900, vol=10.0, spike_at=None, spike_vol=None):
    """Flat-ish series with an engineered 24h-high + SMA720 breakout at the end."""
    bars = mk_bars(n, price=100.0, vol=vol)
    for i, b in enumerate(bars):
        px = 100.0 + 0.001 * i
        b[1] = b[2] = b[3] = b[4] = px
    if spike_at is not None:
        b = bars[spike_at]
        b[4] = b[2] = bars[spike_at - 1][4] * 1.05     # close above prior 24h high
        b[5] = spike_vol if spike_vol is not None else vol
    return bars


class TestRvolCandidate(unittest.TestCase):
    SID = "rvol2_breakout24h_v25_v1"

    def run_engine(self, bars, now, q, state=None, activation_idx=-2):
        st = state or fresh_state(bars[:activation_idx + 1] if activation_idx != -1 else bars,
                                  bars[activation_idx][0] + HOUR, q)
        eng = lab.LabEngine(st, now, bars, q)
        eng.run()
        return eng, st

    def test_entry_on_fresh_breakout_with_rvol_and_24h_exit(self):
        bars = breakout_bars(spike_at=899, spike_vol=50.0)   # RVOL = 5
        now = bars[-1][0] + HOUR + 120
        mid = bars[-1][4]
        q = quote(bid=mid - .05, ask=mid + .05, now=now)
        eng, st = self.run_engine(bars, now, q)
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 1)
        self.assertGreater(a["qty"], 0)
        self.assertEqual(a["exit_due_ts"], int(now) + 24 * HOUR)
        self.assertEqual(a["last_parent_signal_ts"], bars[-1][0])
        # 25h later, exit fires with lateness recorded
        now2 = a["exit_due_ts"] + 600
        q2 = quote(bid=mid + 1, ask=mid + 1.1, now=now2)
        eng2 = lab.LabEngine(st, now2, bars, q2)
        eng2.run()
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["episodes"], 1)
        sells = [o for o in eng2.orders if o["side"] == "SELL"]
        self.assertEqual(sells[0]["lateness_sec"], 600)
        ok, nav, unreal = lab.reconcile(a, q2["mid"])
        self.assertTrue(ok)

    def test_gate_reject_still_consumes_cooldown(self):
        # breakout with LOW volume at bar 874, second breakout 24h later at 898:
        # 898-874 = 24 < 25 -> second parent must be cooldown-rejected even
        # though the first failed the RVOL gate.
        bars = breakout_bars(spike_at=874, spike_vol=10.0)   # RVOL 1.0: gate fails
        b2 = bars[898]
        b2[4] = b2[2] = max(b[2] for b in bars[874:898]) * 1.05
        b2[5] = 80.0                                          # RVOL >= 2: gate passes
        now = bars[-1][0] + HOUR + 120
        q = quote(bid=b2[4] - .05, ask=b2[4] + .05, now=now)
        st = fresh_state(bars[:874], bars[873][0] + HOUR, q)  # activation before both
        eng = lab.LabEngine(st, now, bars, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0, "cooldown from gate-rejected parent must block")
        self.assertEqual(a["last_parent_signal_ts"], bars[874][0])
        rows = {r["signal_ts"]: r for r in eng.opps if r["strategy"] == self.SID}
        self.assertEqual(rows[bars[874][0]]["decision"], "rejected")
        self.assertIn("rvol_below", rows[bars[874][0]]["reason"])
        self.assertEqual(rows[bars[898][0]]["reason"], "cooldown")

    def test_stale_breakout_missed_not_backfilled(self):
        bars = breakout_bars(spike_at=897, spike_vol=50.0)
        # two more quiet candles after the spike; run arrives now
        now = bars[-1][0] + HOUR + 120
        mid = bars[-1][4]
        q = quote(bid=mid - .05, ask=mid + .05, now=now)
        st = fresh_state(bars[:897], bars[896][0] + HOUR, q)
        eng = lab.LabEngine(st, now, bars, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0)
        rows = {r["signal_ts"]: r for r in eng.opps if r["strategy"] == self.SID}
        row = rows[bars[897][0]]
        self.assertEqual(row["decision"], "missed")
        self.assertEqual(row["reason"], "stale_past_entry_deadline")
        self.assertTrue(row["observed_late"])
        self.assertEqual(a["last_parent_signal_ts"], bars[897][0])   # cooldown still consumed

    def test_exit_due_without_quote_stays_pending_never_fabricated(self):
        bars = breakout_bars(spike_at=899, spike_vol=50.0)
        now = bars[-1][0] + HOUR + 120
        mid = bars[-1][4]
        q = quote(bid=mid - .05, ask=mid + .05, now=now)
        eng, st = self.run_engine(bars, now, q)
        a = st["accounts"][self.SID]
        due = a["exit_due_ts"]
        eng2 = lab.LabEngine(st, due + 300, bars, None)      # quote outage at exit time
        eng2.run()
        self.assertGreater(a["qty"], 0)
        self.assertEqual(a["pending"][0]["kind"], "exit_unresolved")
        # quote returns: exit executes, pending clears, lateness recorded
        q3 = quote(bid=mid - .05, ask=mid + .05, now=due + 3900)
        eng3 = lab.LabEngine(st, due + 3900, bars, q3)
        eng3.run()
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["pending"], [])
        self.assertEqual(eng3.orders[0]["lateness_sec"], 3900)

    def test_duplicate_dispatch_is_noop(self):
        bars = breakout_bars(spike_at=899, spike_vol=50.0)
        now = bars[-1][0] + HOUR + 120
        mid = bars[-1][4]
        q = quote(bid=mid - .05, ask=mid + .05, now=now)
        eng, st = self.run_engine(bars, now, q)
        n_orders = sum(a["order_count"] for a in st["accounts"].values())
        snap1 = len(eng.snapshots)
        self.assertGreater(snap1, 0)
        # same hour, same committed state, run again (manual re-dispatch)
        eng2 = lab.LabEngine(st, now + 120, bars, q)
        eng2.run()
        self.assertEqual(sum(a["order_count"] for a in st["accounts"].values()), n_orders)
        self.assertEqual(len(eng2.orders), 0)
        self.assertEqual(len(eng2.snapshots), 0)   # equity rows deduped per hour too

    def test_paused_blocks_new_entry_but_not_exit(self):
        bars = breakout_bars(spike_at=899, spike_vol=50.0)
        now = bars[-1][0] + HOUR + 120
        mid = bars[-1][4]
        q = quote(bid=mid - .05, ask=mid + .05, now=now)
        st = fresh_state(bars[:-1], bars[-2][0] + HOUR, q)
        st["accounts"][self.SID]["paused"] = True
        eng = lab.LabEngine(st, now, bars, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0)
        rows = [r for r in eng.opps if r["strategy"] == self.SID]
        self.assertEqual(rows[-1]["reason"], "paused")
        self.assertEqual(a["last_parent_signal_ts"], bars[-1][0])   # schedule still advances

    def test_warmup_never_trades_pre_activation_candles(self):
        bars = breakout_bars(spike_at=880, spike_vol=50.0)
        now = bars[-1][0] + HOUR + 120
        q = quote(bid=100 - .05, ask=100 + .05, now=now)
        st = fresh_state(bars, bars[-1][0] + HOUR, q)        # activation AFTER the spike
        eng = lab.LabEngine(st, now, bars, q)
        eng.run()
        a = st["accounts"][self.SID]
        self.assertEqual(a["order_count"], 0)
        self.assertEqual(a["last_parent_signal_ts"], 0)      # fresh prospective schedule
        self.assertEqual([r for r in eng.opps if r["strategy"] == self.SID], [])


class TestClosedCandleFilter(unittest.TestCase):
    def test_unfinished_candles_are_excluded(self):
        # fetch_candles filters on b[0]+3600 <= now; emulate its filter directly
        now = T0 + 10 * HOUR + 1800
        bars = [b for b in mk_bars(11) if b[0] + HOUR <= now]
        self.assertEqual(bars[-1][0], T0 + 9 * HOUR)


if __name__ == "__main__":
    unittest.main()
