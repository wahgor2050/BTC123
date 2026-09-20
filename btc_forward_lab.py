#!/usr/bin/env python3
"""
================================================================================
  BTC Forward Paper Lab — DEMO / PAPER TRADING ONLY
================================================================================
  Three frozen research candidates run forward as INDEPENDENT paper accounts,
  alongside (but never touching) the baseline bot in btc_autotrade.py:

    A. sma_1d_30d_v25_v1      — hourly SMA(24) > SMA(720) trend filter,
                                 25% annualised vol target, 10%-of-NAV resize
                                 band. No drawdown throttle, no stop.
    B. rvol2_breakout24h_v25_v1 — 24h-high breakout above SMA(720), greedy
                                 parent schedule with signals >= 25h apart,
                                 RVOL(28 same-UTC-hour days) >= 2 gate,
                                 fixed 24h hold. Rejected parents still
                                 consume the cooldown.
    C. funding_z168_limit_entry_v1 — EXECUTION-HYPOTHESIS TEST (added
                                 2026-09-21, own activation epoch; spec
                                 frozen first in btc_demo/lab/
                                 funding_candidate_spec.md). Signal verbatim
                                 from research/funding_extreme_timing_btc.py:
                                 Kraken perp funding z (168-settlement
                                 trailing window), enter z<=-2.0, exit
                                 z>=0.0, sticky. Entry = resting LIMIT buy
                                 at best bid, 4h deadline, fill only when a
                                 wholly-post-placement hourly candle's low
                                 touches the limit (2bp maker fee, no slip);
                                 unfilled/expired attempts are logged, never
                                 fabricated. Exit = same taker model as A/B.
                                 Uses TWO data sources: Bitstamp spot for
                                 price/fills, Kraken Futures for the signal.

  A and B are UNCONFIRMED research leads (post-hoc, thin samples, fragile to
  costs and best-trade removal; B failed its own matched-control test and its
  Binance replication was negative). C's signal is real at zero cost but
  LOSES money at taker cost (-12.6% CAGR); the maker-cost rerun (+9.3%)
  assumed 100% fill probability — this account exists to measure the actual
  fill rate, nothing more. This lab gathers prospective evidence and
  execution data — it is NOT a validated or profitable system, and it must
  never be described as one.

  *** NO BROKER IS CONNECTED. NO REAL ORDERS ARE EVER SENT. ***

  Data/venue (pinned at activation — a venue change is a new epoch, never a
  silent substitution): Bitstamp BTC/USD spot. Hourly OHLC candles for
  signals, the public ticker's best bid/ask for simulated fills. Quote
  currency is USD. Bitstamp's ticker carries no quote size; that field is
  recorded as absent rather than invented. Live Bitstamp volume is NOT
  asserted to be identical to the unverified "cached" venue behind the
  historical research files — forward RVOL here is a new data epoch.

  Fill model (frozen): mid M=(bid+ask)/2; buy = max(ask, M*(1+0.0002));
  sell = min(bid, M*(1-0.0002)); fee = 0.001 * executed notional per side.
  The 2bp is a minimum combined spread/slippage allowance — a wider observed
  spread overrides it. This is a transparent paper assumption, not a
  guaranteed executable fill or a depth/impact model, and it can cost more
  than the research's fixed 10bp fee + 2bp slip; every order therefore also
  carries a diagnostic reprice under that historical fixed-cost model
  (same order, same quantity — a cost diagnostic, not a second strategy).

  Persistence: btc_demo/lab/state.json (authoritative current state) plus
  append-only monthly archives orders_YYYYMM.csv / opportunities_YYYYMM.csv /
  equity_YYYYMM.csv, and summary.json for the dashboard. State, ledgers and
  snapshots are committed together by the workflow.

  Usage:
      python btc_forward_lab.py               # one pass (what CI runs hourly)
      python btc_forward_lab.py --dry-run     # read live data, write nothing
      python btc_forward_lab.py --summary     # markdown summary of saved state
      python btc_forward_lab.py --pause  rvol2_breakout24h_v25_v1
      python btc_forward_lab.py --resume rvol2_breakout24h_v25_v1
        (pause blocks NEW entries only; due exits, snapshots and history
         continue, and nothing is ever deleted)

  Dependencies: Python 3.9+ standard library only.
================================================================================
"""
import argparse
import csv
import hashlib
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

import btc_funding_signal as fsig

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("btc-forward-lab")

HERE = os.path.dirname(os.path.abspath(__file__))
LAB_DIR = os.path.join(HERE, "btc_demo", "lab")
STATE_FILE = os.path.join(LAB_DIR, "state.json")
SUMMARY_FILE = os.path.join(LAB_DIR, "summary.json")
BASELINE_STATE = os.path.join(HERE, "btc_demo", "state.json")

SCHEMA_VERSION = 1
HOUR = 3600

# ------------------------------------------------------------------------------
# Frozen operational settings (changing any of these after activation would be
# a new strategy version — do not retune in place)
# ------------------------------------------------------------------------------
VENUE = {"venue": "bitstamp", "product": "btcusd", "quote_ccy": "USD",
         "candles": "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=3600&limit=1000",
         "ticker": "https://www.bitstamp.net/api/v2/ticker/btcusd/"}
FEE_RATE = 0.001                 # per side, on executed notional
MIN_HALF_SPREAD = 0.0002         # minimum combined spread/slip allowance (see header)
ENTRY_DEADLINE_SEC = 20 * 60     # fresh-entry deadline after signal candle close
QUOTE_MAX_AGE_SEC = 300          # ticker older than this is not an executable quote
CANDLE_RETRIES = 5               # bounded wait for the just-closed candle
CANDLE_RETRY_SLEEP = 30
ANNUAL = math.sqrt(24 * 365.25)

# Run-health alerting (operational, distinct from trade alerts): serious issues
# alert at once; data weather (feed/quote down) alerts on the 2nd consecutive
# degraded run; each kind then repeats at most every 6 hours.
HEALTH_MIN_REPEAT_SEC = 6 * 3600
HEALTH_CONSEC = {"no_candles": 2, "no_quote": 2, "no_funding": 2}
HEALTH_TEXT = {
    "no_candles": "小時K線攞唔到(連續兩個 run)— 引擎冇新訊號可處理",
    "no_quote": "冇可用報價(連續兩個 run)— 開倉會 missed,到期平倉會延後",
    "no_funding": "Kraken 資金費率攞唔到(連續兩個 run)— 候選C冇新訊號;"
                  "持倉會喺最後已知結算舊過6小時後按凍結規則強制平倉",
    "reconcile_fail": "賬本對唔上 NAV(RECONCILE_FAIL)— 要人手睇",
    "exit_unresolved": "有到期平倉一直執行唔到(exit_unresolved)— 要人手睇",
}
WATCHDOG_STALE_SEC = 3 * 3600    # --health-check: state older than this alerts

STRATEGIES = {
    "sma_1d_30d_v25_v1": {
        "kind": "sma_trend",
        "sma_fast": 24, "sma_slow": 720,
        "vol_window": 720, "target_vol": 0.25, "rebalance_band": 0.10,
    },
    "rvol2_breakout24h_v25_v1": {
        "kind": "rvol_breakout",
        "high_lookback": 24, "sma_slow": 720,
        "vol_window": 720, "target_vol": 0.25,
        "rvol_days": 28, "rvol_min": 2.0,
        "parent_spacing_hours": 25, "hold_hours": 24,
    },
    # Candidate C — spec frozen in btc_demo/lab/funding_candidate_spec.md
    # BEFORE this dict entry existed. Changing anything here is a new version.
    "funding_z168_limit_entry_v1": {
        "kind": "funding_limit",
        "z_window": 168, "z_enter": -2.0, "z_exit": 0.0,
        "stale_limit_sec": 6 * 3600,
        "limit_deadline_sec": 4 * 3600,
        "maker_fee": 0.0002,
        "funding_venue": "kraken", "funding_symbol": "PF_XBTUSD",
        "funding_per_day": 24,
    },
}
START_CASH = 100_000.0

ORDER_FIELDS = ["order_id", "event_id", "strategy", "side", "decision_ts", "request_ts",
                "quote_exchange_ts", "bid", "ask", "mid", "quote_size", "fill_price",
                "qty", "gross_notional", "fee", "cash_before", "qty_before", "cash_after",
                "qty_after", "basis_after", "realized_pnl_delta", "reason",
                "signal_ts", "exit_due_ts", "lateness_sec",
                "diag_fill_price", "diag_fee", "schema"]
OPP_FIELDS = ["event_id", "strategy", "signal_ts", "signal_time", "observed_ts",
              "observed_late", "close", "high24", "sma_fast", "sma_slow", "sigma_ann",
              "rvol_num", "rvol_den", "rvol", "target_weight", "cooldown_ok",
              "gate", "decision", "reason", "data_status", "venue", "schema"]
EQ_FIELDS = ["ts", "time", "strategy", "cash", "qty", "mark", "mark_source",
             "cost_basis", "realized_pnl", "unrealized_pnl", "fees_cum", "nav",
             "exposure_pct", "peak_nav", "drawdown_pct", "data_status", "schema"]
# Candidate C's own signal audit trail (new file family — the shared CSV
# schemas above stay frozen; z lives here, never squeezed into their columns)
FUND_FIELDS = ["ts", "time", "strategy", "bar_ts", "bar_time", "settlement_ts",
               "settlement_age_sec", "rate", "z", "position", "resting_limit_px",
               "resting_deadline_ts", "action", "schema"]


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def month_key(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m")


def param_hash(params):
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------
# Market data (real implementation; tests inject fakes)
# ------------------------------------------------------------------------------
UA = "btc-forward-lab/1.0 (+github actions; educational paper trading)"


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_candles(now=None):
    """Closed Bitstamp hourly candles, ascending [ts, o, h, l, c, v]."""
    now = time.time() if now is None else now
    raw = http_json(VENUE["candles"])["data"]["ohlc"]
    bars = [[int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r["volume"])] for r in raw]
    seen = {}
    for b in bars:
        seen[b[0]] = b                       # last write wins on duplicates
    bars = [seen[k] for k in sorted(seen)]
    return [b for b in bars if b[0] + HOUR <= now]


def fetch_quote(now=None):
    """Best bid/ask snapshot. Returns dict or None if unusable/stale."""
    now = time.time() if now is None else now
    t0 = time.time()
    try:
        j = http_json(VENUE["ticker"])
        bid, ask = float(j["bid"]), float(j["ask"])
        ex_ts = int(j.get("timestamp", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Quote fetch failed: %s", type(exc).__name__)
        return None
    if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
        logger.warning("Quote rejected: bid=%r ask=%r", bid, ask)
        return None
    if ask / bid - 1 > 0.05:
        logger.warning("Quote rejected: spread %.2f%% implausible", (ask / bid - 1) * 100)
        return None
    if ex_ts and now - ex_ts > QUOTE_MAX_AGE_SEC:
        logger.warning("Quote rejected: exchange ts %ds old", int(now - ex_ts))
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
            "exchange_ts": ex_ts, "request_ts": t0, "receipt_ts": time.time(),
            "quote_size": None}   # Bitstamp's ticker carries no size — recorded as absent


# ------------------------------------------------------------------------------
# Indicators over a contiguous hourly window
# ------------------------------------------------------------------------------
def contiguous_tail(bars):
    """Longest strictly-consecutive-hourly suffix of `bars`."""
    if not bars:
        return []
    start = 0
    for i in range(len(bars) - 1, 0, -1):
        if bars[i][0] - bars[i - 1][0] != HOUR:
            start = i
            break
    return bars[start:]


def build_features(bars):
    """Per-bar features on a contiguous window. None where the window is short.
    Same definitions as the frozen research: SMA windows include the current
    close; sigma is the ddof=1 std of the last 720 hourly log returns,
    annualised by sqrt(24*365.25); high24 excludes the current bar; RVOL's
    denominator is the median of the 28 PREVIOUS same-UTC-hour volumes."""
    n = len(bars)
    cl = [b[4] for b in bars]
    hi = [b[2] for b in bars]
    vol = [b[5] for b in bars]
    lr = [None] + [math.log(cl[i] / cl[i - 1]) if cl[i - 1] > 0 and cl[i] > 0 else None
                   for i in range(1, n)]
    f = {"sma_fast": [None] * n, "sma_slow": [None] * n, "sigma": [None] * n,
         "weight": [None] * n, "high24": [None] * n,
         "rvol": [None] * n, "rvol_num": [None] * n, "rvol_den": [None] * n,
         "breakout": [False] * n}
    for i in range(n):
        if i >= 23:
            f["sma_fast"][i] = math.fsum(cl[i - 23:i + 1]) / 24.0
        if i >= 719:
            f["sma_slow"][i] = math.fsum(cl[i - 719:i + 1]) / 720.0
        if i >= 720:
            win = lr[i - 719:i + 1]
            if all(x is not None for x in win):
                m = math.fsum(win) / 720.0
                var = math.fsum((x - m) ** 2 for x in win) / 719.0    # sample variance
                sd = math.sqrt(var) * ANNUAL
                f["sigma"][i] = sd
                if math.isfinite(sd) and sd > 0:
                    f["weight"][i] = min(1.0, 0.25 / sd)
        if i >= 24:
            f["high24"][i] = max(hi[i - 24:i])
        if i >= 24 * 28:
            prev = sorted(vol[i - 24 * k] for k in range(1, 29))
            den = (prev[13] + prev[14]) / 2.0
            if den > 0:
                f["rvol_den"][i] = den
                f["rvol_num"][i] = vol[i]
                f["rvol"][i] = vol[i] / den
        if f["sma_slow"][i] is not None and f["high24"][i] is not None:
            f["breakout"][i] = cl[i] > f["high24"][i] and cl[i] > f["sma_slow"][i]
    return f


# ------------------------------------------------------------------------------
# Fill model + account accounting
# ------------------------------------------------------------------------------
def fill_prices(quote):
    mid = quote["mid"]
    return (max(quote["ask"], mid * (1 + MIN_HALF_SPREAD)),     # buy
            min(quote["bid"], mid * (1 - MIN_HALF_SPREAD)))     # sell


def diag_reprice(side, mid):
    """Historical fixed-cost model (10bp fee + 2bp slip) on the same order —
    a labelled cost diagnostic, not a second strategy."""
    return mid * (1 + 0.0002) if side == "BUY" else mid * (1 - 0.0002)


def new_account(sid):
    params = STRATEGIES[sid]
    return {"strategy_id": sid, "version": "v1", "params": dict(params),
            "param_hash": param_hash(params), "paused": False,
            "start_cash": START_CASH, "cash": START_CASH, "qty": 0.0,
            "cost_basis": 0.0, "realized_pnl": 0.0, "fees_cum": 0.0,
            "episodes": 0, "order_count": 0,
            "last_signal_ts": 0,          # open-ts of last processed closed candle
            "last_parent_signal_ts": 0,   # B only: parent cooldown anchor
            "entry_fill_ts": None, "entry_event_id": None, "exit_due_ts": None,
            "resting": None,              # C only: live resting limit order
            "exit_signal_ts": None,       # C only: exit owed but unexecuted
            "exit_reason": None,
            "peak_nav": START_CASH, "last_equity_hour": 0,
            "pending": [], "data_status": "ok"}


def ensure_accounts(state, now, bars):
    """Strategies added AFTER the lab first activated join with their OWN
    activation epoch: warmup only (candles closed before their activation
    never trade), and their review clock starts here, not at the lab's
    original activation. Existing accounts (A/B) are never touched.
    Idempotent: a second call is a no-op. Requires candles — without a
    warmup anchor a later-added account must wait for a run that has them."""
    if not bars:
        return []
    latest = bars[-1][0]
    added = []
    for sid in STRATEGIES:
        if sid in state["accounts"]:
            continue
        a = new_account(sid)
        a["last_signal_ts"] = latest      # warmup: pre-activation candles never trade
        a["last_entry_signal_ts"] = 0
        a["last_z_eval_ts"] = latest
        a["activation_ts"] = int(now)
        a["activation_utc"] = iso(now)
        state["accounts"][sid] = a
        added.append(sid)
        logger.info("ACTIVATED later-added account %s at %s (own epoch; warmup only).",
                    sid, a["activation_utc"])
    return added


def nav_of(acct, mark):
    return acct["cash"] + acct["qty"] * mark


def apply_buy(acct, qty, price, fee_rate=FEE_RATE):
    """Buy `qty` at `price`; fee goes into cash outflow AND cost basis.
    fee_rate defaults to the A/B taker fee; candidate C's limit-entry fills
    pass their frozen 2bp maker fee instead."""
    gross = qty * price
    fee = gross * fee_rate
    acct["cash"] -= gross + fee
    if -1e-6 < acct["cash"] < 0:
        acct["cash"] = 0.0        # float epsilon from an exact-affordability cap
    assert acct["cash"] >= 0, "purchase affordability violated"
    acct["qty"] += qty
    acct["cost_basis"] += gross + fee
    acct["fees_cum"] += fee
    return gross, fee


def apply_sell(acct, qty, price):
    """Sell `qty` at `price`; realized PnL = net proceeds - removed basis."""
    gross = qty * price
    fee = gross * FEE_RATE
    removed = acct["cost_basis"] * (qty / acct["qty"])
    acct["cash"] += gross - fee
    acct["qty"] -= qty
    acct["cost_basis"] -= removed
    acct["fees_cum"] += fee
    delta = (gross - fee) - removed
    acct["realized_pnl"] += delta
    if acct["qty"] <= 1e-12:
        acct["qty"] = 0.0
        acct["cost_basis"] = 0.0
        acct["episodes"] += 1
    return gross, fee, delta


def reconcile(acct, mark):
    """realized + unrealized must equal NAV - initial, within rounding."""
    nav = nav_of(acct, mark)
    unreal = acct["qty"] * mark - acct["cost_basis"]
    ok = abs((acct["realized_pnl"] + unreal) - (nav - acct["start_cash"])) < 0.01
    return ok, nav, unreal


# ------------------------------------------------------------------------------
# The engine. All I/O is injected so the tests can drive it deterministically.
# ------------------------------------------------------------------------------
class LabEngine:
    def __init__(self, state, now, bars, quote, funding=None):
        self.state = state
        self.now = now
        self.quote = quote
        # funding: {"ok": bool, "f_times": [...], "f_z": [...], "f_rates": [...],
        #           "last_known_ts": int|None} — candidate C's Kraken signal
        # context. ok=False means the fetch failed THIS run (data weather);
        # last_known_ts is the newest settlement ever successfully read
        # (persisted in state), which drives the frozen no-coast-blind rule.
        self.funding = funding or {"ok": False, "f_times": [], "f_z": [],
                                   "f_rates": [], "last_known_ts": None}
        self.orders = []
        self.opps = []
        self.snapshots = []
        self.alerts = []
        self.status_alerts = []      # resting-order lifecycle (placed/expired/…)
        self.funding_rows = []       # candidate C signal audit rows
        win = contiguous_tail(bars)
        self.bars = win
        self.feat = build_features(win)
        self.index = {b[0]: i for i, b in enumerate(win)}
        self.dropped_gap = len(bars) - len(win)

    # ---- helpers ----
    def _log_opp(self, sid, ts, decision, reason, extra=None):
        i = self.index.get(ts)
        fx = {}
        if i is not None:
            fx = {"close": self.bars[i][4], "high24": self.feat["high24"][i],
                  "sma_fast": self.feat["sma_fast"][i], "sma_slow": self.feat["sma_slow"][i],
                  "sigma_ann": self.feat["sigma"][i], "rvol_num": self.feat["rvol_num"][i],
                  "rvol_den": self.feat["rvol_den"][i], "rvol": self.feat["rvol"][i]}
        row = {"event_id": "%s:%d" % (sid, ts), "strategy": sid, "signal_ts": ts,
               "signal_time": iso(ts), "observed_ts": int(self.now),
               "observed_late": self.now - (ts + HOUR) > ENTRY_DEADLINE_SEC,
               "target_weight": None, "cooldown_ok": None, "gate": None,
               "decision": decision, "reason": reason,
               "data_status": "gap_dropped_%d" % self.dropped_gap if self.dropped_gap else "ok",
               "venue": "%s:%s" % (VENUE["venue"], VENUE["product"]),
               "schema": SCHEMA_VERSION}
        row.update(fx)
        row.update(extra or {})
        self.opps.append(row)
        return row

    def _order(self, acct, side, qty, event_id, reason, signal_ts,
               exit_due_ts=None, lateness=0):
        q = self.quote
        buy_px, sell_px = fill_prices(q)
        px = buy_px if side == "BUY" else sell_px
        cash0, qty0 = acct["cash"], acct["qty"]
        if side == "BUY":
            gross, fee = apply_buy(acct, qty, px)
            delta = 0.0
        else:
            gross, fee, delta = apply_sell(acct, qty, px)
        acct["order_count"] += 1
        dpx = diag_reprice(side, q["mid"])
        rec = {"order_id": "%s:%d:%s" % (acct["strategy_id"], signal_ts, side),
               "event_id": event_id, "strategy": acct["strategy_id"], "side": side,
               "decision_ts": int(self.now), "request_ts": round(q["request_ts"], 2),
               "quote_exchange_ts": q["exchange_ts"], "bid": q["bid"], "ask": q["ask"],
               "mid": round(q["mid"], 2),
               "quote_size": "" if q.get("quote_size") is None else q["quote_size"],
               "fill_price": round(px, 2), "qty": round(qty, 8),
               "gross_notional": round(gross, 2), "fee": round(fee, 2),
               "cash_before": round(cash0, 2), "qty_before": round(qty0, 8),
               "cash_after": round(acct["cash"], 2), "qty_after": round(acct["qty"], 8),
               "basis_after": round(acct["cost_basis"], 2),
               "realized_pnl_delta": round(delta, 2), "reason": reason,
               "signal_ts": signal_ts, "exit_due_ts": exit_due_ts or "",
               "lateness_sec": int(lateness), "diag_fill_price": round(dpx, 2),
               "diag_fee": round(qty * dpx * FEE_RATE, 2), "schema": SCHEMA_VERSION}
        self.orders.append(rec)
        self.alerts.append((acct["strategy_id"], side, qty, px, reason))
        return rec

    def _fresh(self, ts):
        return self.now - (ts + HOUR) <= ENTRY_DEADLINE_SEC

    def _new_closed(self, acct):
        """Closed candles not yet processed, ascending."""
        return [b for b in self.bars if b[0] > acct["last_signal_ts"]]

    # ---- candidate B ----
    def run_rvol(self, acct):
        sid = acct["strategy_id"]
        p = acct["params"]
        exited_this_run = False

        # 1) overdue timed exit first — mandatory, executes even late, but only
        #    against a fresh executable quote (never a fabricated stale fill).
        if acct["qty"] > 0 and acct["exit_due_ts"] and self.now >= acct["exit_due_ts"]:
            if self.quote:
                late = self.now - acct["exit_due_ts"]
                self._order(acct, "SELL", acct["qty"], acct["entry_event_id"],
                            "timed_exit_24h", acct["last_entry_signal_ts"],
                            exit_due_ts=acct["exit_due_ts"], lateness=late)
                acct["exit_due_ts"] = None
                acct["entry_fill_ts"] = None
                acct["entry_event_id"] = None
                acct["pending"] = [x for x in acct["pending"] if x.get("kind") != "exit_unresolved"]
                exited_this_run = True
            else:
                msg = {"kind": "exit_unresolved", "due_ts": acct["exit_due_ts"],
                       "noted_ts": int(self.now), "note": "exit due but no executable quote"}
                if not any(x.get("kind") == "exit_unresolved" for x in acct["pending"]):
                    acct["pending"].append(msg)
                logger.error("[%s] exit due at %s but no executable quote — retrying next run",
                             sid, iso(acct["exit_due_ts"]))

        # 2) prospective parent schedule over newly closed candles (ascending).
        latest_ts = self.bars[-1][0] if self.bars else 0
        for b in self._new_closed(acct):
            ts = b[0]
            i = self.index[ts]
            if self.feat["sma_slow"][i] is None or self.feat["weight"][i] is None \
                    or self.feat["rvol"][i] is None:
                if self.feat["breakout"][i]:
                    self._log_opp(sid, ts, "rejected", "data_invalid_window")
                acct["last_signal_ts"] = ts
                continue
            if not self.feat["breakout"][i]:
                acct["last_signal_ts"] = ts
                continue
            cooldown_ok = ts - acct["last_parent_signal_ts"] >= p["parent_spacing_hours"] * HOUR
            if not cooldown_ok:
                self._log_opp(sid, ts, "rejected", "cooldown",
                              {"cooldown_ok": False, "target_weight": self.feat["weight"][i]})
                acct["last_signal_ts"] = ts
                continue
            # accepted parent: consumes the cooldown even if the gate fails
            acct["last_parent_signal_ts"] = ts
            gate = self.feat["rvol"][i] >= p["rvol_min"]
            w = self.feat["weight"][i]
            extra = {"cooldown_ok": True, "gate": gate, "target_weight": w}
            if not gate:
                self._log_opp(sid, ts, "rejected", "rvol_below_%.1f" % p["rvol_min"], extra)
            elif acct["paused"]:
                self._log_opp(sid, ts, "missed", "paused", extra)
            elif acct["qty"] > 0 or acct["exit_due_ts"]:
                self._log_opp(sid, ts, "missed", "position_open", extra)
            elif exited_this_run:
                self._log_opp(sid, ts, "missed", "no_same_run_reentry", extra)
            elif ts != latest_ts or not self._fresh(ts):
                self._log_opp(sid, ts, "missed", "stale_past_entry_deadline", extra)
            elif not self.quote:
                self._log_opp(sid, ts, "missed", "no_executable_quote", extra)
            else:
                row = self._log_opp(sid, ts, "entered", "parent+rvol_gate", extra)
                buy_px, _ = fill_prices(self.quote)
                nav = acct["cash"]                      # flat, so NAV == cash
                qty = (w * nav) / (buy_px * (1 + FEE_RATE))   # fee reserved
                if qty > 0:
                    acct["last_entry_signal_ts"] = ts
                    rec = self._order(acct, "BUY", qty, row["event_id"],
                                      "rvol_breakout_entry", ts)
                    acct["entry_fill_ts"] = int(self.now)
                    acct["entry_event_id"] = row["event_id"]
                    acct["exit_due_ts"] = int(self.now) + p["hold_hours"] * HOUR
                    rec["exit_due_ts"] = acct["exit_due_ts"]
            acct["last_signal_ts"] = ts

    # ---- candidate A ----
    def run_sma(self, acct):
        sid = acct["strategy_id"]
        p = acct["params"]
        fresh_bars = self._new_closed(acct)
        if not fresh_bars:
            return
        # A delayed run acts once, on the latest eligible completed candle.
        # Intermediate candles are recorded as skipped, never back-filled.
        for b in fresh_bars[:-1]:
            acct["last_signal_ts"] = b[0]
        ts = fresh_bars[-1][0]
        i = self.index[ts]
        acct["last_signal_ts"] = ts
        w = None
        if self.feat["sma_fast"][i] is not None and self.feat["sma_slow"][i] is not None \
                and self.feat["weight"][i] is not None:
            trend = self.feat["sma_fast"][i] > self.feat["sma_slow"][i]
            w = self.feat["weight"][i] if trend else 0.0
        if w is None:
            self._log_opp(sid, ts, "skipped", "data_invalid_window")
            return
        if not self.quote:
            self._log_opp(sid, ts, "skipped", "no_executable_quote", {"target_weight": w})
            return
        mid = self.quote["mid"]
        nav = nav_of(acct, mid)
        held_value = acct["qty"] * mid
        drift = abs(w * nav - held_value)
        entering = acct["qty"] <= 1e-12 and w > 0
        exiting = acct["qty"] > 1e-12 and w == 0.0
        fresh = self._fresh(ts)
        late = 0 if fresh else self.now - (ts + HOUR) - ENTRY_DEADLINE_SEC

        if exiting:
            # mandatory exit — regardless of the band, even when late
            row = self._log_opp(sid, ts, "entered", "trend_down_exit",
                                {"target_weight": 0.0})
            self._order(acct, "SELL", acct["qty"], row["event_id"],
                        "trend_down_exit", ts, lateness=late)
            return
        if entering:
            if acct["paused"]:
                self._log_opp(sid, ts, "missed", "paused", {"target_weight": w})
            elif not fresh:
                self._log_opp(sid, ts, "missed", "stale_past_entry_deadline",
                              {"target_weight": w})
            else:
                row = self._log_opp(sid, ts, "entered", "trend_up_entry",
                                    {"target_weight": w})
                buy_px, _ = fill_prices(self.quote)
                qty = (w * nav) / (buy_px * (1 + FEE_RATE))
                qty = min(qty, acct["cash"] / (buy_px * (1 + FEE_RATE)))
                if qty > 0:
                    self._order(acct, "BUY", qty, row["event_id"], "trend_up_entry", ts)
            return
        if acct["qty"] > 1e-12 and w > 0 and drift > p["rebalance_band"] * nav:
            if not fresh:
                self._log_opp(sid, ts, "missed", "resize_stale_past_deadline",
                              {"target_weight": w})
                return
            row = self._log_opp(sid, ts, "entered", "vol_resize", {"target_weight": w})
            buy_px, sell_px = fill_prices(self.quote)
            if w * nav > held_value:
                qty = (w * nav - held_value) / (buy_px * (1 + FEE_RATE))
                qty = min(qty, acct["cash"] / (buy_px * (1 + FEE_RATE)))
                if qty > 0:
                    self._order(acct, "BUY", qty, row["event_id"], "vol_resize_up", ts)
            else:
                qty = min((held_value - w * nav) / sell_px, acct["qty"])
                if qty > 0:
                    self._order(acct, "SELL", qty, row["event_id"], "vol_resize_down", ts)
        # inside the band, or flat with trend down: nothing to do, by design

    # ---- candidate C: funding-extreme limit-entry (execution-hypothesis) ----
    def _zmap(self, ts, stale_limit):
        """(z, settlement_ts, rate) readable by the bar that OPENED at ts,
        from THIS run's successful fetch. Verbatim research mapping: newest
        settlement at or before the bar open; older than stale_limit -> z None.
        Returns (None, None, None) when the fetch failed this run."""
        import bisect
        fu = self.funding
        if not fu.get("ok"):
            return None, None, None
        j = bisect.bisect_right(fu["f_times"], ts) - 1
        if j < 0:
            return None, None, None
        s_ts = fu["f_times"][j]
        rate = fu["f_rates"][j] if fu.get("f_rates") else None
        if ts - s_ts > stale_limit:
            return None, s_ts, rate
        return fu["f_z"][j], s_ts, rate

    def run_funding(self, acct):
        sid = acct["strategy_id"]
        p = acct["params"]
        fu_ok = bool(self.funding.get("ok"))
        latest_ts = self.bars[-1][0] if self.bars else 0
        exited_this_run = False
        actions = {}                      # bar_ts -> action label for the audit CSV
        if "last_z_eval_ts" not in acct:  # first run after migration
            acct["last_z_eval_ts"] = acct["last_signal_ts"]

        def flag_exit_pending(ts, reason):
            if not any(x.get("kind") == "exit_unresolved" for x in acct["pending"]):
                acct["pending"].append({"kind": "exit_unresolved", "due_ts": int(self.now),
                                        "noted_ts": int(self.now),
                                        "note": "funding exit signalled but no executable quote"})
            acct["exit_signal_ts"] = ts
            acct["exit_reason"] = reason
            logger.error("[%s] exit signalled (%s) but no executable quote — retrying next run",
                         sid, reason)

        def do_exit(ts, reason):
            nonlocal exited_this_run
            late = max(0, int(self.now - (ts + HOUR)))
            self._order(acct, "SELL", acct["qty"],
                        acct.get("entry_event_id") or "%s:%d" % (sid, ts),
                        reason, ts, lateness=late)
            acct["entry_event_id"] = None
            acct["entry_fill_ts"] = None
            acct["exit_signal_ts"] = None
            acct["exit_reason"] = None
            acct["pending"] = [x for x in acct["pending"] if x.get("kind") != "exit_unresolved"]
            exited_this_run = True

        # 0) exit owed from an earlier run (signal seen, no quote then) — first,
        #    mandatory, never fabricated against a stale quote.
        if acct["qty"] > 0 and acct.get("exit_signal_ts") and self.quote:
            do_exit(acct["exit_signal_ts"], acct.get("exit_reason") or "funding_exit_z")

        # 1) resting-limit-order lifecycle — candle-driven, deterministic and
        #    idempotent: a restart or duplicate dispatch re-derives the same
        #    outcome from the same candles; once filled, resting is None and
        #    the position blocks any re-fill.
        r = acct.get("resting")
        if r:
            outcome = None                # (kind, reason, bar_ts)
            if fu_ok:
                for b in self.bars:
                    ts = b[0]
                    if ts < r["placed_ts"] or ts + HOUR > r["deadline_ts"]:
                        continue          # wholly-inside-window candles only
                    z, _s, _rt = self._zmap(ts, p["stale_limit_sec"])
                    if z is None or z >= p["z_exit"]:
                        # cancel-before-fill tie-break: z is known at the candle
                        # OPEN, a low-touch happens later inside the candle
                        outcome = ("cancelled",
                                   "cancelled_z_reverted" if z is not None
                                   else "cancelled_signal_stale", ts)
                        break
                    if b[3] <= r["limit_px"]:
                        outcome = ("filled", "limit_fill_entry", ts)
                        break
                if outcome is None and self.now >= r["deadline_ts"]:
                    outcome = ("expired", "expired_unfilled", None)
            # fu_ok False: lifecycle pauses — the cancel condition cannot be
            # evaluated without z, and filling through an unevaluated cancel
            # check could fabricate an entry the frozen rule would have
            # cancelled. Kraken keeps a rolling year, so the SAME candles are
            # re-evaluated deterministically once the feed returns; the
            # no_funding health alert covers a prolonged outage.
            if outcome:
                kind, reason, bts = outcome
                if kind == "filled":
                    fill_close = bts + HOUR
                    cash0, qty0 = acct["cash"], acct["qty"]
                    gross, fee = apply_buy(acct, r["qty"], r["limit_px"],
                                           fee_rate=p["maker_fee"])
                    acct["order_count"] += 1
                    q = self.quote
                    mid_v = q["mid"] if q else None
                    dpx = diag_reprice("BUY", mid_v) if mid_v else None
                    rec = {"order_id": "%s:%d:BUY" % (sid, r["signal_ts"]),
                           "event_id": r["event_id"], "strategy": sid, "side": "BUY",
                           "decision_ts": int(self.now),
                           "request_ts": round(q["request_ts"], 2) if q else "",
                           "quote_exchange_ts": q["exchange_ts"] if q else "",
                           "bid": q["bid"] if q else "", "ask": q["ask"] if q else "",
                           "mid": round(mid_v, 2) if mid_v else "",
                           "quote_size": "",
                           "fill_price": round(r["limit_px"], 2), "qty": round(r["qty"], 8),
                           "gross_notional": round(gross, 2), "fee": round(fee, 2),
                           "cash_before": round(cash0, 2), "qty_before": round(qty0, 8),
                           "cash_after": round(acct["cash"], 2),
                           "qty_after": round(acct["qty"], 8),
                           "basis_after": round(acct["cost_basis"], 2),
                           "realized_pnl_delta": 0.0, "reason": reason,
                           "signal_ts": r["signal_ts"], "exit_due_ts": "",
                           "lateness_sec": int(fill_close - r["placed_ts"]),
                           "diag_fill_price": round(dpx, 2) if dpx else "",
                           "diag_fee": round(r["qty"] * dpx * FEE_RATE, 2) if dpx else "",
                           "schema": SCHEMA_VERSION}
                    self.orders.append(rec)
                    acct["entry_event_id"] = r["event_id"]
                    acct["entry_fill_ts"] = fill_close
                    acct["last_entry_signal_ts"] = r["signal_ts"]
                    self.alerts.append((sid, "BUY", r["qty"], r["limit_px"], reason))
                    self.status_alerts.append(
                        "限價單成交:%s @ $%s(K線 %s 低位掂價;maker 2bp;掛咗 %d 分鐘)"
                        % (sid, f"{r['limit_px']:,.2f}", iso(bts),
                           (fill_close - r["placed_ts"]) // 60))
                    actions[bts] = "limit_filled"
                else:
                    self._log_opp(sid, r["signal_ts"], kind, reason,
                                  {"target_weight": 1.0})
                    self.status_alerts.append(
                        "限價單%s:%s @ $%s(%s)"
                        % ("取消" if kind == "cancelled" else "過期未成交",
                           sid, f"{r['limit_px']:,.2f}", reason))
                    if bts is not None:
                        actions[bts] = kind
                acct["resting"] = None
                r = None

        # 2) exit scan over every candle not yet evaluated WITH funding data —
        #    a feed outage must not swallow an exit crossing (last_z_eval_ts
        #    only advances on candles judged with real z data).
        if fu_ok:
            for b in self.bars:
                ts = b[0]
                if ts <= acct["last_z_eval_ts"]:
                    continue
                z, _s, _rt = self._zmap(ts, p["stale_limit_sec"])
                can_exit = (acct["qty"] > 0 and not exited_this_run
                            and (not acct.get("entry_fill_ts")
                                 or ts + HOUR > acct["entry_fill_ts"]))
                if can_exit and (z is None or z >= p["z_exit"]):
                    reason = "funding_exit_z" if z is not None else "signal_stale_exit"
                    if self.quote:
                        do_exit(ts, reason)
                        self._log_opp(sid, ts, "entered", reason, {"target_weight": 0.0})
                        actions[ts] = "exit"
                    else:
                        flag_exit_pending(ts, reason)
                        actions[ts] = "exit_pending"
                acct["last_z_eval_ts"] = ts

        # 3) entry attempts + signal audit rows over newly closed candles.
        for b in self._new_closed(acct):
            ts = b[0]
            z, s_ts, rate = self._zmap(ts, p["stale_limit_sec"])
            action = actions.get(ts, "")
            if action in ("", "limit_filled") and z is not None \
                    and z <= p["z_enter"] and acct["qty"] <= 1e-12:
                if acct.get("resting"):
                    self._log_opp(sid, ts, "rejected", "order_already_resting")
                    action = "already_resting"
                elif acct["paused"]:
                    self._log_opp(sid, ts, "missed", "paused")
                    action = "missed_paused"
                elif exited_this_run:
                    self._log_opp(sid, ts, "missed", "no_same_run_reentry")
                    action = "missed_same_run_reentry"
                elif ts != latest_ts or not self._fresh(ts):
                    self._log_opp(sid, ts, "missed", "stale_past_entry_deadline")
                    action = "missed_stale"
                elif not self.quote:
                    self._log_opp(sid, ts, "missed", "no_executable_quote")
                    action = "missed_no_quote"
                else:
                    row = self._log_opp(sid, ts, "resting_placed", "funding_z_entry",
                                        {"target_weight": 1.0})
                    bid = self.quote["bid"]
                    qty = acct["cash"] / (bid * (1 + p["maker_fee"]))
                    acct["resting"] = {"placed_ts": int(self.now), "limit_px": bid,
                                       "qty": qty,
                                       "deadline_ts": int(self.now) + p["limit_deadline_sec"],
                                       "signal_ts": ts, "event_id": row["event_id"]}
                    self.status_alerts.append(
                        "掛限價買單:%s @ $%s(z=%.2f;%s 到期;成交先算入市,唔會老作)"
                        % (sid, f"{bid:,.2f}", z, iso(acct["resting"]["deadline_ts"])))
                    action = "resting_placed"
            elif not action and not fu_ok and ts == latest_ts \
                    and acct["qty"] <= 1e-12 and not acct.get("resting"):
                # cannot know whether a signal fired — recorded, never invented
                self._log_opp(sid, ts, "missed", "no_funding_data")
                action = "no_funding_data"
            self.funding_rows.append({
                "ts": int(self.now), "time": iso(self.now), "strategy": sid,
                "bar_ts": ts, "bar_time": iso(ts),
                "settlement_ts": s_ts if s_ts is not None else "",
                "settlement_age_sec": (ts - s_ts) if s_ts is not None else "",
                "rate": ("%.10g" % rate) if rate is not None else "",
                "z": round(z, 4) if z is not None else "",
                "position": "long" if acct["qty"] > 1e-12
                            else ("resting" if acct.get("resting") else "flat"),
                "resting_limit_px": round(acct["resting"]["limit_px"], 2)
                                    if acct.get("resting") else "",
                "resting_deadline_ts": acct["resting"]["deadline_ts"]
                                       if acct.get("resting") else "",
                "action": action, "schema": SCHEMA_VERSION})
            acct["last_signal_ts"] = ts

        # 4) no-coast-blind fallback (frozen spec §4): with the live fetch down,
        #    a position may not outlive a 6h-stale last-known settlement.
        if acct["qty"] > 0 and not exited_this_run and not fu_ok and latest_ts:
            lk = self.funding.get("last_known_ts")
            if lk is None or latest_ts - lk > p["stale_limit_sec"]:
                if self.quote:
                    do_exit(latest_ts, "signal_stale_exit")
                    self._log_opp(sid, latest_ts, "entered", "signal_stale_exit",
                                  {"target_weight": 0.0})
                else:
                    flag_exit_pending(latest_ts, "signal_stale_exit")

    # ---- snapshots ----
    def snapshot(self, acct):
        hour_bucket = int(self.now) // HOUR
        if acct["last_equity_hour"] == hour_bucket:
            return                       # duplicate dispatch within the hour: no double row
        acct["last_equity_hour"] = hour_bucket
        if self.quote:
            mark, src = self.quote["mid"], "quote_mid"
        elif self.bars:
            mark, src = self.bars[-1][4], "last_close_stale"
        else:
            mark, src = None, "unavailable"
        if mark is None:
            return
        ok, nav, unreal = reconcile(acct, mark)
        acct["peak_nav"] = max(acct["peak_nav"], nav)
        status = "ok" if ok else "RECONCILE_FAIL"
        if not ok:
            logger.error("[%s] ledger-to-NAV reconciliation FAILED", acct["strategy_id"])
        if acct["pending"]:
            status += "|" + ";".join(x["kind"] for x in acct["pending"])
        self.snapshots.append({
            "ts": int(self.now), "time": iso(self.now), "strategy": acct["strategy_id"],
            "cash": round(acct["cash"], 2), "qty": round(acct["qty"], 8),
            "mark": round(mark, 2), "mark_source": src,
            "cost_basis": round(acct["cost_basis"], 2),
            "realized_pnl": round(acct["realized_pnl"], 2),
            "unrealized_pnl": round(unreal, 2), "fees_cum": round(acct["fees_cum"], 2),
            "nav": round(nav, 2),
            "exposure_pct": round(acct["qty"] * mark / nav * 100, 2) if nav > 0 else 0.0,
            "peak_nav": round(acct["peak_nav"], 2),
            "drawdown_pct": round((nav / acct["peak_nav"] - 1) * 100, 3),
            "data_status": status, "schema": SCHEMA_VERSION})

    def run(self):
        for sid, acct in self.state["accounts"].items():
            kind = acct["params"]["kind"]
            if kind == "rvol_breakout":
                self.run_rvol(acct)
            elif kind == "funding_limit":
                self.run_funding(acct)
            else:
                self.run_sma(acct)
            self.snapshot(acct)


# ------------------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------------------
def new_state(now, bars, quote):
    latest = bars[-1][0] if bars else int(now) // HOUR * HOUR - HOUR
    accounts = {}
    for sid in STRATEGIES:
        a = new_account(sid)
        # warmup only: candles closed before activation never trade
        a["last_signal_ts"] = latest
        a["last_entry_signal_ts"] = 0
        a["last_z_eval_ts"] = latest
        a["activation_ts"] = int(now)
        a["activation_utc"] = iso(now)
        accounts[sid] = a
    return {"schema_version": SCHEMA_VERSION,
            "mode": "PAPER / FORWARD LAB — no broker connected, unverified research leads",
            "activation_ts": int(now), "activation_utc": iso(now),
            "venue_epoch": {"venue": VENUE["venue"], "product": VENUE["product"],
                            "quote_ccy": VENUE["quote_ccy"],
                            "epoch_start_utc": iso(now), "epoch_start_ts": int(now)},
            "entry_deadline_sec": ENTRY_DEADLINE_SEC,
            "fill_model": {"fee_per_side": FEE_RATE, "min_half_spread": MIN_HALF_SPREAD,
                           "note": "buy=max(ask,mid*1.0002); sell=min(bid,mid*0.9998); "
                                   "paper assumption, not a guaranteed fill"},
            "btc_anchor": {"ts": int(now), "price": quote["mid"] if quote else None},
            "accounts": accounts, "last_run": {}, "updated_utc": None}


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    os.makedirs(LAB_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_FILE)


def append_csv(path, fields, rows):
    if not rows:
        return
    os.makedirs(LAB_DIR, exist_ok=True)
    fresh = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if fresh:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def archive_paths(now):
    mk = month_key(now)
    return (os.path.join(LAB_DIR, "orders_%s.csv" % mk),
            os.path.join(LAB_DIR, "opportunities_%s.csv" % mk),
            os.path.join(LAB_DIR, "equity_%s.csv" % mk),
            os.path.join(LAB_DIR, "funding_signal_%s.csv" % mk))


# ------------------------------------------------------------------------------
# Dashboard payload
# ------------------------------------------------------------------------------
def read_equity_history():
    rows = []
    if not os.path.isdir(LAB_DIR):
        return rows
    for name in sorted(os.listdir(LAB_DIR)):
        if name.startswith("equity_") and name.endswith(".csv"):
            with open(os.path.join(LAB_DIR, name), encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
    return rows


def downsample(points, cutoff_ts):
    """Older than cutoff: keep the last point per UTC day. Raw CSVs keep it all."""
    out, day_last = [], {}
    for ts, v in points:
        if ts >= cutoff_ts:
            out.append([ts, v])
        else:
            day_last[ts // 86400] = [ts, v]
    return [day_last[k] for k in sorted(day_last)] + out


def baseline_series(activation_ts):
    """Baseline account rebased to the shared forward start. Anchor is the last
    committed baseline NAV at/before activation; N/A when none exists."""
    try:
        with open(BASELINE_STATE, encoding="utf-8") as fh:
            b = json.load(fh)
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "baseline state unreadable"}
    curve = b.get("equity_curve") or []
    anchor = None
    for ts, eq in curve:
        if ts <= activation_ts:
            anchor = [ts, eq]
    if not anchor or anchor[1] <= 0:
        return {"available": False, "reason": "no baseline NAV at/before lab activation"}
    pts = [[int(ts), round((eq / anchor[1] - 1) * 100, 3)]
           for ts, eq in curve if ts >= anchor[0]]
    st = b.get("stats", {})
    return {"available": True, "anchor_ts": anchor[0], "anchor_time": iso(anchor[0]),
            "anchor_nav": anchor[1], "points": pts,
            "cash": b.get("cash"), "qty": b.get("qty"),
            "original_equity": st.get("equity"), "original_return_pct": st.get("total_return_pct"),
            "updated_utc": b.get("updated_utc"),
            "note": "baseline uses daily candles + its own fixed-cost fill model; "
                    "rebased % only — different execution/cost model from the lab"}


def build_summary(state, engine_orders_recent=None):
    hist = read_equity_history()
    cutoff = int(time.time()) - 45 * 86400
    per = {}
    btc_pts = []
    for sid in state["accounts"]:
        pts = [[int(r["ts"]), round((float(r["nav"]) / START_CASH - 1) * 100, 3)]
               for r in hist if r["strategy"] == sid]
        dd = [[int(r["ts"]), float(r["drawdown_pct"])] for r in hist if r["strategy"] == sid]
        per[sid] = {"returns": downsample(pts, cutoff), "drawdown": downsample(dd, cutoff)}
    anchor = state.get("btc_anchor") or {}
    if anchor.get("price"):
        seen = set()
        for r in hist:
            if r["ts"] in seen or r["mark_source"] == "unavailable":
                continue
            seen.add(r["ts"])
            btc_pts.append([int(r["ts"]),
                            round((float(r["mark"]) / anchor["price"] - 1) * 100, 3)])
        btc_pts.sort()
    orders, opps = [], []
    for name in sorted(os.listdir(LAB_DIR)) if os.path.isdir(LAB_DIR) else []:
        p = os.path.join(LAB_DIR, name)
        if name.startswith("orders_") and name.endswith(".csv"):
            with open(p, encoding="utf-8") as fh:
                orders.extend(csv.DictReader(fh))
        if name.startswith("opportunities_") and name.endswith(".csv"):
            with open(p, encoding="utf-8") as fh:
                opps.extend(csv.DictReader(fh))
    accounts = {}
    for sid, a in state["accounts"].items():
        last = [r for r in hist if r["strategy"] == sid]
        last = last[-1] if last else None
        resting = a.get("resting")
        accounts[sid] = {
            "strategy_id": sid, "version": a["version"], "param_hash": a["param_hash"],
            "params": a["params"], "paused": a["paused"],
            "cash": round(a["cash"], 2), "qty": round(a["qty"], 8),
            "cost_basis": round(a["cost_basis"], 2),
            "realized_pnl": round(a["realized_pnl"], 2), "fees_cum": round(a["fees_cum"], 2),
            "episodes": a["episodes"], "order_count": a["order_count"],
            "exit_due_ts": a.get("exit_due_ts"),
            "exit_due_time": iso(a["exit_due_ts"]) if a.get("exit_due_ts") else None,
            "activation_ts": a.get("activation_ts"),
            "activation_utc": a.get("activation_utc"),
            "resting": None if not resting else {
                "limit_px": round(resting["limit_px"], 2),
                "qty": round(resting["qty"], 8),
                "placed_ts": resting["placed_ts"], "placed_time": iso(resting["placed_ts"]),
                "deadline_ts": resting["deadline_ts"],
                "deadline_time": iso(resting["deadline_ts"]),
                "signal_ts": resting["signal_ts"]},
            "pending": a["pending"],
            "last_snapshot": last}
    return {"schema_version": SCHEMA_VERSION, "generated_utc": iso(time.time()),
            "mode": state["mode"], "activation_ts": state["activation_ts"],
            "activation_utc": state["activation_utc"],
            "venue_epoch": state["venue_epoch"], "entry_deadline_sec": ENTRY_DEADLINE_SEC,
            "fill_model": state["fill_model"], "start_cash": START_CASH,
            "code_commit": os.environ.get("GITHUB_SHA", ""),
            "last_run": state.get("last_run", {}),
            "health": state.get("health", {}),
            "evaluation_protocol": {"file": "evaluation_protocol.md",
                                    "sha256": protocol_sha256()},
            "funding_candidate_spec": {"file": "funding_candidate_spec.md",
                                       "sha256": file_sha256("funding_candidate_spec.md")},
            "funding_last": state.get("funding_last"),
            "accounts": accounts, "series": per, "btc_reference": btc_pts,
            "baseline": baseline_series(state["activation_ts"]),
            "orders_recent": orders[-60:][::-1], "opportunities_recent": opps[-80:][::-1],
            "honesty": {"label": "研究紙盤:未驗證優勢 — A/B 係事後、樣本薄嘅研究線索;"
                                  "C 係執行假說測試(訊號零成本先有料,taker 成本蝕錢,"
                                  "測緊限價單實際接唔接到貨)。三個都唔係已驗證有優勢嘅系統;"
                                  "歷史正回報唔代表將來有錢賺。",
                        "never_claims": ["validated", "profitable system"]}}


def write_summary(state):
    os.makedirs(LAB_DIR, exist_ok=True)
    tmp = SUMMARY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(build_summary(state), fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, SUMMARY_FILE)


# ------------------------------------------------------------------------------
# Run health (operational alerts, distinct from trade alerts)
# ------------------------------------------------------------------------------
def health_bucket(state):
    """state['health'] with defaults — the live state predates this field."""
    h = state.get("health")
    if not isinstance(h, dict):
        h = {}
    h.setdefault("consec", {})
    h.setdefault("last_alert", {})
    state["health"] = h
    return h


def evaluate_health(state, issues, now):
    """Update consecutive-run counters for `issues` (set of kinds) and return
    the kinds that should alert THIS run. Serious kinds alert immediately;
    data-weather kinds need HEALTH_CONSEC consecutive runs; every kind is then
    rate-limited to one alert per HEALTH_MIN_REPEAT_SEC."""
    h = health_bucket(state)
    for kind in list(h["consec"]):
        if kind not in issues:
            h["consec"][kind] = 0
    fire = []
    for kind in sorted(issues):
        n = h["consec"].get(kind, 0) + 1
        h["consec"][kind] = n
        if n < HEALTH_CONSEC.get(kind, 1):
            continue
        if now - h["last_alert"].get(kind, 0) >= HEALTH_MIN_REPEAT_SEC:
            h["last_alert"][kind] = int(now)
            fire.append(kind)
    h["current_issues"] = sorted(issues)
    h["checked_utc"] = iso(now)
    return fire


def collect_run_issues(bars, quote, engine, state, funding_ok=True):
    issues = set()
    if not bars:
        issues.add("no_candles")
    if quote is None:
        issues.add("no_quote")
    if not funding_ok:
        issues.add("no_funding")
    if engine is not None and any("RECONCILE_FAIL" in (s.get("data_status") or "")
                                  for s in engine.snapshots):
        issues.add("reconcile_fail")
    if any(a.get("pending") for a in state.get("accounts", {}).values()):
        issues.add("exit_unresolved")
    return issues


def file_sha256(name):
    try:
        with open(os.path.join(LAB_DIR, name), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def protocol_sha256():
    return file_sha256("evaluation_protocol.md")


# ------------------------------------------------------------------------------
# Telegram (same secrets/pattern as the baseline; PAPER LAB label, never blocks)
# ------------------------------------------------------------------------------
def send_lab_alerts(alerts):
    if not alerts:
        return
    try:
        from btc_autotrade import send_telegram, telegram_configured
    except Exception:  # noqa: BLE001
        return
    if not telegram_configured():
        logger.info("%d lab alert(s), Telegram not configured.", len(alerts))
        return
    for sid, side, qty, px, reason in alerts:
        send_telegram("🧪 FORWARD LAB — PAPER trade, no broker, no real money\n\n"
                      "%s %s %.6f BTC @ $%s\nreason: %s\n\n"
                      "Unverified research candidate; not a validated system."
                      % (side, sid, qty, f"{px:,.2f}", reason))


def send_status_alerts(lines):
    """Resting-limit-order lifecycle for candidate C (placed / filled /
    expired / cancelled) — meaningful position-pipeline events only, batched
    into ONE message per run. Never a trade signal, never blocks."""
    if not lines:
        return
    try:
        from btc_autotrade import send_telegram, telegram_configured
    except Exception:  # noqa: BLE001
        return
    if not telegram_configured():
        logger.info("%d lab status alert(s), Telegram not configured.", len(lines))
        return
    send_telegram("🧪 FORWARD LAB — PAPER,冇 broker、冇真單(限價單狀態)\n\n"
                  + "\n".join("• " + s for s in lines)
                  + "\n\n執行假說測試,唔係已驗證系統。")


def send_health_alerts(kinds, extra_lines=None):
    """Operational health Telegram — NEVER a trade signal, never blocks."""
    if not kinds and not extra_lines:
        return
    try:
        from btc_autotrade import send_telegram, telegram_configured
    except Exception:  # noqa: BLE001
        return
    lines = ["• " + HEALTH_TEXT.get(k, k) for k in kinds] + list(extra_lines or [])
    if not telegram_configured():
        logger.info("Health alert (Telegram not configured): %s", "; ".join(lines))
        return
    send_telegram("🩺 LAB HEALTH — 前向實驗室運行警報(唔係交易訊號)\n\n"
                  + "\n".join(lines)
                  + "\n\n交易照凍結規則行;呢個只係數據/運行健康通知。")


# ------------------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------------------
def wait_for_current_candle(now_fn=time.time, fetch=fetch_candles, sleep=time.sleep):
    """Bounded wait for the just-closed hourly candle to appear on the feed."""
    bars = fetch(now_fn())
    expected = (int(now_fn()) // HOUR - 1) * HOUR
    tries = 0
    while bars and bars[-1][0] < expected and tries < CANDLE_RETRIES:
        tries += 1
        logger.info("Latest closed candle %s not yet published (expect %s) — retry %d/%d",
                    iso(bars[-1][0]), iso(expected), tries, CANDLE_RETRIES)
        sleep(CANDLE_RETRY_SLEEP)
        bars = fetch(now_fn())
    return bars


def run_once():
    started = time.time()
    try:
        bars = wait_for_current_candle()
    except Exception as exc:  # noqa: BLE001
        logger.error("Candle fetch failed: %s", exc)
        bars = []
    quote = fetch_quote()
    funding_live = fsig.fetch_kraken_funding()      # None = weather, never invented
    state = load_state()
    activated = False
    if state is None:
        if not bars or quote is None:
            logger.error("Cannot activate the lab without both candles and a quote.")
            return 1
        state = new_state(started, bars, quote)
        activated = True
        logger.info("ACTIVATED forward lab at %s (venue %s:%s). Warmup only — "
                    "no pre-activation candle will ever trade.",
                    state["activation_utc"], VENUE["venue"], VENUE["product"])
    else:
        # later-added strategies (candidate C) join with their own epoch
        ensure_accounts(state, started, bars)
    if funding_live:
        state["funding_last"] = {
            "newest_settlement_ts": funding_live["newest_settlement_ts"],
            "newest_settlement_utc": iso(funding_live["newest_settlement_ts"]),
            "fetched_utc": iso(time.time()),
            "n_settlements": len(funding_live["f_times"])}
    funding_ctx = {"ok": funding_live is not None,
                   "f_times": funding_live["f_times"] if funding_live else [],
                   "f_z": funding_live["f_z"] if funding_live else [],
                   "f_rates": funding_live["f_rates"] if funding_live else [],
                   "last_known_ts": (state.get("funding_last") or {})
                                    .get("newest_settlement_ts")}
    if not bars and quote is None:
        state["last_run"] = {"time": iso(started), "ok": False,
                             "error": "no candles and no quote"}
        fire = evaluate_health(state,
                               collect_run_issues(bars, quote, None, state,
                                                  funding_ok=funding_ctx["ok"]),
                               time.time())
        state["updated_utc"] = iso(time.time())
        save_state(state)
        write_summary(state)
        send_health_alerts(fire)
        return 0

    eng = LabEngine(state, time.time(), bars, quote, funding=funding_ctx)
    eng.run()
    op, opp, eqp, fup = archive_paths(eng.now)
    append_csv(op, ORDER_FIELDS, eng.orders)
    append_csv(opp, OPP_FIELDS, eng.opps)
    append_csv(eqp, EQ_FIELDS, eng.snapshots)
    append_csv(fup, FUND_FIELDS, eng.funding_rows)
    state["last_run"] = {"time": iso(eng.now), "ok": True,
                         "quote_ok": quote is not None,
                         "funding_ok": funding_ctx["ok"],
                         "bars": len(bars), "latest_bar": iso(bars[-1][0]) if bars else None,
                         "orders": len(eng.orders), "opportunities": len(eng.opps),
                         "activated": activated,
                         "seconds": round(time.time() - started, 2)}
    fire = evaluate_health(state,
                           collect_run_issues(bars, quote, eng, state,
                                              funding_ok=funding_ctx["ok"]),
                           time.time())
    state["updated_utc"] = iso(time.time())
    save_state(state)
    write_summary(state)
    send_lab_alerts(eng.alerts)
    send_status_alerts(eng.status_alerts)
    send_health_alerts(fire)
    for sid, a in state["accounts"].items():
        mark = quote["mid"] if quote else (bars[-1][4] if bars else 0)
        logger.info("[%s] NAV $%.2f | cash $%.2f | %.6f BTC | realized $%.2f | "
                    "episodes %d | orders %d%s", sid, nav_of(a, mark), a["cash"], a["qty"],
                    a["realized_pnl"], a["episodes"], a["order_count"],
                    " | PAUSED" if a["paused"] else "")
    return 0


def dry_run():
    bars = fetch_candles()
    quote = fetch_quote()
    if not bars:
        logger.warning("No candles reachable; dry run ends (weather, not a defect).")
        return 0
    f = build_features(contiguous_tail(bars))
    i = len(f["weight"]) - 1
    logger.info("Closed bars: %d (last %s) | quote %s",
                len(bars), iso(bars[-1][0]),
                "bid %.2f / ask %.2f" % (quote["bid"], quote["ask"]) if quote else "UNAVAILABLE")
    if f["weight"][i] is None:
        logger.error("Feed window cannot cover the 721-bar warmup — defect.")
        return 1
    logger.info("SMA24 %.2f vs SMA720 %.2f -> trend %s | sigma %.1f%% | w=%.3f",
                f["sma_fast"][i], f["sma_slow"][i],
                "UP" if f["sma_fast"][i] > f["sma_slow"][i] else "DOWN",
                f["sigma"][i] * 100, f["weight"][i] or 0)
    logger.info("breakout=%s rvol=%s (den %s)", f["breakout"][i],
                "%.2f" % f["rvol"][i] if f["rvol"][i] else "n/a",
                "%.2f" % f["rvol_den"][i] if f["rvol_den"][i] else "n/a")
    # candidate C signal (Kraken funding) — weather never fails the dry run
    fu = fsig.fetch_kraken_funding()
    if fu is None:
        logger.warning("Funding (Kraken) unreachable — candidate C would treat "
                       "this run as data weather (no entries, no invented signal).")
    else:
        p = STRATEGIES["funding_z168_limit_entry_v1"]
        latest = contiguous_tail(bars)[-1][0]
        z, s_ts = fsig.z_at(latest, fu["f_times"], fu["f_z"], p["stale_limit_sec"])
        logger.info("funding: %d settlements, newest %s (age vs latest bar open: %d min)",
                    len(fu["f_times"]), iso(fu["newest_settlement_ts"]),
                    (latest - (s_ts or 0)) // 60 if s_ts else -1)
        if z is None:
            logger.info("funding z: None (stale/warmup) -> candidate C stays flat")
        else:
            act = ("ENTER (would place resting limit at bid)" if z <= p["z_enter"]
                   else ("EXIT territory (flat stays flat; a long would sell)"
                         if z >= p["z_exit"] else "hold zone (sticky, no action)"))
            logger.info("funding z=%.3f -> %s", z, act)
    logger.info("Dry run OK — nothing written, no paper account touched.")
    return 0


def markdown_summary():
    state = load_state()
    lines = ["### 🧪 BTC Forward Lab — paper/demo (unverified research candidates)", ""]
    if not state:
        lines.append("_Not activated yet._")
        return "\n".join(lines)
    lines += ["Activated %s · venue %s:%s · every account started at $100,000 "
              "(later-added accounts carry their own activation epoch)"
              % (state["activation_utc"], state["venue_epoch"]["venue"],
                 state["venue_epoch"]["product"]), "",
              "| account | NAV basis | cash | BTC | realized | episodes | orders | status |",
              "|---|---|---|---|---|---|---|---|"]
    for sid, a in state["accounts"].items():
        status = "PAUSED" if a["paused"] else ("holding" if a["qty"] > 0 else "flat")
        if a.get("resting"):
            status += " · resting limit $%.0f" % a["resting"]["limit_px"]
        if a["pending"]:
            status += " ⚠" + ",".join(x["kind"] for x in a["pending"])
        lines.append("| %s | $%s | $%s | %.6f | $%s | %d | %d | %s |"
                     % (sid, f"{a['start_cash']:,.0f}", f"{a['cash']:,.2f}", a["qty"],
                        f"{a['realized_pnl']:,.2f}", a["episodes"], a["order_count"], status))
    lines += ["", "> Paper fills only; no broker; candidates are NOT validated alpha."]
    return "\n".join(lines)


def health_check():
    """Read-only watchdog, meant to run from an INDEPENDENT schedule (the daily
    baseline workflow): catches the failure mode the hourly run can't see —
    the hourly workflow itself silently not running. Writes nothing; the daily
    cadence is the rate limit. Always exits 0 so a watchdog bug can never take
    down its host job."""
    state = load_state()
    if state is None:
        logger.info("Watchdog: lab not activated yet — nothing to check.")
        return 0
    problems = []
    now = time.time()
    stamp = (state.get("last_run") or {}).get("time") or state.get("updated_utc")
    try:
        ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M UTC") \
                     .replace(tzinfo=timezone.utc).timestamp()
        age_h = (now - ts) / 3600
        if now - ts > WATCHDOG_STALE_SEC:
            problems.append("• lab state 已 %.1f 小時冇更新 — hourly workflow 可能死咗"
                            "或者被 GitHub 停咗 schedule" % age_h)
    except Exception:  # noqa: BLE001
        problems.append("• lab state 嘅 last_run 時間解析唔到(%r)" % stamp)
    if not (state.get("last_run") or {}).get("ok", True):
        problems.append("• 最後一次 run 標記為失敗:%s"
                        % (state["last_run"].get("error") or "unknown"))
    for sid, a in (state.get("accounts") or {}).items():
        for p in a.get("pending") or []:
            problems.append("• %s 未解決:%s(%s 起)"
                            % (sid, p.get("kind"), iso(p.get("noted_ts", 0))))
    eq_path = archive_paths(now)[2]
    if os.path.exists(eq_path):
        with open(eq_path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        bad = [r for r in rows[-48:] if "RECONCILE_FAIL" in (r.get("data_status") or "")]
        if bad:
            problems.append("• 過去48個快照有 %d 個 RECONCILE_FAIL(最近 %s)"
                            % (len(bad), bad[-1].get("time")))
    if not problems:
        logger.info("Watchdog: lab healthy (state age OK, no pending, no reconcile fails).")
        return 0
    for p in problems:
        logger.error("Watchdog: %s", p)
    send_health_alerts([], extra_lines=problems)
    return 0


def set_paused(sid, paused):
    state = load_state()
    if not state or sid not in state["accounts"]:
        logger.error("Unknown strategy %r (activated: %s)", sid,
                     list(state["accounts"]) if state else "not yet")
        return 1
    acct = state["accounts"][sid]
    acct["paused"] = paused
    now = time.time()
    if paused and acct.get("resting"):
        # frozen spec: pausing cancels the resting limit order (logged,
        # append-only) and blocks new placements; exits are unaffected.
        r = acct["resting"]
        acct["resting"] = None
        row = {"event_id": r["event_id"], "strategy": sid,
               "signal_ts": r["signal_ts"], "signal_time": iso(r["signal_ts"]),
               "observed_ts": int(now), "observed_late": False,
               "target_weight": 1.0, "cooldown_ok": None, "gate": None,
               "decision": "cancelled", "reason": "cancelled_paused",
               "data_status": "ok",
               "venue": "%s:%s" % (VENUE["venue"], VENUE["product"]),
               "schema": SCHEMA_VERSION}
        append_csv(archive_paths(now)[1], OPP_FIELDS, [row])
        logger.info("[%s] resting limit order cancelled by pause (limit $%.2f).",
                    sid, r["limit_px"])
    state["updated_utc"] = iso(now)
    save_state(state)
    write_summary(state)
    logger.info("%s %s. History untouched; due exits and snapshots continue.",
                sid, "PAUSED (no new entries)" if paused else "RESUMED")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="BTC forward paper lab (no broker connected)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--health-check", action="store_true",
                    help="read-only watchdog: alert if lab state is stale / "
                         "pending exits / reconcile failures; writes nothing")
    ap.add_argument("--pause", metavar="STRATEGY_ID")
    ap.add_argument("--resume", metavar="STRATEGY_ID")
    args = ap.parse_args(argv)
    if args.summary:
        print(markdown_summary())
        return 0
    if args.health_check:
        return health_check()
    if args.dry_run:
        return dry_run()
    if args.pause:
        return set_paused(args.pause, True)
    if args.resume:
        return set_paused(args.resume, False)
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
