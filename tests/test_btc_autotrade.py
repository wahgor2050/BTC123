#!/usr/bin/env python3
"""
Offline tests for the BTC paper-trading demo.

They never touch the network: a deterministic synthetic candle series is fed
straight into the engine, so CI can prove the accounting, the indicators and
the dashboard rendering still work even when an exchange API is unreachable.

    python -m unittest discover -s tests -v
"""

import json
import logging
import math
import random
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import btc_autotrade as bot  # noqa: E402

bot.logger.setLevel(logging.CRITICAL)  # keep the test output readable


def synthetic_bars(n=4000, start=60000.0, seed=7, granularity=3600):
    """Random walk with a few regime changes so the strategy actually trades."""
    rng = random.Random(seed)
    t0 = int(time.time()) - n * granularity
    price, bars = start, []
    for i in range(n):
        drift = 0.0012 if (i // 700) % 2 == 0 else -0.0010   # alternating trends
        price *= math.exp(drift + rng.gauss(0, 0.004))
        o = price * (1 + rng.gauss(0, 0.0008))
        c = price
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0015)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.0015)))
        bars.append([t0 + i * granularity, round(o, 2), round(h, 2),
                     round(l, 2), round(c, 2), round(abs(rng.gauss(30, 8)), 3)])
    return bars


class IndicatorTests(unittest.TestCase):
    def test_sma_of_a_constant_series_is_that_constant(self):
        out = bot.sma_series([100.0] * 50, 12)
        self.assertTrue(all(v is None for v in out[:11]))
        self.assertTrue(all(abs(v - 100.0) < 1e-9 for v in out[11:]))

    def test_sma_needs_a_full_window(self):
        out = bot.sma_series([float(i) for i in range(10)], 4)
        self.assertIsNone(out[2])
        self.assertAlmostEqual(out[3], 1.5)      # (0+1+2+3)/4
        self.assertAlmostEqual(out[9], 7.5)      # (6+7+8+9)/4

    def test_volatility_is_zero_for_a_flat_price(self):
        out = bot.vol_series([100.0] * 100, 24)
        self.assertTrue(all(abs(v) < 1e-9 for v in out[23:]))

    def test_volatility_rises_with_choppier_prices(self):
        calm = bot.vol_series([100.0 * (1.0001 ** i) for i in range(500)], 168)
        wild = [100.0]
        for i in range(1, 500):
            wild.append(wild[-1] * (1.03 if i % 2 else 0.97))
        rough = bot.vol_series(wild, 168)
        self.assertLess(calm[-1], rough[-1])

    def test_target_weight_is_flat_without_an_uptrend(self):
        bars = [[i * 3600, 100.0, 100.0, 100.0, 100.0 - i * 0.01, 1.0] for i in range(2000)]
        want, _, _, _ = bot.target_weights(bars, bot.CONFIG)
        self.assertTrue(all(w == 0.0 for w in want), "went long into a falling price")

    def test_target_weight_never_exceeds_fully_invested(self):
        want, _, _, _ = bot.target_weights(synthetic_bars(4000), bot.CONFIG)
        self.assertLessEqual(max(want), 1.0)
        self.assertGreaterEqual(min(want), 0.0)

    def test_calmer_markets_get_a_bigger_position(self):
        """Volatility targeting: same trend, less noise, more exposure."""
        cfg = dict(bot.CONFIG)
        def ramp(noise, seed):
            rng = random.Random(seed)
            px, out = 100.0, []
            for i in range(4000):
                px *= math.exp(0.0004 + rng.gauss(0, noise))
                out.append([i * 3600, px, px * 1.001, px * 0.999, px, 1.0])
            return out
        calm, _, _, _ = bot.target_weights(ramp(0.001, 1), cfg)
        wild, _, _, _ = bot.target_weights(ramp(0.010, 1), cfg)
        calm_avg = sum(calm[-500:]) / 500
        wild_avg = sum(wild[-500:]) / 500
        self.assertGreater(calm_avg, wild_avg,
                           "a calmer market should earn a larger position, not a smaller one")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="btc-demo-test-")
        self._saved = (bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD)
        bot.OUT_DIR = self.tmp
        bot.STATE_FILE = os.path.join(self.tmp, "state.json")
        bot.TRADES_CSV = os.path.join(self.tmp, "trades.csv")
        bot.DASHBOARD = os.path.join(self.tmp, "index.html")
        self.cfg = dict(bot.CONFIG)
        self.bars = synthetic_bars(4000, granularity=self.cfg["granularity"])

    def tearDown(self):
        bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_engine(self, bars=None, state=None):
        state = state or bot.new_state(self.cfg)
        engine = bot.PaperEngine(state, self.cfg)
        processed = engine.process(bars or self.bars)
        engine.update_stats((bars or self.bars)[-1][4])
        return state, processed

    def test_engine_takes_trades_and_records_signals(self):
        state, processed = self.run_engine()
        self.assertGreater(processed, 3000)
        self.assertGreater(len(state["trades"]), 0, "strategy never traded on trending data")
        self.assertTrue(any(s["kind"] == "BUY" for s in state["signals"]))

    def test_cash_never_goes_negative_and_equity_adds_up(self):
        state, _ = self.run_engine()
        self.assertGreaterEqual(state["cash"], -1e-6)
        engine = bot.PaperEngine(state, self.cfg)
        price = self.bars[-1][4]
        expected = state["cash"] + state["qty"] * price
        self.assertAlmostEqual(engine.equity(price), expected, places=6)
        self.assertAlmostEqual(state["stats"]["equity"], round(expected, 2), places=2)

    def test_never_holds_more_than_the_account(self):
        state, _ = self.run_engine()
        for ts, eq in state["equity_curve"]:
            self.assertGreater(eq, 0, "the paper account went to zero or below")
        self.assertLessEqual(state["stats"]["invested_pct"], 100.5)

    def test_position_is_scaled_not_all_or_nothing(self):
        """
        The whole point of volatility targeting is partial positions.

        Needs a market wilder than the risk budget, otherwise the cap at fully
        invested hides the sizing entirely — which is exactly what happened the
        first time this was written against the calm default series.
        """
        rng = random.Random(11)
        px, bars = 100.0, []
        for i in range(4000):
            px *= math.exp(0.0006 + rng.gauss(0, 0.02))      # ~190% annualised
            bars.append([i * 3600, px, px * 1.004, px * 0.996, px, 1.0])
        want, _, _, vol = bot.target_weights(bars, self.cfg)
        weights = [w for w in want if w > 0]
        self.assertTrue(weights, "never wanted to be long at all")
        self.assertGreater(max(x for x in vol if x is not None), self.cfg["target_vol"],
                           "the test series is not wild enough to exercise sizing")
        self.assertTrue(any(w < 0.99 for w in weights),
                        "every position was all-in; volatility sizing is not doing anything")
        self.assertLessEqual(max(weights), 1.0)

    def test_rerunning_the_same_bars_books_nothing_twice(self):
        state, first = self.run_engine()
        trades_before, cash_before = len(state["trades"]), state["cash"]
        _, second = self.run_engine(state=state)
        self.assertEqual(second, 0, "already-closed candles were processed again")
        self.assertEqual(len(state["trades"]), trades_before)
        self.assertEqual(state["cash"], cash_before)

    def test_result_is_identical_however_often_the_job_runs(self):
        """
        The cron cadence must not change the trades.

        Whether the workflow fires every 15 minutes or every 2 hours, the engine
        replays every candle that closed since the last run, so the paper account
        has to come out the same. Only the dashboard is fresher.
        """
        every_bar, _ = self.run_engine()                      # as if it ran constantly
        chunked = bot.new_state(self.cfg)                     # as if it ran occasionally
        for cut in range(1000, len(self.bars) + 1, 600):      # ~25 days between runs
            engine = bot.PaperEngine(chunked, self.cfg)
            engine.process(self.bars[:cut])
        engine = bot.PaperEngine(chunked, self.cfg)
        engine.process(self.bars)
        engine.update_stats(self.bars[-1][4])

        self.assertEqual(len(chunked["trades"]), len(every_bar["trades"]),
                         "a different run cadence produced a different number of trades")
        self.assertAlmostEqual(chunked["cash"], every_bar["cash"], places=6)
        self.assertEqual(chunked["last_bar_ts"], every_bar["last_bar_ts"])
        for a, b in zip(chunked["trades"], every_bar["trades"]):
            self.assertEqual((a["entry_time"], a["exit_time"], a["reason"]),
                             (b["entry_time"], b["exit_time"], b["reason"]))
            self.assertAlmostEqual(a["pnl"], b["pnl"], places=6)

    def test_new_candles_resume_where_the_last_run_stopped(self):
        state, _ = self.run_engine(self.bars[:2500])
        last_ts = state["last_bar_ts"]
        _, more = self.run_engine(self.bars, state=state)
        self.assertGreater(more, 0)
        self.assertGreater(state["last_bar_ts"], last_ts)

    def test_state_survives_a_save_load_round_trip(self):
        state, _ = self.run_engine()
        bot.save_state(state)
        reloaded = bot.load_state(self.cfg)
        self.assertEqual(reloaded["last_bar_ts"], state["last_bar_ts"])
        self.assertEqual(len(reloaded["trades"]), len(state["trades"]))
        self.assertAlmostEqual(reloaded["cash"], state["cash"], places=6)

    def test_reset_starts_a_clean_account(self):
        state, _ = self.run_engine()
        bot.save_state(state)
        fresh = bot.load_state(self.cfg, reset=True)
        self.assertEqual(fresh["cash"], self.cfg["start_cash"])
        self.assertEqual(fresh["trades"], [])
        self.assertEqual(fresh["qty"], 0.0)

    def test_trades_csv_matches_the_state(self):
        state, _ = self.run_engine()
        bot.write_trades_csv(state)
        with open(bot.TRADES_CSV, encoding="utf-8") as fh:
            rows = fh.read().strip().splitlines()
        self.assertEqual(len(rows), len(state["trades"]) + 1)
        self.assertTrue(rows[0].startswith("entry_time,exit_time,side"))

    def test_dashboard_renders_valid_embedded_json(self):
        state, _ = self.run_engine()
        bot.render_dashboard(state)
        with open(bot.DASHBOARD, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("__PAYLOAD__", html, "payload placeholder was not substituted")
        self.assertIn("PAPER / DEMO", html)
        blob = html.split("const D = ", 1)[1].split(";\nconst NS", 1)[0]
        payload = json.loads(blob)                       # must be parseable JSON
        self.assertEqual(payload["stats"]["equity"], state["stats"]["equity"])
        self.assertEqual(len(payload["bars"]), len(state["bars"]))

    def test_no_broker_credentials_or_order_endpoints_exist(self):
        """The demo must stay a simulation: nothing that could reach a real broker."""
        with open(os.path.join(os.path.dirname(bot.__file__), "btc_autotrade.py"), encoding="utf-8") as fh:
            src = fh.read().lower()
        for forbidden in ["ib_insync", "ibapi", "api_key", "api_secret", "/order", "private/", "127.0.0.1:7497"]:
            self.assertNotIn(forbidden, src, "found broker/order surface %r in the demo" % forbidden)


class CommandLineTests(unittest.TestCase):
    """
    Exercise the entry points, not just the engine.

    A strategy change once left dry_run() calling ema_series, which no longer
    existed. Nothing imported it, no test touched it, and the workflow step
    that ran it was marked continue-on-error — so a NameError rode into the
    repository under a green tick. These call the CLI paths with a fake feed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-")
        self.saved = (bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD, bot.http_json)
        bot.OUT_DIR = self.tmp
        bot.STATE_FILE = os.path.join(self.tmp, "state.json")
        bot.TRADES_CSV = os.path.join(self.tmp, "trades.csv")
        bot.DASHBOARD = os.path.join(self.tmp, "index.html")

    def tearDown(self):
        (bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV,
         bot.DASHBOARD, bot.http_json) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _feed(self, n=1200):
        gran = bot.CONFIG["granularity"]
        bars = synthetic_bars(n, granularity=gran)
        now = int(time.time()) // gran * gran
        bars = [[now - (len(bars) - i) * gran] + b[1:] for i, b in enumerate(bars)]

        def fake(url, timeout=20):
            if "bitstamp" not in url:
                raise RuntimeError("blocked")
            return {"data": {"ohlc": [{"timestamp": str(b[0]), "open": b[1], "high": b[2],
                                       "low": b[3], "close": b[4], "volume": b[5]}
                                      for b in bars]}}
        bot.http_json = fake
        return bars

    def test_dry_run_reads_the_feed_and_writes_nothing(self):
        self._feed()
        before = sorted(os.listdir(self.tmp))
        self.assertEqual(bot.main(["--dry-run"]), 0)
        self.assertEqual(sorted(os.listdir(self.tmp)), before,
                         "--dry-run wrote to disk")

    def test_dry_run_survives_every_exchange_being_down(self):
        bot.http_json = lambda url, timeout=20: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertEqual(bot.main(["--dry-run"]), 0,
                         "an unreachable exchange is weather, not a defect")

    def test_dry_run_fails_when_the_feed_is_too_short_for_the_strategy(self):
        """
        A feed the strategy cannot be computed on is a defect, not weather.

        The window is widened here because the fetcher already rejects a feed
        under 30 bars as unusable, which makes the strategy-level check
        unreachable at the default 30-bar average.
        """
        saved = bot.CONFIG["sma_slow"]
        bot.CONFIG["sma_slow"] = 400
        try:
            self._feed(n=100)
            self.assertEqual(bot.main(["--dry-run"]), 1)
        finally:
            bot.CONFIG["sma_slow"] = saved

    def test_a_full_pass_writes_state_csv_and_dashboard(self):
        self._feed()
        self.assertEqual(bot.main([]), 0)
        for name in ("state.json", "trades.csv", "index.html"):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, name)), "%s missing" % name)
        with open(bot.STATE_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertTrue(st["last_run"]["ok"])
        self.assertIn("target_weight", st)

    def test_summary_runs_before_and_after_a_pass(self):
        self.assertEqual(bot.main(["--summary"]), 0)   # no state yet
        self._feed()
        bot.main([])
        self.assertEqual(bot.main(["--summary"]), 0)

    def test_reset_starts_the_account_over(self):
        """
        --reset must discard the old account, not extend it.

        The earlier version of this asserted the trade list was empty
        afterwards, which only held because the old configuration never
        produced a signal on this fixture. --reset replays the same history
        into a fresh account, so the honest assertion is that the history is
        replaced rather than appended to.
        """
        self._feed()
        bot.main([])
        with open(bot.STATE_FILE, encoding="utf-8") as fh:
            first = json.load(fh)
        bot.main([])                       # a second pass adds nothing new
        with open(bot.STATE_FILE, encoding="utf-8") as fh:
            again = json.load(fh)
        self.assertEqual(len(again["trades"]), len(first["trades"]))
        self.assertEqual(len(again["equity_curve"]), len(first["equity_curve"]))

        marked = dict(again)
        marked["trades"] = again["trades"] + [{"entry_time": "SENTINEL", "exit_time": "SENTINEL",
                                               "side": "long", "qty": 1.0, "entry_price": 1.0,
                                               "exit_price": 1.0, "fees": 0.0, "pnl": 0.0,
                                               "pnl_pct": 0.0, "reason": "sentinel",
                                               "bars_held": 0}]
        with open(bot.STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(marked, fh)

        bot.main(["--reset"])
        with open(bot.STATE_FILE, encoding="utf-8") as fh:
            fresh = json.load(fh)
        self.assertNotIn("sentinel", [t["reason"] for t in fresh["trades"]],
                         "--reset carried the old account forward")
        self.assertEqual(len(fresh["trades"]), len(first["trades"]),
                         "a reset replay should reproduce the same history, not double it")


class BacktestParityTests(unittest.TestCase):
    """
    The live bot and the backtest must be the same strategy.

    A backtest only means something if it describes the thing that actually
    runs. These two have separate code — the bot trades incrementally and
    persists state between runs, the lab replays in one batch — so nothing
    stops them drifting apart except a test that compares them.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            import btc_backtest as lab
        except ImportError:                       # pragma: no cover
            raise unittest.SkipTest("btc_backtest not importable")
        cls.lab = lab
        try:
            hourly = lab.load_history()
        except (FileNotFoundError, OSError):
            raise unittest.SkipTest("no cached history")
        cls.cfg = dict(bot.CONFIG)
        # the cache is hourly; roll it up to whatever candle the bot trades
        step = max(1, cls.cfg["granularity"] // 3600)
        if step > 1:
            rolled = []
            for i in range(0, len(hourly) - step + 1, step):
                g = hourly[i:i + step]
                rolled.append([g[0][0], g[0][1], max(b[2] for b in g),
                               min(b[3] for b in g), g[-1][4], sum(b[5] for b in g)])
            hourly = rolled
        cls.bars = hourly[-1500:]
        # the lab annualises volatility from bars-per-day, so tell it the candle size
        lab.BARS_PER_DAY = 86400.0 / cls.cfg["granularity"]
        cls.params = {"fast": cls.cfg["sma_fast"], "slow": cls.cfg["sma_slow"],
                      "target_vol": cls.cfg["target_vol"], "vol_window": cls.cfg["vol_window"]}

    def test_the_two_agree_on_what_the_strategy_wants(self):
        mine, _, _, _ = bot.target_weights(self.bars, self.cfg)
        theirs = self.lab.STRATEGIES["dual_sma_voltarget"][0](
            self.lab.Indicators(self.bars), self.params)
        bad = [i for i in range(len(self.bars))
               if abs(float(mine[i]) - float(theirs[i])) > 1e-9]
        self.assertEqual(bad, [], "bot and lab disagree on the target weight at %s" % bad[:5])

    def test_the_two_agree_on_the_resulting_equity(self):
        tmp = tempfile.mkdtemp(prefix="parity-")
        saved = (bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD)
        bot.OUT_DIR = tmp
        bot.STATE_FILE = os.path.join(tmp, "s.json")
        bot.TRADES_CSV = os.path.join(tmp, "t.csv")
        bot.DASHBOARD = os.path.join(tmp, "i.html")
        try:
            state = bot.new_state(self.cfg)
            engine = bot.PaperEngine(state, self.cfg)
            engine.process(self.bars)
            engine.update_stats(self.bars[-1][4])

            want = self.lab.STRATEGIES["dual_sma_voltarget"][0](
                self.lab.Indicators(self.bars), self.params)
            eq, trades = self.lab.backtest(
                self.bars, want, fee_bps=self.cfg["fee_bps"],
                slip_bps=self.cfg["slippage_bps"], start_cash=self.cfg["start_cash"],
                rebalance_band=self.cfg["rebalance_band"],
                dd_limit=self.cfg["dd_limit"], dd_throttle=self.cfg["dd_throttle"],
                dd_recover=self.cfg["dd_recover"])

            drift = abs(state["stats"]["equity"] - eq[-1]) / eq[-1]
            self.assertLess(drift, 0.005,
                            "equity diverged by %.3f%% — bot $%.2f vs lab $%.2f"
                            % (drift * 100, state["stats"]["equity"], eq[-1]))
            self.assertEqual(len(state["trades"]), len(trades),
                             "different number of trades: bot %d, lab %d"
                             % (len(state["trades"]), len(trades)))
        finally:
            bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD = saved
            shutil.rmtree(tmp, ignore_errors=True)


class CircuitBreakerTests(unittest.TestCase):
    """The account-level brake: it is the one rule that is not in the signal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brake-")
        self.saved = (bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD)
        bot.OUT_DIR = self.tmp
        bot.STATE_FILE = os.path.join(self.tmp, "s.json")
        bot.TRADES_CSV = os.path.join(self.tmp, "t.csv")
        bot.DASHBOARD = os.path.join(self.tmp, "i.html")
        self.bars = synthetic_bars(4000, granularity=bot.CONFIG["granularity"])

    def tearDown(self):
        bot.OUT_DIR, bot.STATE_FILE, bot.TRADES_CSV, bot.DASHBOARD = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **over):
        cfg = dict(bot.CONFIG, **over)
        st = bot.new_state(cfg)
        eng = bot.PaperEngine(st, cfg)
        eng.process(self.bars)
        eng.update_stats(self.bars[-1][4])
        return st

    def test_the_brake_reduces_the_drawdown(self):
        """
        Trip below whatever this fixture actually reaches.

        The default 15% trip never fires here — the synthetic series only draws
        down 14.2% — which is the brake behaving correctly, not a bug, so the
        test has to set a level the fixture can cross before it can observe
        anything at all.
        """
        off = self._run(dd_limit=0.0)
        reached = off["stats"]["max_drawdown_pct"]
        self.assertGreater(reached, 2.0, "fixture never drew down; nothing to brake")
        on = self._run(dd_limit=reached / 200.0, dd_throttle=0.25, dd_recover=0.01)
        self.assertLess(on["stats"]["max_drawdown_pct"], reached,
                        "the brake did not reduce the drawdown at all")

    def test_the_brake_stays_out_of_the_way_when_it_is_never_tripped(self):
        """A trip level the account never reaches must change nothing."""
        off = self._run(dd_limit=0.0)
        high = self._run(dd_limit=0.95, dd_throttle=0.1, dd_recover=0.5)
        self.assertAlmostEqual(off["stats"]["equity"], high["stats"]["equity"], places=6)
        self.assertNotIn("BRAKE", [s["kind"] for s in high["signals"]])

    def test_switching_it_off_changes_nothing(self):
        a = self._run(dd_limit=0.0)
        b = self._run(dd_limit=0.0, dd_throttle=0.1)
        self.assertEqual(a["stats"]["equity"], b["stats"]["equity"])

    def test_the_peak_never_goes_backwards(self):
        st = self._run(dd_limit=0.15, dd_throttle=0.25, dd_recover=0.05)
        self.assertGreaterEqual(st["peak_equity"], bot.CONFIG["start_cash"])
        best = max(e[1] for e in st["equity_curve"])
        self.assertAlmostEqual(st["peak_equity"], best, delta=max(1.0, best * 0.001))

    def test_it_trips_and_clears_rather_than_latching_forever(self):
        st = self._run(dd_limit=0.05, dd_throttle=0.25, dd_recover=0.01)
        kinds = [s["kind"] for s in st["signals"]]
        self.assertIn("BRAKE", kinds, "a 5% trip never fired on a volatile series")
        self.assertIn("CLEAR", kinds, "the brake latched on and never released")

    def test_a_deeper_brake_never_holds_more_than_a_shallower_one(self):
        quarter = self._run(dd_limit=0.15, dd_throttle=0.25, dd_recover=0.05)
        half = self._run(dd_limit=0.15, dd_throttle=0.50, dd_recover=0.05)
        self.assertLessEqual(quarter["stats"]["max_drawdown_pct"],
                             half["stats"]["max_drawdown_pct"] + 1e-6)


class DataParsingTests(unittest.TestCase):
    """Parsers are exercised with recorded payload shapes — still no network."""

    def _check(self, bars):
        self.assertTrue(bars)
        for ts, o, h, l, c, v in bars:
            self.assertIsInstance(ts, int)
            self.assertGreaterEqual(h, max(o, c))
            self.assertLessEqual(l, min(o, c))
            self.assertGreaterEqual(v, 0)
        self.assertEqual([b[0] for b in bars], sorted(b[0] for b in bars))

    def test_coinbase_shape(self):
        payload = [[1700000900, 36900.0, 37100.0, 37000.0, 37050.0, 12.5],
                   [1700000000, 36800.0, 37050.0, 36900.0, 37000.0, 10.0]]
        bot.http_json = lambda url, timeout=20: payload
        self._check(bot.fetch_coinbase(900, 10))

    def test_kraken_shape(self):
        payload = {"error": [], "result": {"XXBTZUSD": [
            [1700000000, "37000.0", "37100.0", "36900.0", "37050.0", "37010.0", "11.0", 42],
            [1700000900, "37050.0", "37200.0", "37000.0", "37150.0", "37100.0", "9.0", 31]]}}
        bot.http_json = lambda url, timeout=20: payload
        self._check(bot.fetch_kraken(900, 10))

    def test_binance_shape(self):
        payload = [[1700000000000, "37000.0", "37100.0", "36900.0", "37050.0", "11.0", 1700000899999],
                   [1700000900000, "37050.0", "37200.0", "37000.0", "37150.0", "9.0", 1700001799999]]
        bot.http_json = lambda url, timeout=20: payload
        bars = bot.fetch_binance(900, 10)
        self._check(bars)
        self.assertEqual(bars[0][0], 1700000000)  # ms -> s

    def test_bitstamp_shape(self):
        payload = {"data": {"ohlc": [
            {"timestamp": "1700000000", "open": "37000", "high": "37100", "low": "36900",
             "close": "37050", "volume": "11"},
            {"timestamp": "1700000900", "open": "37050", "high": "37200", "low": "37000",
             "close": "37150", "volume": "9"}]}}
        bot.http_json = lambda url, timeout=20: payload
        self._check(bot.fetch_bitstamp(900, 10))

    def test_fetch_falls_through_to_a_working_source(self):
        gran = 900
        good = synthetic_bars(120, granularity=gran)

        def only_bitstamp(url, timeout=20):
            if "bitstamp" not in url:
                raise RuntimeError("blocked by network policy")
            return {"data": {"ohlc": [{"timestamp": str(b[0]), "open": b[1], "high": b[2],
                                       "low": b[3], "close": b[4], "volume": b[5]} for b in good]}}

        bot.http_json = only_bitstamp
        source, bars = bot.fetch_bars(gran, 120)
        self.assertEqual(source, "bitstamp")
        self.assertTrue(all(b[0] + gran <= int(time.time()) for b in bars), "unclosed candle leaked through")

    def test_all_sources_down_raises(self):
        bot.http_json = lambda url, timeout=20: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            bot.fetch_bars(900, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TelegramTests(unittest.TestCase):
    """
    An alert is a message to a person about money. Three things must hold:
    it never fires unasked, it never carries the bot token anywhere it could
    be read, and a broken Telegram never takes down a trading run.
    """

    TOKEN = "123456:AAHfaketokenfaketokenfaketoken-xyz"

    def setUp(self):
        self.saved = {k: os.environ.get(k)
                      for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        for k in self.saved:
            os.environ.pop(k, None)
        self.state = {"stats": {"equity": 113_486.72, "total_return_pct": 13.49,
                                "buy_hold_return_pct": 22.10}}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _configure(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = self.TOKEN
        os.environ["TELEGRAM_CHAT_ID"] = "99887766"

    # -- off by default ---------------------------------------------------
    def test_not_configured_without_both_variables(self):
        self.assertFalse(bot.telegram_configured())
        os.environ["TELEGRAM_BOT_TOKEN"] = self.TOKEN
        self.assertFalse(bot.telegram_configured(), "a token alone is not a destination")
        os.environ["TELEGRAM_CHAT_ID"] = "99887766"
        self.assertTrue(bot.telegram_configured())

    def test_send_makes_no_request_when_unconfigured(self):
        called = []
        saved, bot.urllib.request.urlopen = bot.urllib.request.urlopen, \
            lambda *a, **k: called.append(1)
        try:
            self.assertFalse(bot.send_telegram("hello"))
        finally:
            bot.urllib.request.urlopen = saved
        self.assertEqual(called, [], "an unconfigured clone must not phone home")

    def test_nothing_is_sent_when_unconfigured(self):
        sigs = [{"kind": "BUY", "text": "entered"}]
        self.assertEqual(bot.notify_new_signals(self.state, sigs, 50_000.0), 0)

    # -- only position changes -------------------------------------------
    def test_routine_trims_do_not_raise_a_phone(self):
        self._configure()
        sent = []
        saved, bot.send_telegram = bot.send_telegram, lambda t, **k: sent.append(t) or True
        try:
            bot.notify_new_signals(self.state, [{"kind": "TRIM", "text": "resized"}], 50_000.0)
        finally:
            bot.send_telegram = saved
        self.assertEqual(sent, [], "volatility sizing nudges the position constantly; "
                                   "alerting on it trains you to ignore alerts")

    def test_each_position_change_sends_one_message(self):
        self._configure()
        sent = []
        saved, bot.send_telegram = bot.send_telegram, lambda t, **k: sent.append(t) or True
        try:
            n = bot.notify_new_signals(self.state, [
                {"kind": "BUY", "text": "entered"},
                {"kind": "TRIM", "text": "resized"},
                {"kind": "BRAKE", "text": "throttled"},
            ], 50_000.0)
        finally:
            bot.send_telegram = saved
        self.assertEqual(n, 2)
        self.assertEqual(len(sent), 2)

    def test_the_notified_kinds_are_the_position_changes(self):
        for kind in ("BUY", "SELL", "BRAKE", "CLEAR"):
            self.assertIn(kind, bot.NOTIFY_KINDS)
        self.assertNotIn("TRIM", bot.NOTIFY_KINDS)

    # -- the message ------------------------------------------------------
    def test_every_alert_says_paper_on_the_first_line(self):
        for kind in bot.NOTIFY_KINDS:
            first = bot.format_alert(kind, "something", 50_000.0, self.state).split("\n")[0]
            self.assertIn("PAPER", first,
                          "%s alert did not label itself a simulation up front" % kind)

    def test_the_alert_carries_the_numbers_a_person_needs(self):
        msg = bot.format_alert("BUY", "went long", 51_234.5, self.state)
        self.assertIn("51,234.50", msg)
        self.assertIn("went long", msg)
        self.assertIn("113,486.72", msg)
        self.assertIn("22.10", msg, "buy & hold is the comparison that stops this "
                                    "reading as a win when it is not")

    # -- failure is contained --------------------------------------------
    def test_a_broken_telegram_does_not_raise(self):
        self._configure()

        def boom(*a, **k):
            raise OSError("connection reset")

        saved, bot.urllib.request.urlopen = bot.urllib.request.urlopen, boom
        try:
            with self.assertLogs(bot.logger, level="WARNING"):
                self.assertFalse(bot.send_telegram("hello"))
        finally:
            bot.urllib.request.urlopen = saved

    def test_the_token_never_reaches_a_log_line(self):
        self._configure()

        def boom(*a, **k):
            # urllib puts the full URL — and therefore the token — in its errors.
            raise OSError("HTTP Error 401: Unauthorized for " + bot.TELEGRAM_API % bot.TOKEN
                          if hasattr(bot, "TOKEN") else
                          "HTTP Error 401 for " + bot.TELEGRAM_API % self.TOKEN)

        saved, bot.urllib.request.urlopen = bot.urllib.request.urlopen, boom
        try:
            with self.assertLogs(bot.logger, level="WARNING") as cm:
                bot.send_telegram("hello")
        finally:
            bot.urllib.request.urlopen = saved
        joined = "\n".join(cm.output)
        self.assertNotIn(self.TOKEN, joined,
                         "the bot token leaked into a log that CI publishes")
        self.assertNotIn("AAHfaketoken", joined)

    def test_telegram_refusal_is_reported_as_failure(self):
        self._configure()

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"ok": False, "description": "chat not found"}).encode()

        saved, bot.urllib.request.urlopen = bot.urllib.request.urlopen, lambda *a, **k: Fake()
        try:
            with self.assertLogs(bot.logger, level="WARNING"):
                self.assertFalse(bot.send_telegram("hello"))
        finally:
            bot.urllib.request.urlopen = saved

    def test_test_alert_fails_loudly_when_unconfigured(self):
        self.assertEqual(bot.main(["--test-alert"]), 1,
                         "a misconfigured notifier must not exit 0 — that is how "
                         "you end up believing alerts are armed when they are not")

    def test_test_alert_reports_a_delivery_failure(self):
        self._configure()
        saved, bot.send_telegram = bot.send_telegram, lambda t, **k: False
        try:
            self.assertEqual(bot.main(["--test-alert"]), 1)
        finally:
            bot.send_telegram = saved

    def test_test_alert_says_it_is_not_a_signal(self):
        self._configure()
        sent = []
        saved, bot.send_telegram = bot.send_telegram, lambda t, **k: sent.append(t) or True
        try:
            self.assertEqual(bot.main(["--test-alert"]), 0)
        finally:
            bot.send_telegram = saved
        self.assertIn("not a signal", sent[0].lower())
        self.assertIn("PAPER", sent[0])


class DashboardAlertSectionTests(unittest.TestCase):
    """The button is a link, and it must point at the reader's own repository."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash-")
        self.saved = (bot.OUT_DIR, bot.DASHBOARD)
        bot.OUT_DIR = self.tmp
        bot.DASHBOARD = os.path.join(self.tmp, "index.html")

    def tearDown(self):
        bot.OUT_DIR, bot.DASHBOARD = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self):
        bot.render_dashboard({"stats": {}, "signals": [], "trades": [], "bars": []})
        with open(bot.DASHBOARD, encoding="utf-8") as fh:
            return fh.read()

    def test_the_page_carries_an_alerts_section(self):
        html = self._render()
        self.assertIn('id="alerts"', html)
        self.assertIn("Telegram alerts", html)

    def test_the_link_follows_github_repository(self):
        saved = os.environ.get("GITHUB_REPOSITORY")
        try:
            os.environ["GITHUB_REPOSITORY"] = "someone/their-fork"
            import importlib
            importlib.reload(bot)
            bot.OUT_DIR = self.tmp
            bot.DASHBOARD = os.path.join(self.tmp, "index.html")
            html = self._render()
            self.assertIn("someone/their-fork/actions/workflows/btc_autotrade.yml", html,
                          "a fork would send its reader to somebody else's Actions tab")
        finally:
            if saved is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = saved
            import importlib
            importlib.reload(bot)
            bot.OUT_DIR, bot.DASHBOARD = self.tmp, os.path.join(self.tmp, "index.html")

    def test_the_page_has_a_real_button_and_a_fallback_link(self):
        html = self._render()
        self.assertIn('id="testalert"', html, "the test must be a button, not only a link")
        self.assertIn("/api/test-alert", html)
        self.assertIn("actions/workflows/btc_autotrade.yml", html,
                      "a host without the function still needs a route that works")

    def test_the_live_price_control_is_present(self):
        html = self._render()
        self.assertIn('id="refresh"', html)
        for host in ("api.coinbase.com", "api.binance.com", "api.kraken.com"):
            self.assertIn(host, html, "one exchange being blocked should not "
                                      "leave the reader with no price at all")

    def test_no_credential_is_embedded_in_the_page(self):
        saved = os.environ.get("TELEGRAM_BOT_TOKEN")
        try:
            os.environ["TELEGRAM_BOT_TOKEN"] = "123456:AAHsecrettokensecrettoken"
            html = self._render()
            self.assertNotIn("AAHsecrettoken", html,
                             "the dashboard is published to a static host; a token in it "
                             "is a token given away")
            self.assertNotIn("api.telegram.org/bot1", html)
        finally:
            if saved is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = saved
