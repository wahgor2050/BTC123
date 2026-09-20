"""Behavior tests for forward-lab candidate C (funding_z168_limit_entry_v1).

Deterministic fixtures, no network. Covers the frozen spec in
btc_demo/lab/funding_candidate_spec.md: z-score math (prefix invariance,
first-valid index, degenerate window), settlement-to-bar mapping with the 6h
staleness rule, resting-limit-order placement/fill/expiry/cancel (including
the cancel-before-fill tie-break and the partially-pre-placement candle
exclusion), maker-fee entry vs taker-model exit, the two-data-source
weather rules (fetch failure holds within 6h of last-known settlement,
forces an exit beyond it), restart/duplicate-dispatch safety for the
resting-order state, pause-cancels-resting, later-added account migration,
and the frozen A/B parameter hashes staying untouched.
"""
import json
import math
import os
import shutil
import statistics
import tempfile
import unittest

import btc_forward_lab as lab
import btc_funding_signal as fsig

HOUR = 3600
T0 = 1_790_000_000 - (1_790_000_000 % HOUR)
SID = "funding_z168_limit_entry_v1"
A_SID = "sma_1d_30d_v25_v1"
B_SID = "rvol2_breakout24h_v25_v1"


def mk_bars(n, price=100.0, vol=10.0, start=T0):
    return [[start + i * HOUR, price, price, price, price, vol] for i in range(n)]


def quote(bid=99.9, ask=100.1, ts=None, now=None):
    now = now if now is not None else 0
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
            "exchange_ts": ts or int(now), "request_ts": float(now),
            "receipt_ts": float(now), "quote_size": None}


def fctx(bars, z_by_ts=None, ok=True, last_known=None, default_z=-1.0):
    """Funding context with one settlement at every bar open. default_z=-1.0
    is the sticky hold zone: no entry, no exit, no cancel."""
    z_by_ts = z_by_ts or {}
    f_times = [b[0] for b in bars]
    f_z = [z_by_ts.get(t, default_z) for t in f_times]
    return {"ok": ok, "f_times": f_times, "f_z": f_z,
            "f_rates": [1e-6] * len(f_times),
            "last_known_ts": last_known if last_known is not None
            else (f_times[-1] if f_times else None)}


class TestZScoreMath(unittest.TestCase):
    def test_first_valid_index_and_reference_math(self):
        import random
        rng = random.Random(11)
        rates = [rng.gauss(0, 1e-6) for _ in range(400)]
        z = fsig.settlement_zscores(rates)
        first = next(i for i, v in enumerate(z) if v is not None)
        self.assertEqual(first, fsig.Z_WINDOW)
        k = 300
        win = rates[k - 168:k]
        expect = (rates[k] - statistics.mean(win)) / statistics.stdev(win)
        self.assertAlmostEqual(z[k], expect, places=12)

    def test_prefix_invariance_no_lookahead(self):
        import random
        rng = random.Random(5)
        rates = [rng.gauss(0, 1e-6) for _ in range(500)]
        z = fsig.settlement_zscores(rates)
        mut = list(rates)
        for m in range(340, 500):
            mut[m] = 99.0
        z_mut = fsig.settlement_zscores(mut)
        self.assertTrue(all(z[i] == z_mut[i] for i in range(340)))
        self.assertNotEqual(z[340], z_mut[340])   # non-vacuous

    def test_degenerate_window_is_none(self):
        rates = [1e-6] * 300
        z = fsig.settlement_zscores(rates)
        self.assertTrue(all(v is None for v in z))

    def test_bar_mapping_and_staleness(self):
        f_times = [T0, T0 + HOUR, T0 + 2 * HOUR]
        f_z = [0.1, 0.2, 0.3]
        z, s = fsig.z_at(T0 + HOUR + 10, f_times, f_z)      # newest at/<= open
        self.assertEqual((z, s), (0.2, T0 + HOUR))
        z, s = fsig.z_at(T0 + 2 * HOUR, f_times, f_z)       # exact boundary counts
        self.assertEqual((z, s), (0.3, T0 + 2 * HOUR))
        z, s = fsig.z_at(T0 - 1, f_times, f_z)              # before first settlement
        self.assertEqual((z, s), (None, None))
        z, s = fsig.z_at(T0 + 2 * HOUR + 6 * 3600 + 1, f_times, f_z)   # > 6h stale
        self.assertEqual((z, s), (None, T0 + 2 * HOUR))


class FundingEngineBase(unittest.TestCase):
    def fresh(self, n=60):
        bars = mk_bars(n)
        now0 = bars[-1][0] + HOUR
        st = lab.new_state(now0, bars, quote(now=now0))
        return bars, st

    def acct(self, st):
        return st["accounts"][SID]

    def run_eng(self, st, bars, now, q, fu):
        eng = lab.LabEngine(st, now, bars, q, funding=fu)
        eng.run()
        return eng

    def place_order(self, n=60):
        """Fresh state + one new candle whose z<=-2 -> resting order placed."""
        bars, st = self.fresh(n)
        nb = bars + [[bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        now = nb[-1][0] + HOUR + 120
        q = quote(now=now)
        fu = fctx(nb, {nb[-1][0]: -2.5})
        eng = self.run_eng(st, nb, now, q, fu)
        return nb, st, now, q, eng


class TestEntryPlacement(FundingEngineBase):
    def test_fresh_signal_places_resting_order_at_bid(self):
        nb, st, now, q, eng = self.place_order()
        a = self.acct(st)
        r = a["resting"]
        self.assertIsNotNone(r)
        self.assertEqual(r["limit_px"], q["bid"])
        self.assertAlmostEqual(r["qty"], a["cash"] / (q["bid"] * 1.0002), places=10)
        self.assertEqual(r["deadline_ts"], int(now) + 4 * HOUR)
        self.assertEqual(a["order_count"], 0)          # placement is NOT a fill
        rows = [o for o in eng.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["decision"], "resting_placed")
        self.assertTrue(any("掛限價買單" in s for s in eng.status_alerts))
        self.assertTrue(any(fr["action"] == "resting_placed" for fr in eng.funding_rows))

    def test_stale_signal_is_missed_never_backfilled(self):
        bars, st = self.fresh()
        nb = bars + [[bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        now = nb[-1][0] + HOUR + 45 * 60                # 45 min: past deadline
        eng = self.run_eng(st, nb, now, quote(now=now), fctx(nb, {nb[-1][0]: -2.5}))
        a = self.acct(st)
        self.assertIsNone(a["resting"])
        rows = [o for o in eng.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["decision"], "missed")
        self.assertEqual(rows[-1]["reason"], "stale_past_entry_deadline")

    def test_hold_zone_and_no_quote_and_paused(self):
        bars, st = self.fresh()
        nb = bars + [[bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        now = nb[-1][0] + HOUR + 120
        # hold zone: -2 < z < 0 -> nothing at all
        eng = self.run_eng(st, nb, now, quote(now=now), fctx(nb, {nb[-1][0]: -1.2}))
        self.assertIsNone(self.acct(st)["resting"])
        self.assertEqual([o for o in eng.opps if o["strategy"] == SID], [])
        # no quote -> missed
        bars2, st2 = self.fresh()
        nb2 = bars2 + [[bars2[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        eng2 = self.run_eng(st2, nb2, now, None, fctx(nb2, {nb2[-1][0]: -2.5}))
        rows = [o for o in eng2.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["reason"], "no_executable_quote")
        # paused -> missed
        bars3, st3 = self.fresh()
        st3["accounts"][SID]["paused"] = True
        nb3 = bars3 + [[bars3[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        eng3 = self.run_eng(st3, nb3, now, quote(now=now), fctx(nb3, {nb3[-1][0]: -2.5}))
        rows = [o for o in eng3.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["reason"], "paused")

    def test_fetch_failure_logged_never_invented(self):
        bars, st = self.fresh()
        nb = bars + [[bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]]
        now = nb[-1][0] + HOUR + 120
        eng = self.run_eng(st, nb, now, quote(now=now), fctx(nb, ok=False))
        rows = [o for o in eng.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["reason"], "no_funding_data")
        self.assertIsNone(self.acct(st)["resting"])


class TestRestingLifecycle(FundingEngineBase):
    def test_partially_pre_placement_candle_never_fills(self):
        nb, st, now, q, eng = self.place_order()
        # candle containing placed_ts: open == placed-120s < placed_ts, its low
        # smashes through the limit — must NOT fill (would fabricate a fill)
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 50.0, 100.0, 10.0]
        nb2 = nb + [c1]
        now2 = c1[0] + HOUR + 120
        self.run_eng(st, nb2, now2, quote(now=now2), fctx(nb2))
        a = self.acct(st)
        self.assertEqual(a["qty"], 0.0)
        self.assertIsNotNone(a["resting"])              # still resting

    def test_fill_on_wholly_inside_candle_low_touch_maker_fee(self):
        nb, st, now, q, eng = self.place_order()
        r0 = dict(self.acct(st)["resting"])
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]   # partial: no touch
        c2 = [c1[0] + HOUR, 100.0, 100.2, 99.0, 99.5, 10.0]         # inside: touches
        nb2 = nb + [c1, c2]
        now2 = c2[0] + HOUR + 120
        eng2 = self.run_eng(st, nb2, now2, quote(bid=99.4, ask=99.6, now=now2), fctx(nb2))
        a = self.acct(st)
        self.assertIsNone(a["resting"])
        self.assertAlmostEqual(a["qty"], r0["qty"], places=10)
        buys = [o for o in eng2.orders if o["side"] == "BUY" and o["strategy"] == SID]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["reason"], "limit_fill_entry")
        self.assertAlmostEqual(buys[0]["fill_price"], r0["limit_px"], places=2)
        gross = r0["qty"] * r0["limit_px"]
        self.assertAlmostEqual(buys[0]["fee"], round(gross * 0.0002, 2), places=2)
        self.assertGreaterEqual(a["cash"], 0)
        ok, nav, unreal = lab.reconcile(a, 99.5)
        self.assertTrue(ok)
        self.assertEqual(a["entry_fill_ts"], c2[0] + HOUR)
        self.assertTrue(any("成交" in s for s in eng2.status_alerts))

    def test_expiry_after_deadline_no_touch(self):
        nb, st, now, q, eng = self.place_order()
        cs = []
        t = nb[-1][0]
        for k in range(1, 6):
            cs.append([t + k * HOUR, 100.0, 100.5, 99.95, 100.0, 10.0])  # never touches
        nb2 = nb + cs
        now2 = cs[-1][0] + HOUR + 120                   # > deadline (placed+4h)
        eng2 = self.run_eng(st, nb2, now2, quote(now=now2), fctx(nb2))
        a = self.acct(st)
        self.assertIsNone(a["resting"])
        self.assertEqual(a["qty"], 0.0)
        rows = [o for o in eng2.opps if o["strategy"] == SID]
        self.assertIn("expired_unfilled", [r["reason"] for r in rows])
        self.assertTrue(any("過期未成交" in s for s in eng2.status_alerts))

    def test_cancel_before_fill_tiebreak_z_reverted(self):
        nb, st, now, q, eng = self.place_order()
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        c2 = [c1[0] + HOUR, 100.0, 100.2, 99.0, 99.5, 10.0]   # touches AND z>=0
        nb2 = nb + [c1, c2]
        now2 = c2[0] + HOUR + 120
        eng2 = self.run_eng(st, nb2, now2, quote(now=now2),
                            fctx(nb2, {c2[0]: 0.4}))
        a = self.acct(st)
        self.assertEqual(a["qty"], 0.0)                 # cancel wins, no fill
        self.assertIsNone(a["resting"])
        rows = [o for o in eng2.opps if o["strategy"] == SID]
        self.assertIn("cancelled_z_reverted", [r["reason"] for r in rows])

    def test_fetch_failure_pauses_lifecycle_then_resolves(self):
        nb, st, now, q, eng = self.place_order()
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        c2 = [c1[0] + HOUR, 100.0, 100.2, 99.0, 99.5, 10.0]
        nb2 = nb + [c1, c2]
        now2 = c2[0] + HOUR + 120
        # funding down: no fill, no cancel, no expiry — order waits
        self.run_eng(st, nb2, now2, quote(now=now2), fctx(nb2, ok=False))
        self.assertIsNotNone(self.acct(st)["resting"])
        self.assertEqual(self.acct(st)["qty"], 0.0)
        # feed returns next run: the SAME candle fills deterministically
        eng3 = self.run_eng(st, nb2, now2 + HOUR, quote(now=now2 + HOUR), fctx(nb2))
        self.assertIsNone(self.acct(st)["resting"])
        self.assertGreater(self.acct(st)["qty"], 0)
        self.assertEqual(len([o for o in eng3.orders if o["strategy"] == SID]), 1)

    def test_no_repeg_while_resting(self):
        nb, st, now, q, eng = self.place_order()
        r0 = dict(self.acct(st)["resting"])
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb2 = nb + [c1]
        now2 = c1[0] + HOUR + 120
        eng2 = self.run_eng(st, nb2, now2, quote(bid=98.0, ask=98.2, now=now2),
                            fctx(nb2, {c1[0]: -3.0}))   # extreme again while resting
        a = self.acct(st)
        self.assertEqual(a["resting"]["limit_px"], r0["limit_px"])   # unchanged
        self.assertEqual(a["resting"]["placed_ts"], r0["placed_ts"])
        rows = [o for o in eng2.opps if o["strategy"] == SID]
        self.assertEqual(rows[-1]["reason"], "order_already_resting")

    def test_restart_and_duplicate_dispatch_cannot_double_fill(self):
        nb, st, now, q, eng = self.place_order()
        # simulate the state being committed then the process restarting
        st = json.loads(json.dumps(st))
        c1 = [nb[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        c2 = [c1[0] + HOUR, 100.0, 100.2, 99.0, 99.5, 10.0]
        nb2 = nb + [c1, c2]
        now2 = c2[0] + HOUR + 120
        pre_fill = json.loads(json.dumps(st))           # crash-before-save copy
        eng2 = self.run_eng(st, nb2, now2, quote(now=now2), fctx(nb2))
        self.assertEqual(self.acct(st)["order_count"], 1)
        # duplicate dispatch on the committed post-fill state: no second fill
        eng3 = self.run_eng(st, nb2, now2 + 120, quote(now=now2 + 120), fctx(nb2))
        self.assertEqual(self.acct(st)["order_count"], 1)
        self.assertEqual([o for o in eng3.orders if o["strategy"] == SID], [])
        self.assertEqual(eng3.snapshots, [])            # equity deduped too
        # crash-before-save replay: same candles, same single fill, same id
        eng4 = self.run_eng(pre_fill, nb2, now2 + HOUR, quote(now=now2 + HOUR), fctx(nb2))
        b2 = [o for o in eng2.orders if o["strategy"] == SID]
        b4 = [o for o in eng4.orders if o["strategy"] == SID]
        self.assertEqual(len(b4), 1)
        self.assertEqual(b2[0]["order_id"], b4[0]["order_id"])
        self.assertEqual(b2[0]["fill_price"], b4[0]["fill_price"])


class TestExit(FundingEngineBase):
    def holding_state(self):
        bars, st = self.fresh()
        a = self.acct(st)
        lab.apply_buy(a, 999.0, 100.0, fee_rate=0.0002)
        a["entry_fill_ts"] = bars[-1][0] + HOUR - HOUR   # long since before latest
        a["entry_event_id"] = "%s:%d" % (SID, bars[-2][0])
        a["last_entry_signal_ts"] = bars[-2][0]
        return bars, st

    def test_exit_on_z_cross_uses_taker_model(self):
        bars, st = self.holding_state()
        c1 = [bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb = bars + [c1]
        now = c1[0] + HOUR + 120
        q = quote(bid=101.0, ask=101.2, now=now)
        eng = self.run_eng(st, nb, now, q, fctx(nb, {c1[0]: 0.3}))
        a = self.acct(st)
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["episodes"], 1)
        sells = [o for o in eng.orders if o["side"] == "SELL" and o["strategy"] == SID]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "funding_exit_z")
        mid = q["mid"]
        expect_px = min(q["bid"], mid * (1 - lab.MIN_HALF_SPREAD))
        self.assertAlmostEqual(sells[0]["fill_price"], round(expect_px, 2), places=2)
        gross = 999.0 * expect_px
        self.assertAlmostEqual(sells[0]["fee"], round(gross * lab.FEE_RATE, 2), places=1)
        ok, nav, unreal = lab.reconcile(a, mid)
        self.assertTrue(ok)

    def test_exit_signal_without_quote_stays_pending_then_executes(self):
        bars, st = self.holding_state()
        c1 = [bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb = bars + [c1]
        now = c1[0] + HOUR + 120
        eng = self.run_eng(st, nb, now, None, fctx(nb, {c1[0]: 0.3}))
        a = self.acct(st)
        self.assertGreater(a["qty"], 0)
        self.assertEqual(a["pending"][0]["kind"], "exit_unresolved")
        self.assertEqual(a["exit_signal_ts"], c1[0])
        # quote returns: mandatory exit executes, lateness recorded
        now2 = now + 2 * HOUR
        eng2 = self.run_eng(st, nb, now2, quote(now=now2), fctx(nb, {c1[0]: 0.3}))
        self.assertEqual(a["qty"], 0.0)
        self.assertEqual(a["pending"], [])
        sells = [o for o in eng2.orders if o["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertGreater(sells[0]["lateness_sec"], 0)

    def test_stale_signal_exit_when_settlements_dry_up(self):
        bars, st = self.holding_state()
        # 8 new candles but the funding series stops at the old latest bar —
        # the mapped settlement goes >6h stale -> mandatory exit
        cs = [[bars[-1][0] + k * HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
              for k in range(1, 9)]
        nb = bars + cs
        now = cs[-1][0] + HOUR + 120
        fu = fctx(bars)                                  # settlements END at bars[-1]
        eng = self.run_eng(st, nb, now, quote(now=now), fu)
        a = self.acct(st)
        self.assertEqual(a["qty"], 0.0)
        sells = [o for o in eng.orders if o["side"] == "SELL"]
        self.assertEqual(sells[0]["reason"], "signal_stale_exit")

    def test_fetch_fail_recent_lastknown_holds(self):
        bars, st = self.holding_state()
        c1 = [bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb = bars + [c1]
        now = c1[0] + HOUR + 120
        fu = fctx(nb, ok=False, last_known=c1[0])        # last known is fresh
        eng = self.run_eng(st, nb, now, quote(now=now), fu)
        a = self.acct(st)
        self.assertGreater(a["qty"], 0)                  # holds, no invented exit
        self.assertEqual([o for o in eng.orders if o["strategy"] == SID], [])

    def test_fetch_fail_stale_lastknown_forces_exit(self):
        bars, st = self.holding_state()
        c1 = [bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb = bars + [c1]
        now = c1[0] + HOUR + 120
        fu = fctx(nb, ok=False, last_known=c1[0] - 7 * HOUR)   # > 6h stale
        eng = self.run_eng(st, nb, now, quote(now=now), fu)
        a = self.acct(st)
        self.assertEqual(a["qty"], 0.0)
        sells = [o for o in eng.orders if o["side"] == "SELL"]
        self.assertEqual(sells[0]["reason"], "signal_stale_exit")

    def test_missed_exit_crossing_caught_after_outage(self):
        bars, st = self.holding_state()
        c1 = [bars[-1][0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        c2 = [c1[0] + HOUR, 100.0, 100.5, 99.95, 100.0, 10.0]
        nb = bars + [c1, c2]
        # run 1: feed down at the z>=0 crossing candle — held (< 6h stale)
        now1 = c1[0] + HOUR + 120
        self.run_eng(st, nb[:len(bars) + 1], now1, quote(now=now1),
                     fctx(nb, ok=False, last_known=c1[0]))
        self.assertGreater(self.acct(st)["qty"], 0)
        # run 2: feed returns; c1's crossing must STILL trigger the exit even
        # though c1 is no longer the newest candle
        now2 = c2[0] + HOUR + 120
        eng2 = self.run_eng(st, nb, now2, quote(now=now2),
                            fctx(nb, {c1[0]: 0.5, c2[0]: -1.0}))
        self.assertEqual(self.acct(st)["qty"], 0.0)
        sells = [o for o in eng2.orders if o["side"] == "SELL"]
        self.assertEqual(sells[0]["reason"], "funding_exit_z")


class TestFrozenAndMigration(FundingEngineBase):
    def test_ab_param_hashes_untouched(self):
        self.assertEqual(lab.param_hash(lab.STRATEGIES[A_SID]), "cbc923b3bee843d3")
        self.assertEqual(lab.param_hash(lab.STRATEGIES[B_SID]), "b239ee4d1af534e8")

    def test_c_params_frozen(self):
        p = lab.STRATEGIES[SID]
        self.assertEqual(p["z_window"], 168)
        self.assertEqual(p["z_enter"], -2.0)
        self.assertEqual(p["z_exit"], 0.0)
        self.assertEqual(p["stale_limit_sec"], 21600)
        self.assertEqual(p["limit_deadline_sec"], 14400)
        self.assertEqual(p["maker_fee"], 0.0002)
        self.assertEqual(p["funding_per_day"], 24)

    def test_ensure_accounts_adds_c_with_own_epoch_and_is_idempotent(self):
        bars = mk_bars(40)
        now0 = bars[-1][0] + HOUR
        st = lab.new_state(now0, bars, quote(now=now0))
        del st["accounts"][SID]                     # emulate the pre-C live state
        for a in st["accounts"].values():           # strip C-era fields like live state
            a.pop("activation_ts", None)
        before = json.loads(json.dumps(st["accounts"]))
        now1 = now0 + 5 * 86400
        added = lab.ensure_accounts(st, now1, bars)
        self.assertEqual(added, [SID])
        c = st["accounts"][SID]
        self.assertEqual(c["activation_ts"], int(now1))
        self.assertEqual(c["last_signal_ts"], bars[-1][0])   # warmup: no back-trading
        for sid in (A_SID, B_SID):                  # A/B byte-identical
            self.assertEqual(st["accounts"][sid], before[sid])
        self.assertEqual(lab.ensure_accounts(st, now1 + HOUR, bars), [])
        self.assertEqual(lab.ensure_accounts({"accounts": {}}, now1, []), [])

    def test_pause_cancels_resting_order(self):
        nb, st, now, q, eng = self.place_order()
        tmp = tempfile.mkdtemp()
        old = (lab.LAB_DIR, lab.STATE_FILE, lab.SUMMARY_FILE)
        try:
            lab.LAB_DIR = tmp
            lab.STATE_FILE = os.path.join(tmp, "state.json")
            lab.SUMMARY_FILE = os.path.join(tmp, "summary.json")
            lab.save_state(st)
            self.assertEqual(lab.set_paused(SID, True), 0)
            st2 = lab.load_state()
            self.assertTrue(st2["accounts"][SID]["paused"])
            self.assertIsNone(st2["accounts"][SID]["resting"])
            opp_files = [f for f in os.listdir(tmp) if f.startswith("opportunities_")]
            self.assertEqual(len(opp_files), 1)
            with open(os.path.join(tmp, opp_files[0])) as fh:
                body = fh.read()
            self.assertIn("cancelled_paused", body)
        finally:
            lab.LAB_DIR, lab.STATE_FILE, lab.SUMMARY_FILE = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_nav_sizing_never_goes_negative_on_fill(self):
        nb, st, now, q, eng = self.place_order()
        a = self.acct(st)
        r = a["resting"]
        lab.apply_buy(a, r["qty"], r["limit_px"], fee_rate=0.0002)
        self.assertGreaterEqual(a["cash"], 0.0)
        self.assertAlmostEqual(a["cash"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
