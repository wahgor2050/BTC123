#!/usr/bin/env python3
"""
================================================================================
  BTC Hourly EMA(12,26) Reverse-Dip Swing Bot — DEMO / PAPER TRADING ONLY
================================================================================
  Pulls live BTC-USD HOURLY candles from the same public-exchange fallback
  chain btc_autotrade.py already uses, runs the EMA(12,26) "reverse-dip"
  rule (long while EMA12 < EMA26 — buy the dip, sell the tip; flat once
  EMA12 recovers above EMA26), and books the fills into its own simulated
  (paper) account under ./btc_swing_ema_demo/ — completely separate from
  btc_autotrade.py's live state in ./btc_demo/.

  *** NO BROKER IS CONNECTED. NO IBKR. NO REAL ORDERS ARE EVER SENT. ***

  ──────────────────────────────────────────────────────────────────────────
  READ THIS BEFORE WATCHING THE EQUITY CURVE — THE BACKTEST SAYS THIS LOSES
  ──────────────────────────────────────────────────────────────────────────
  research/results_swing_crypto_futures_reverse_dip.txt, crypto_hourly
  section, BTC, ema12_26_reverse_dip (the exact rule wired here):

    sealed window 2024-09-13 -> 2026-09-16:
        Calmar -0.58 | CAGR -33.4% | MDD -57.7% | 301 trades
        beats its matched random control: YES
        buy & hold over the same window: +13.8% CAGR (strategy loses badly)
        zero-cost gap +0.01 — barely survives zero cost, i.e. not PURELY a
        fee artifact, but the edge is razor-thin and the strategy still
        loses money outright.
    pre-sealed 2016-09-15 -> 2024-09-15:
        Calmar -0.40 | CAGR -39.8% | MDD -98.4%
        beats control: YES, but zero-cost gap -0.53 — does NOT survive zero
        cost here; that window's "beat control" is mostly a cost artifact.
    the adaptive-percentile RSI(14) cousin is WORSE on BTC hourly (sealed
    Calmar -0.43, zero-cost fails both windows), which is why it was not
    wired.

  Bottom line: as backtested, this is a LOSING strategy on BTC hourly —
  negative CAGR outright, not merely "loses to buy & hold" like the stock
  daily version (stock_swing_autotrade.py). It clears exactly one bar, the
  weakest one: it beats random timing of the same frequency. It is wired
  here because the user asked how to run it, PAPER-ONLY, for watching — not
  because anyone should expect it to make money. Do NOT quote the sibling
  stock bot's "genuine cost-surviving timing skill" line for this one: on
  BTC hourly the zero-cost check only barely passes in one window and fails
  in the other.

  ──────────────────────────────────────────────────────────────────────────
  CANDLE / CADENCE MATCHING — WHY THIS MUST RUN HOURLY, NOT DAILY
  ──────────────────────────────────────────────────────────────────────────
  btc_autotrade.py's CONFIG documents the lesson this repo already paid
  for: the candle granularity and the job cadence MUST match. "On hourly
  candles with a two-hourly job, the engine books a fill at an open that is
  already an hour or two in the past by the time the job wakes — a price
  the schedule could never actually have reached."

  This bot trades HOURLY candles, so its runner must wake EVERY HOUR,
  shortly after the top of the hour (a few minutes past, so the candle has
  closed). A decision is made on a closed candle's CLOSE and filled at the
  NEXT candle's OPEN — the same convention as btc_backtest.backtest() and
  the research scripts, so the backtest numbers above honestly describe
  this engine. If the runner wakes late or misses hours, the last_bar_ts
  replay books the missed bars at their historical opens — acceptable as
  catch-up (and as the one-off --reset backfill seed, which replays the
  whole fetched window), NOT acceptable as the steady state. Do not run
  this on btc_autotrade.py's daily cron. For that reason granularity is
  HARDCODED to 3600 here — no env override, so a stray BTC_GRANULARITY
  cannot silently reintroduce the mismatch.

  There is deliberately NO GitHub Actions workflow for this bot yet: given
  the backtest verdict above, actually scheduling a known-losing strategy
  live (even paper) is the user's own call, not this script's.

  ──────────────────────────────────────────────────────────────────────────
  WHAT IS REUSED, WHAT IS NEW
  ──────────────────────────────────────────────────────────────────────────
  - Exchange fetch chain: imported from btc_autotrade (bitstamp -> binance
    -> coinbase -> kraken, closed-bars-only filter). All four support 3600s.
    600 bars fits every source's single-pass cap, kraken's 720 included.
  - Fill/fee bookkeeping: btc_autotrade.PaperEngine is subclassed; its
    trade_to() (slippage, fees, never-borrow clamp, trade booking) and
    update_stats() are reused unchanged. Only process() is overridden:
    no volatility sizing, no drawdown brake — the research rule is a plain
    all-in / all-out toggle and adding either would detach the engine from
    the backtest that describes it.
  - Signal math: btc_backtest.Indicators.ema — the IDENTICAL code path the
    research scripts used (research/swing_recent_intraday.py's
    strat_ema_cross_reverse imports the same class), so there is no
    reimplementation drift. want[i] = 1.0 if EMA12[i] < EMA26[i] else 0.0.
  - Costs: fee 10 bps + slippage 5 bps, the research COST convention.
  - State: ./btc_swing_ema_demo/state.json (+ trades.csv), atomic writes,
    idempotent reruns via last_bar_ts (a rerun inside the same hour books
    nothing). Telegram and the HTML dashboard are deliberately omitted.

  EMA warm-up guard (engineering only, NOT part of the pre-registered
  rule): Indicators.ema seeds at the first close of the window, so the
  first ~100 bars of a fresh 600-bar window carry seed bias. The one-off
  --reset backfill replay therefore skips trading for the first
  WARMUP_BARS (130 = 5x26) bars. In steady-state hourly operation the
  decision bar always has ~600 bars behind it (seed influence < 1e-19), so
  this guard never changes a live decision — it only stops the initial
  seeding replay from trading on an unconverged average. The research
  backtests ran the un-guarded rule over multi-year windows where the
  unconverged fraction was negligible; nothing here alters that rule.

  Usage:
      python3 btc_swing_ema_hourly.py            # one pass (hourly runner)
      python3 btc_swing_ema_hourly.py --reset    # wipe paper account, reseed
      python3 btc_swing_ema_hourly.py --dry-run  # read market, write nothing
      python3 btc_swing_ema_hourly.py --summary  # markdown state summary
      python3 btc_swing_ema_hourly.py --loop     # local live demo, hourly

  Dependencies: Python 3.9+ standard library only (same as its siblings).
================================================================================
"""

import argparse
import json
import logging
import os
import sys
import time

import btc_autotrade as bat          # fetch chain + PaperEngine + helpers
import btc_backtest as btl           # Indicators (the research EMA code path)

logger = logging.getLogger("btc-swing-ema")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "btc_swing_ema_demo")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
TRADES_CSV = os.path.join(OUT_DIR, "trades.csv")

STATE_VERSION = 1
GRANULARITY = 3600        # HARDCODED hourly — see the cadence section above.
WARMUP_BARS = 130         # 5 x EMA(26): backfill-replay seed guard only.

CONFIG = {
    "granularity": GRANULARITY,               # not env-overridable, on purpose
    "history_bars": int(bat._env("BTC_SWING_HISTORY_BARS", 600, int)),
    "ema_fast": 12,                           # the pre-registered rule —
    "ema_slow": 26,                           # not tunable without a new backtest
    "start_cash": bat._env("BTC_SWING_START_CASH", 100_000.0),
    "fee_bps": bat._env("BTC_SWING_FEE_BPS", 10.0),       # research COST
    "slippage_bps": bat._env("BTC_SWING_SLIPPAGE_BPS", 5.0),
    # PaperEngine.trade_to/update_stats expectations:
    "rebalance_band": 0.50,   # binary signal: only crossings trade, never resizes
    "dd_limit": 0.0,          # no brake — the research rule has none
    "keep_bars": int(bat._env("BTC_SWING_KEEP_BARS", 400, int)),
    "keep_equity": int(bat._env("BTC_SWING_KEEP_EQUITY", 4000, int)),
    "keep_trades": int(bat._env("BTC_SWING_KEEP_TRADES", 400, int)),
}


# ------------------------------------------------------------------------------
# State — btc_autotrade's shape, this bot's own file. Never touches btc_demo/.
# ------------------------------------------------------------------------------
def new_state(cfg):
    state = bat.new_state(cfg)
    state["version"] = STATE_VERSION
    state["mode"] = ("PAPER / DEMO — no broker connected | EMA(12,26) reverse-dip, "
                     "hourly | backtested CAGR -33.4% sealed: expected to LOSE")
    return state


def load_state(cfg, reset=False):
    if reset or not os.path.exists(STATE_FILE):
        logger.info("Starting a fresh paper account with $%s", f"{cfg['start_cash']:,.0f}")
        return new_state(cfg)
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("version") != STATE_VERSION:
        logger.warning("State version mismatch, resetting paper account")
        return new_state(cfg)
    state["config"] = dict(cfg)
    return state


def save_state(state):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=False)
    os.replace(tmp, STATE_FILE)


def write_trades_csv(state):
    import csv
    fields = ["entry_time", "exit_time", "side", "qty", "entry_price", "exit_price",
              "fees", "pnl", "pnl_pct", "reason", "bars_held"]
    with open(TRADES_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for t in state["trades"]:
            writer.writerow({k: t.get(k, "") for k in fields})


# ------------------------------------------------------------------------------
# Signal — the research code path, unchanged.
# ------------------------------------------------------------------------------
def reverse_dip_want(bars, cfg):
    """want[i] = 1.0 while EMA(fast) < EMA(slow) at bar i's close, else 0.0.
    Same definition as research/swing_recent_intraday.strat_ema_cross_reverse,
    computed with the same btc_backtest.Indicators.ema code."""
    ind = btl.Indicators(bars)
    ef, es = ind.ema(cfg["ema_fast"]), ind.ema(cfg["ema_slow"])
    return [1.0 if ef[i] < es[i] else 0.0 for i in range(len(bars))], ef, es


# ------------------------------------------------------------------------------
# Engine — btc_autotrade.PaperEngine bookkeeping, reverse-dip decisions.
# Decision on bar i-1's CLOSE, fill at bar i's OPEN (btc_backtest convention).
# ------------------------------------------------------------------------------
class SwingEngine(bat.PaperEngine):
    def process(self, bars):
        want, ef, es = reverse_dip_want(bars, self.c)
        self.s["want"] = [round(w, 4) for w in want[-self.c["keep_bars"]:]]
        processed = 0
        fresh = self.s["last_bar_ts"] == 0          # --reset / first-run replay
        for i in range(1, len(bars)):
            ts, o, _h, _l, close, _v = bars[i]
            if ts <= self.s["last_bar_ts"]:
                continue
            if fresh and i < WARMUP_BARS:
                # Backfill-seed guard only: skip trading while the window's
                # seeded EMA is unconverged. Never triggers in steady state
                # (new bars arrive at the end of a full window). Docstring.
                self.s["last_bar_ts"] = int(ts)
                continue
            if self.s["first_price"] is None:
                self.s["first_price"] = o           # B&H measured from the same bar
            target = want[i - 1]                    # last hour's close decides,
            eq = self.equity(o)                     # this hour's open fills
            held = (self.s["qty"] * o / eq) if eq > 0 else 0.0
            crossing = (target == 0.0) != (held <= 1e-9)
            if crossing or abs(target - held) > self.c["rebalance_band"]:
                if target > 0:
                    note = "dip: EMA12 < EMA26 — buying the dip"
                else:
                    note = "recovered: EMA12 > EMA26 — selling the tip"
                self.trade_to(ts, o, target, note)
            self.s["last_bar_ts"] = int(ts)
            eq_close = self.equity(close)
            self.s["peak_equity"] = max(self.s.get("peak_equity", eq_close), eq_close)
            self.s["equity_curve"].append([int(ts), round(eq_close, 2)])
            processed += 1

        self.s["equity_curve"] = self.s["equity_curve"][-self.c["keep_equity"]:]
        keep = self.c["keep_bars"]
        self.s["bars"] = [[int(b[0]), b[1], b[2], b[3], b[4], b[5]] for b in bars[-keep:]]
        self.s["indicators"] = {
            "ema_fast": [round(v, 2) for v in ef[-keep:]],
            "ema_slow": [round(v, 2) for v in es[-keep:]],
        }
        self.s["target_weight"] = round(want[-1], 4)
        return processed


# ------------------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------------------
def dry_run(cfg):
    """Read the live market, report what the rule sees, write nothing."""
    try:
        source, bars = bat.fetch_bars(cfg["granularity"], cfg["history_bars"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("No exchange reachable, skipping the dry run: %s", exc)
        return 0
    want, ef, es = reverse_dip_want(bars, cfg)
    i = len(bars) - 1
    state_txt = ("LONG (EMA12 below EMA26 — in the dip)" if want[i] > 0
                 else "FLAT (EMA12 above EMA26 — dip over)")
    logger.info("Source         : %s", source)
    logger.info("Closed bars    : %d  (%s -> %s)", len(bars),
                bat.iso(bars[0][0]), bat.iso(bars[-1][0]))
    logger.info("Last close     : $%s", f"{bars[-1][4]:,.2f}")
    logger.info("EMA 12 / 26    : %s / %s", f"{ef[i]:,.2f}", f"{es[i]:,.2f}")
    logger.info("Rule wants     : %s", state_txt)
    logger.info("Dry run OK — nothing was written, no order was placed (there is no broker).")
    return 0


def run_once(cfg, state):
    started = time.time()
    try:
        source, bars = bat.fetch_bars(cfg["granularity"], cfg["history_bars"])
    except Exception as exc:  # noqa: BLE001
        logger.error("No market data this run: %s", exc)
        state.setdefault("last_run", {})
        state["last_run"].update({"time": bat.iso(started), "ok": False,
                                  "error": str(exc)[:300]})
        state["updated_utc"] = bat.iso(started)
        save_state(state)
        return False

    engine = SwingEngine(state, cfg)
    processed = engine.process(bars)
    last_price = bars[-1][4]
    engine.update_stats(last_price)

    state["updated_utc"] = bat.iso(time.time())
    state["last_run"] = {
        "time": bat.iso(time.time()),
        "ok": True,
        "source": source,
        "bars_processed": processed,
        "last_bar": bat.iso(state["last_bar_ts"]),
        "seconds": round(time.time() - started, 2),
    }
    save_state(state)
    write_trades_csv(state)

    st = state["stats"]
    logger.info("Processed %d new bar(s) | price $%.2f | equity $%.2f (%+.2f%%) | "
                "B&H %+.2f%% | %d trades, %s%% win",
                processed, last_price, st["equity"], st["total_return_pct"],
                st["buy_hold_return_pct"], st["trades"], st["win_rate_pct"])
    if state["qty"] > 0:
        logger.info("Open: LONG %.6f BTC @ $%.2f — in the dip (EMA12 < EMA26)",
                    state["qty"], state["entry_price"])
    else:
        logger.info("Open: flat (in cash) — EMA12 is above EMA26")
    return True


def markdown_summary(state):
    lines = ["### BTC hourly EMA(12,26) reverse-dip — paper/demo", ""]
    lines.append("> Backtested (sealed 2024-09-13→2026-09-16): CAGR **-33.4%**, "
                 "MDD -57.7%, Calmar -0.58 — a losing strategy, run for watching only.")
    lines.append("")
    run, st = state.get("last_run", {}), state.get("stats", {})
    if not run:
        lines.append("_No run recorded yet._")
    elif not run.get("ok"):
        lines.append("No market data last run: `%s`" % run.get("error", "unknown"))
    else:
        qty = state.get("qty", 0.0)
        rows = [
            ("BTC price", "$%s" % f"{st.get('last_price', 0):,.2f}"),
            ("Paper equity", "$%s" % f"{st.get('equity', 0):,.2f}"),
            ("Strategy return", "%+.2f%%" % st.get("total_return_pct", 0.0)),
            ("Buy & hold", "%+.2f%%" % st.get("buy_hold_return_pct", 0.0)),
            ("Trades (win rate)", "%d (%.0f%%)" % (st.get("trades", 0),
                                                   st.get("win_rate_pct", 0.0))),
            ("Position", ("LONG %.6f BTC @ $%s" % (qty, f"{state.get('entry_price', 0):,.2f}"))
             if qty > 0 else "flat (in cash)"),
            ("Data source", run.get("source", "-")),
            ("New candles booked", str(run.get("bars_processed", 0))),
            ("Last candle", run.get("last_bar", "-")),
        ]
        lines += ["| metric | value |", "|---|---|"]
        lines += ["| %s | %s |" % r for r in rows]
    lines += ["", "> Simulated fills only. No broker is connected and no real order is ever placed."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="BTC hourly EMA(12,26) reverse-dip paper bot (no broker; "
                    "backtested as a LOSING strategy — see the docstring)")
    ap.add_argument("--reset", action="store_true", help="wipe the paper account and reseed")
    ap.add_argument("--dry-run", action="store_true", help="read the market, write nothing")
    ap.add_argument("--summary", action="store_true", help="print a markdown summary and exit")
    ap.add_argument("--loop", action="store_true", help="keep running locally, hourly")
    ap.add_argument("--interval", type=int, default=3600,
                    help="seconds between --loop passes (default 3600 — hourly "
                         "candles need an hourly cadence, see the docstring)")
    args = ap.parse_args(argv)

    cfg = dict(CONFIG)

    if args.dry_run:
        return dry_run(cfg)

    if args.summary:
        if not os.path.exists(STATE_FILE):
            print("_No paper-account state yet._")
            return 0
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            print(markdown_summary(json.load(fh)))
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    state = load_state(cfg, reset=args.reset)

    logger.info("=" * 78)
    logger.info("  BTC HOURLY EMA(12,26) REVERSE-DIP — PAPER ACCOUNT, NO BROKER, NO REAL ORDERS")
    logger.info("  1h candles | long while EMA12 < EMA26, flat otherwise | all-in/all-out")
    logger.info("  Backtest verdict: LOSING (sealed CAGR -33.4%%) — run for watching only")
    logger.info("=" * 78)

    if not args.loop:
        ok = run_once(cfg, state)
        return 0 if ok else 0   # transient API hiccups must not fail a runner

    try:
        while True:
            run_once(cfg, state)
            logger.info("Sleeping %ds… (Ctrl-C to stop)", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Stopped by user. State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
