#!/usr/bin/env python3
"""
================================================================================
  BTC Live Auto-Trader — DEMO / PAPER TRADING ONLY
================================================================================
  Pulls live BTC-USD candles from public exchange APIs, runs an EMA-cross +
  RSI/ATR strategy, and books the fills into a simulated (paper) account.

  *** NO BROKER IS CONNECTED. NO IBKR. NO REAL ORDERS ARE EVER SENT. ***

  Everything lives in plain JSON/CSV under ./btc_demo/ so a GitHub Action can
  run it every 15 minutes and commit the updated state + dashboard back.

  Usage:
      python btc_autotrade.py              # one pass (what CI runs)
      python btc_autotrade.py --loop       # keep running locally, live demo
      python btc_autotrade.py --reset      # wipe the paper account, start over
      python btc_autotrade.py --backfill 300  # seed history on the first run

  Dependencies: Python 3.9+ standard library only.
================================================================================
"""

import argparse
import csv
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("btc-paper")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "btc_demo")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
TRADES_CSV = os.path.join(OUT_DIR, "trades.csv")
DASHBOARD = os.path.join(OUT_DIR, "index.html")

STATE_VERSION = 2          # position shape changed with the strategy
DAYS_PER_YEAR = 365.0


# ------------------------------------------------------------------------------
# Configuration (every value can be overridden with an env var of the same name)
# ------------------------------------------------------------------------------
def _env(name, default, cast=float):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("Bad value for %s=%r, using default %r", name, raw, default)
        return default


# The strategy below is not a guess. It is the parameter set that the most
# independent asset-splits agreed on in btc_backtest.py — chosen because seven
# assets and thirty-five train/test splits kept landing on it, NOT because it
# scored highest on BTC. The single best-on-BTC parameters collapsed from
# Calmar 1.39 to -0.13 when applied to the other assets; this one held up.
#
# On BTC over ten years: Calmar 1.14 / Sharpe 1.44 / -46% worst drawdown,
# against buy & hold's 0.74 / 1.05 / -83%. In the sealed two-year holdout that
# was never used to choose anything: +51.7% against +28.5%, drawdown 30%
# against 53%. It gives up raw return (+6,885% vs +12,325% over the decade)
# because it is only in the market about 43% of the time.
CONFIG = {
    # market / data
    #
    # Daily candles, and the job runs once a day. Those two have to match. On
    # hourly candles with a two-hourly job, the engine books a fill at an open
    # that is already an hour or two in the past by the time the job wakes —
    # a price the schedule could never actually have reached. Matching the
    # candle to the cadence removes the overstatement entirely.
    #
    # It costs almost nothing: the strategy trades 9.7 times a year, and from
    # one-hour to one-day candles its Sharpe sits between 1.42 and 1.44. The
    # hourly resolution was never doing any work.
    "granularity": int(_env("BTC_GRANULARITY", 86400, int)),   # daily candles
    "history_bars": int(_env("BTC_HISTORY_BARS", 200, int)),   # ~6 months, covers the 30d average
    # strategy: dual SMA trend, position sized by realised volatility
    "sma_fast": int(_env("BTC_SMA_FAST", 3, int)),       # 3 days
    "sma_slow": int(_env("BTC_SMA_SLOW", 30, int)),      # 30 days
    "target_vol": _env("BTC_TARGET_VOL", 45.0),          # annualised %, the risk budget
    "vol_window": int(_env("BTC_VOL_WINDOW", 30, int)),  # 30 days of returns
    "rebalance_band": _env("BTC_REBALANCE_BAND", 0.10),  # only resize on a 10% drift
    # Account-level circuit breaker. Once the account is `dd_limit` below its
    # own peak the whole book is scaled to `dd_throttle`, and it stays there
    # until the drawdown heals to `dd_recover`.
    #
    # This is a risk-appetite dial, not an edge. Across eight assets it cuts
    # the median worst drawdown from 48% to 32% and costs roughly a third of
    # the return. Set BTC_DD_LIMIT=0 to switch it off entirely.
    #
    # Note what it does NOT do: the strategy's worst calendar year without any
    # breaker is already -14% (buy & hold's is -59%), because the trend filter
    # and volatility sizing do most of the work. The breaker compresses pain
    # WITHIN a year, it does not rescue a bad one.
    "dd_limit": _env("BTC_DD_LIMIT", 0.15),        # trip at -15% from the peak
    "dd_throttle": _env("BTC_DD_THROTTLE", 0.50),  # scale the book to half
    "dd_recover": _env("BTC_DD_RECOVER", 0.05),    # release within 5% of the peak
    # paper account
    "start_cash": _env("BTC_START_CASH", 100_000.0),
    "fee_bps": _env("BTC_FEE_BPS", 10.0),                # 0.10% taker fee
    "slippage_bps": _env("BTC_SLIPPAGE_BPS", 5.0),
    # housekeeping
    "keep_bars": int(_env("BTC_KEEP_BARS", 400, int)),
    "keep_equity": int(_env("BTC_KEEP_EQUITY", 2000, int)),
    "keep_trades": int(_env("BTC_KEEP_TRADES", 400, int)),
}


# ------------------------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------------------------
UA = "btc-paper-demo/1.0 (+github actions; educational paper trading)"


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------------------
# Telegram
#
# Off unless both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are in the
# environment, so a clone, a fork or a local run never tries to message
# anybody. In CI they come from repository secrets.
#
# Only position changes are sent. This strategy trades about ten times a year
# and a daily "nothing happened" would teach you to ignore the one message
# that matters. TRIM is excluded for the same reason: volatility sizing nudges
# the position far more often than it opens or closes one.
# ------------------------------------------------------------------------------
NOTIFY_KINDS = tuple(
    k.strip().upper()
    for k in os.environ.get("BTC_NOTIFY_KINDS", "BUY,SELL,BRAKE,CLEAR").split(",")
    if k.strip()
)
TELEGRAM_API = "https://api.telegram.org/bot%s/sendMessage"

# A fork should link to its own Actions tab, not to this one. GitHub sets
# GITHUB_REPOSITORY on every runner; the fallback is only for local renders.
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "pepe760/USstockpatternV20260412")
ACTIONS_URL = "https://github.com/%s/actions/workflows/btc_autotrade.yml" % REPO_SLUG


def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def format_alert(kind, text, price, state):
    """
    The message a person reads on their phone.

    It says PAPER on the first line every time. Someone glancing at a phone
    sees "BUY" and a bitcoin price; what stops that from being mistaken for
    advice is the label being impossible to miss, not a footnote.
    """
    st = state.get("stats", {})
    head = {"BUY": "🟢 BUY", "SELL": "🔴 SELL",
            "BRAKE": "🟠 BRAKE", "CLEAR": "🔵 CLEAR"}.get(kind, kind)
    lines = [
        "%s — PAPER TRADE, no broker, no real money" % head,
        "",
        text,
        "",
        "BTC $%s" % f"{price:,.2f}",
        "Paper equity $%s (%+.2f%%)" % (f"{st.get('equity', 0.0):,.2f}",
                                        st.get("total_return_pct", 0.0)),
        "Buy & hold over the same period %+.2f%%" % st.get("buy_hold_return_pct", 0.0),
    ]
    return "\n".join(lines)


def send_telegram(text, timeout=15):
    """
    True if Telegram accepted it. Never raises, and never puts the token in a
    log line: a failed alert must not take down a trading run, but it must not
    disappear either, so the caller warns.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        return False
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_API % token, data=payload,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            logger.warning("Telegram refused the alert: %s", str(body.get("description"))[:120])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        # str(exc) on a urllib error carries the URL, and the URL carries the
        # token. Report the type only.
        logger.warning("Telegram alert failed: %s", type(exc).__name__)
        return False


def notify_new_signals(state, new_signals, price):
    """Send one message per position change produced by this run."""
    worth = [s for s in new_signals if s.get("kind") in NOTIFY_KINDS]
    if not worth:
        return 0
    if not telegram_configured():
        logger.info("%d alert(s) worth sending, but Telegram is not configured.", len(worth))
        return 0
    sent = 0
    for sig in worth:
        if send_telegram(format_alert(sig["kind"], sig.get("text", ""), price, state)):
            sent += 1
    if sent < len(worth):
        # A GitHub warning annotation shows on the run page. This repo has been
        # fooled once by a green run that quietly did nothing.
        print("::warning title=Telegram alert failed::%d of %d alert(s) were not delivered."
              % (len(worth) - sent, len(worth)))
    return sent


# ------------------------------------------------------------------------------
# Data sources. Each returns ascending [ts, open, high, low, close, volume].
# We try them in order so one exchange being blocked/down never kills the run.
# ------------------------------------------------------------------------------
def fetch_coinbase(gran, limit):
    """Coinbase caps a response at 300 candles, so page backwards to reach `limit`."""
    allowed = [60, 300, 900, 3600, 21600, 86400]
    if gran not in allowed:
        raise ValueError("coinbase granularity %s unsupported" % gran)
    out, end = {}, int(time.time())
    # Bounded: stop on an empty page, on a page that adds nothing new (a stale
    # or pinned endpoint would otherwise loop forever), and at a hard page cap.
    max_pages = max(1, min(20, limit // 300 + 2))
    for _ in range(max_pages):
        if len(out) >= limit:
            break
        start = end - gran * 300
        url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles"
               "?granularity=%d&start=%s&end=%s"
               % (gran,
                  datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                  datetime.fromtimestamp(end, tz=timezone.utc).isoformat()))
        raw = http_json(url)
        if not raw:
            break
        before = len(out)
        for r in raw:
            out[int(r[0])] = [int(r[0]), float(r[3]), float(r[2]), float(r[1]),
                              float(r[4]), float(r[5])]
        if len(out) == before:
            break
        end = start
        time.sleep(0.2)
    bars = [out[k] for k in sorted(out)]
    return bars[-limit:]


def fetch_kraken(gran, limit):
    if gran % 60 != 0:
        raise ValueError("kraken needs whole minutes")
    url = "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=%d" % (gran // 60)
    raw = http_json(url)
    if raw.get("error"):
        raise RuntimeError("kraken error: %s" % raw["error"])
    series = next(iter(raw["result"].values())) if raw.get("result") else []
    bars = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])] for r in series]
    bars.sort(key=lambda b: b[0])
    return bars[-limit:]


def fetch_binance(gran, limit):
    tf = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 86400: "1d"}.get(gran)
    if not tf:
        raise ValueError("binance interval %s unsupported" % gran)
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=%s&limit=%d" % (tf, min(limit, 1000))
    raw = http_json(url)
    bars = [[int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
    bars.sort(key=lambda b: b[0])
    return bars[-limit:]


def fetch_bitstamp(gran, limit):
    allowed = [60, 180, 300, 900, 1800, 3600, 14400, 86400]
    if gran not in allowed:
        raise ValueError("bitstamp step %s unsupported" % gran)
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=%d&limit=%d" % (gran, min(limit, 1000))
    # Bitstamp returns up to 1000 in one call, which covers a 30-day hourly average
    raw = http_json(url)
    series = raw["data"]["ohlc"]
    bars = [[int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r["volume"])] for r in series]
    bars.sort(key=lambda b: b[0])
    return bars[-limit:]


# Ordered by how much history each can return in one pass. Kraken caps at 720
# candles, which no longer covers a 30-day hourly average plus warm-up, so it
# sits last and is only used if everything else is unreachable.
SOURCES = [
    ("bitstamp", fetch_bitstamp),
    ("binance", fetch_binance),
    ("coinbase", fetch_coinbase),
    ("kraken", fetch_kraken),
]


def fetch_bars(gran, limit):
    """Return (source_name, closed_bars). Raises only if every source fails."""
    errors = []
    now = int(time.time())
    for name, fn in SOURCES:
        try:
            bars = fn(gran, limit)
            # keep only candles that have actually closed
            bars = [b for b in bars if b[0] + gran <= now]
            if len(bars) < 30:
                raise RuntimeError("only %d closed bars returned" % len(bars))
            logger.info("Data source: %s (%d closed bars, last %s)",
                        name, len(bars), iso(bars[-1][0]))
            return name, bars
        except Exception as exc:  # noqa: BLE001 - we genuinely want the next source
            errors.append("%s: %s" % (name, exc))
            logger.warning("Source %s failed -> %s", name, exc)
    raise RuntimeError("all data sources failed | " + " | ".join(errors))


# ------------------------------------------------------------------------------
# Indicators
# ------------------------------------------------------------------------------
def candle_label(gran):
    if gran >= 86400:
        return "%dd" % (gran // 86400)
    if gran >= 3600:
        return "%dh" % (gran // 3600)
    return "%dm" % (gran // 60)


def sma_series(values, period):
    """Simple moving average; None until the window is full."""
    out, run = [None] * len(values), 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def vol_series(closes, period, bars_per_day=1.0):
    """
    Annualised standard deviation of hourly log returns, in percent.

    This is what sizes the position: the strategy aims for a constant amount of
    risk rather than a constant amount of money, so a calm market gets a bigger
    position than a wild one.
    """
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0
                    for i in range(1, len(closes))]
    out, s1, s2 = [None] * len(closes), 0.0, 0.0
    ann = math.sqrt(bars_per_day * DAYS_PER_YEAR) * 100.0
    for i, r in enumerate(rets):
        s1 += r
        s2 += r * r
        if i >= period:
            old = rets[i - period]
            s1 -= old
            s2 -= old * old
        if i >= period - 1:
            mean = s1 / period
            out[i] = math.sqrt(max(s2 / period - mean * mean, 0.0)) * ann
    return out


def target_weights(bars, cfg):
    """
    The share of equity the strategy wants to hold at each bar, 0.0 to 1.0.

    Long or flat only — it never shorts. Trend decides IF (fast average above
    slow), volatility decides HOW MUCH (target / realised, capped at fully
    invested). This is the same rule, with the same parameters, that
    btc_backtest.py measured; keeping one definition is the only way the
    backtest can honestly describe what this bot does.
    """
    closes = [b[4] for b in bars]
    fast = sma_series(closes, cfg["sma_fast"])
    slow = sma_series(closes, cfg["sma_slow"])
    vol = vol_series(closes, cfg["vol_window"], 86400.0 / cfg["granularity"])
    out = [0.0] * len(bars)
    for i in range(len(bars)):
        if fast[i] is None or slow[i] is None or vol[i] is None or vol[i] <= 1e-9:
            continue
        if fast[i] > slow[i]:
            out[i] = min(1.0, cfg["target_vol"] / vol[i])
    return out, fast, slow, vol


# ------------------------------------------------------------------------------
# Paper account state
# ------------------------------------------------------------------------------
def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def new_state(cfg):
    return {
        "version": STATE_VERSION,
        "mode": "PAPER / DEMO — no broker connected",
        "created_utc": iso(time.time()),
        "updated_utc": None,
        "config": dict(cfg),
        "cash": float(cfg["start_cash"]),
        "qty": 0.0,                 # BTC held; the strategy scales this, it is not all-or-nothing
        "entry_price": 0.0,
        "entry_ts": 0,
        "entry_time": None,
        "fees_open": 0.0,
        "target_weight": 0.0,
        "realised_vol": None,
        "peak_equity": float(cfg["start_cash"]),
        "throttled": False,
        "last_bar_ts": 0,
        "first_price": None,
        "bars": [],
        "equity_curve": [],
        "trades": [],
        "signals": [],
        "stats": {},
        "last_run": {},
    }


def load_state(cfg, reset=False):
    if reset or not os.path.exists(STATE_FILE):
        logger.info("Starting a fresh paper account with $%s", f"{cfg['start_cash']:,.0f}")
        return new_state(cfg)
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("version") != STATE_VERSION:
        logger.warning("State version mismatch, resetting paper account")
        return new_state(cfg)
    state["config"] = dict(cfg)  # config tweaks apply going forward
    return state


def save_state(state):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=False)
    os.replace(tmp, STATE_FILE)


def write_trades_csv(state):
    fields = ["entry_time", "exit_time", "side", "qty", "entry_price", "exit_price",
              "fees", "pnl", "pnl_pct", "reason", "bars_held"]
    with open(TRADES_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for t in state["trades"]:
            writer.writerow({k: t.get(k, "") for k in fields})


# ------------------------------------------------------------------------------
# The trading engine
#
# Fill timing matters more than anything else here. A decision is made on a
# candle's CLOSE and filled at the NEXT candle's OPEN — never on the candle
# that produced the signal. That is exactly what btc_backtest.py does, and the
# moment the two differ the backtest stops describing this bot.
# ------------------------------------------------------------------------------
class PaperEngine:
    def __init__(self, state, cfg):
        self.s = state
        self.c = cfg
        self.fee = cfg["fee_bps"] / 10_000.0
        self.slip = cfg["slippage_bps"] / 10_000.0

    def equity(self, price):
        return self.s["cash"] + self.s["qty"] * price

    def log_signal(self, ts, kind, text):
        self.s["signals"].append({"ts": int(ts), "time": iso(ts), "kind": kind, "text": text})
        self.s["signals"] = self.s["signals"][-60:]
        logger.info("[%s] %-6s %s", iso(ts), kind, text)

    def trade_to(self, ts, price, target_frac, note):
        """Move the position to `target_frac` of equity, booking costs."""
        eq = self.equity(price)
        if eq <= 0:
            return
        qty = self.s["qty"]
        target_qty = eq * target_frac / price
        delta = target_qty - qty
        if delta == 0:
            return
        fill = price * (1 + self.slip) if delta > 0 else price * (1 - self.slip)
        if delta > 0:
            cost = delta * fill * (1 + self.fee)
            if cost > self.s["cash"]:                       # never borrow
                delta = self.s["cash"] / (fill * (1 + self.fee))
                cost = delta * fill * (1 + self.fee)
            if delta <= 0:
                return
            self.s["cash"] -= cost
            if qty <= 0:
                self.s["entry_price"] = fill
                self.s["entry_ts"] = int(ts)
                self.s["entry_time"] = iso(ts)
                self.s["fees_open"] = delta * fill * self.fee
            else:
                self.s["entry_price"] = (self.s["entry_price"] * qty + fill * delta) / (qty + delta)
                self.s["fees_open"] += delta * fill * self.fee
            self.s["qty"] = qty + delta
            self.log_signal(ts, "BUY", "%s → %.0f%% invested (%.6f BTC @ $%s)"
                            % (note, target_frac * 100, self.s["qty"], f"{fill:,.2f}"))
        else:
            cut = -delta
            gross = cut * fill
            fees = gross * self.fee
            self.s["cash"] += gross - fees
            self.s["qty"] = qty - cut
            entry = self.s["entry_price"]
            pnl = cut * (fill - entry) - fees - self.s["fees_open"] * (cut / qty if qty else 0)
            if self.s["qty"] <= 1e-12:                      # fully out — book the trade
                self.s["qty"] = 0.0
                trade = {
                    "entry_time": self.s.get("entry_time", iso(ts)),
                    "exit_time": iso(ts),
                    "side": "long",
                    "qty": round(cut, 8),
                    "entry_price": round(entry, 2),
                    "exit_price": round(fill, 2),
                    "fees": round(fees + self.s.get("fees_open", 0.0), 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (cut * entry) * 100.0, 3) if entry else 0.0,
                    "reason": note,
                    "bars_held": int(ts - self.s.get("entry_ts", ts)) // self.c["granularity"],
                }
                self.s["trades"].append(trade)
                self.s["trades"] = self.s["trades"][-self.c["keep_trades"]:]
                self.s["fees_open"] = 0.0
                self.log_signal(ts, "SELL", "%s → flat | P&L $%s (%.2f%%)"
                                % (note, f"{pnl:,.2f}", trade["pnl_pct"]))
            else:
                self.log_signal(ts, "TRIM", "%s → %.0f%% invested (%.6f BTC left)"
                                % (note, target_frac * 100, self.s["qty"]))

    def process(self, bars):
        want, fast, slow, vol = target_weights(bars, self.c)
        self.s["want"] = [round(w, 4) for w in want[-self.c["keep_bars"]:]]
        processed = 0
        for i in range(1, len(bars)):
            ts, o, _h, _l, close, _v = bars[i]
            if ts <= self.s["last_bar_ts"]:
                continue
            if self.s["first_price"] is None:
                self.s["first_price"] = o
            target = want[i - 1]                    # yesterday's decision, today's open
            eq = self.equity(o)
            # circuit breaker, applied before sizing so it scales the whole book
            if self.c["dd_limit"] > 0 and self.s.get("peak_equity", 0) > 0:
                dd = 1.0 - eq / self.s["peak_equity"]
                if dd >= self.c["dd_limit"] and not self.s.get("throttled"):
                    self.s["throttled"] = True
                    self.log_signal(ts, "BRAKE", "account %.1f%% below its peak — book scaled to %.0f%%"
                                    % (dd * 100, self.c["dd_throttle"] * 100))
                elif self.s.get("throttled") and dd <= self.c["dd_recover"]:
                    self.s["throttled"] = False
                    self.log_signal(ts, "CLEAR", "drawdown healed to %.1f%% — full size restored" % (dd * 100))
                if self.s.get("throttled"):
                    target *= self.c["dd_throttle"]
            held = (self.s["qty"] * o / eq) if eq > 0 else 0.0
            drift = abs(target - held)
            crossing = (target == 0.0) != (held <= 1e-9)
            if crossing or drift > self.c["rebalance_band"]:
                if target <= 0:
                    note = "trend down"
                elif held <= 1e-9:
                    note = "trend up, vol %.0f%%" % (vol[i - 1] or 0)
                else:
                    note = "vol %.0f%%" % (vol[i - 1] or 0)
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
            "sma_fast": [None if v is None else round(v, 2) for v in fast[-keep:]],
            "sma_slow": [None if v is None else round(v, 2) for v in slow[-keep:]],
            "vol": [None if v is None else round(v, 2) for v in vol[-keep:]],
        }
        self.s["target_weight"] = round(want[-1], 4)
        self.s["realised_vol"] = None if vol[-1] is None else round(vol[-1], 2)
        return processed

    def update_stats(self, price):
        trades = self.s["trades"]
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        equity = self.equity(price)
        start = self.c["start_cash"]
        curve = [e[1] for e in self.s["equity_curve"]] or [equity]
        peak, max_dd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100.0)
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        first = self.s["first_price"] or price
        invested = (self.s["qty"] * price / equity * 100.0) if equity > 0 else 0.0
        self.s["stats"] = {
            "equity": round(equity, 2),
            "cash": round(self.s["cash"], 2),
            "position_value": round(self.s["qty"] * price, 2),
            "invested_pct": round(invested, 1),
            "total_return_pct": round((equity / start - 1) * 100.0, 3),
            "buy_hold_return_pct": round((price / first - 1) * 100.0, 3),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_dd, 2),
            "total_fees": round(sum(t["fees"] for t in trades), 2),
            "last_price": round(price, 2),
        }


# ------------------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------------------
def render_dashboard(state):
    payload = {
        "bars": state.get("bars", []),
        "indicators": state.get("indicators", {}),
        "equity": state.get("equity_curve", []),
        "trades": state.get("trades", [])[-40:][::-1],
        "signals": state.get("signals", [])[-25:][::-1],
        "stats": state.get("stats", {}),
        "position": ({"qty": state.get("qty", 0.0),
                      "entry_price": state.get("entry_price", 0.0),
                      "entry_time": state.get("entry_time"),
                      "target_weight": state.get("target_weight", 0.0),
                      "realised_vol": state.get("realised_vol")}
                     if state.get("qty", 0.0) > 0 else None),
        "want": state.get("want", []),
        "throttled": state.get("throttled", False),
        "peak_equity": state.get("peak_equity"),
        "config": state.get("config", {}),
        "last_run": state.get("last_run", {}),
        "updated": state.get("updated_utc"),
        "actions_url": ACTIONS_URL,
    }
    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DASHBOARD, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info("Dashboard written -> %s", DASHBOARD)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC Auto-Trade Demo</title>
<style>
  :root{
    --bg:#0b0e14; --panel:#131722; --panel2:#1a1f2e; --line:#262b3a;
    --text:#d6dbe6; --muted:#7d879c; --up:#26a69a; --down:#ef5350;
    --accent:#4c9ffe; --warn:#f0b90b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang TC","Microsoft JhengHei",sans-serif;}
  header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;
         flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between}
  h1{font-size:17px;margin:0;letter-spacing:.3px}
  .badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;
         font-weight:700;letter-spacing:.6px;background:rgba(240,185,11,.14);
         color:var(--warn);border:1px solid rgba(240,185,11,.35)}
  .price{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
  .muted{color:var(--muted);font-size:12px}
  main{padding:18px 22px 60px;max-width:1280px;margin:0 auto}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px}
  .card .value{font-size:21px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}
  .pos{color:var(--up)} .neg{color:var(--down)}
  section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
          padding:16px;margin-bottom:16px}
  section h2{font-size:13px;margin:0 0 12px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px}
  table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
  th{text-align:right;color:var(--muted);font-weight:600;padding:7px 8px;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  td{padding:7px 8px;border-bottom:1px solid rgba(38,43,58,.55)}
  tbody tr:hover{background:var(--panel2)}
  .pill{font-size:10.5px;padding:2px 7px;border-radius:5px;background:var(--panel2);color:var(--muted)}
  .btn{display:inline-block;margin-top:12px;padding:9px 16px;border-radius:7px;
       background:var(--panel2);border:1px solid #2c3550;color:#4c9ffe;
       text-decoration:none;font-size:13px;font-weight:600}
  .btn:hover{border-color:#4c9ffe}
  .note{color:var(--muted);font-size:11.5px;line-height:1.7;margin-top:10px}
  .linky{color:#4c9ffe;text-decoration:none;font-size:11.5px}
  .linky:hover{text-decoration:underline}
  .live{margin-left:6px;font-variant-numeric:tabular-nums}
  .sig{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid rgba(38,43,58,.55);font-size:12.5px}
  .sig time{color:var(--muted);min-width:145px}
  .tag{font-weight:700;min-width:46px}
  .BUY{color:var(--up)} .SELL{color:var(--down)} .TRIM{color:var(--accent)} .SKIP{color:var(--muted)}
  .BRAKE{color:var(--warn)} .CLEAR{color:var(--accent)}
  svg{width:100%;display:block}
  .empty{color:var(--muted);font-size:13px;padding:14px 2px}
  footer{color:var(--muted);font-size:11.5px;text-align:center;padding:0 22px 40px;line-height:1.7}
  code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:11.5px}
</style>
</head>
<body>
<header>
  <div>
    <h1>₿ BTC-USD Auto-Trade <span class="badge">PAPER / DEMO</span></h1>
    <div class="muted" id="sub">loading…</div>
  </div>
  <div style="text-align:right">
    <div class="price" id="price">–</div>
    <div class="muted" id="src">–</div>
    <div class="muted" style="margin-top:4px">
      <a href="#" id="refresh" class="linky">↻ live price</a>
      <span id="livebox"></span>
    </div>
  </div>
</header>
<main>
  <div class="grid" id="stats"></div>
  <section>
    <h2>Price &amp; signals</h2>
    <div id="chart"></div>
  </section>
  <section>
    <h2>Paper equity curve</h2>
    <div id="equity"></div>
  </section>
  <section>
    <h2>Open position</h2>
    <div id="position"></div>
  </section>
  <section>
    <h2>Recent signals</h2>
    <div id="signals"></div>
  </section>
  <section>
    <h2>Closed trades</h2>
    <div id="trades"></div>
  </section>
  <section>
    <h2>Telegram alerts</h2>
    <div id="alerts"></div>
  </section>
</main>
<footer>
  Long or flat only, sized by volatility. Decisions are made on a candle's close and filled at the
  next candle's open.<br>
  Simulated fills only — no brokerage account is connected and no real order is ever placed.<br>
  Not investment advice. Regenerated by GitHub Actions from <code>btc_autotrade.py</code>.<br>
  <a href="study.html" style="color:#4c9ffe;text-decoration:none">📄 成份研究報告(目標 · 方法 · 發現 · 分析 · 結論)→</a>
  &nbsp;·&nbsp;
  <a href="backtest.html" style="color:#4c9ffe;text-decoration:none">📊 回測數據 →</a>
  &nbsp;·&nbsp;
  <a href="lab/index.html" style="color:#4c9ffe;text-decoration:none">🧪 Forward Lab 前向紙盤(兩個未驗證候選,獨立帳戶)→</a>
</footer>
<script>
const D = __PAYLOAD__;
const NS = "http://www.w3.org/2000/svg";
const money = n => (n===null||n===undefined||isNaN(n)) ? "–" :
  "$" + Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = n => (n===null||n===undefined||isNaN(n)) ? "–" : (n>=0?"+":"") + Number(n).toFixed(2) + "%";
const cls = n => n>0 ? "pos" : (n<0 ? "neg" : "");
const el = (tag, attrs) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

/* ---------- header ---------- */
const st = D.stats || {};
document.getElementById("price").textContent = money(st.last_price);
document.getElementById("price").className = "price " + cls(st.buy_hold_return_pct);
document.getElementById("src").textContent = "source: " + ((D.last_run||{}).source || "–");
const C = D.config || {};
const candleLabel = g => g >= 86400 ? (g/86400)+"d" : (g >= 3600 ? (g/3600)+"h" : (g/60)+"m");
document.getElementById("sub").textContent =
  "updated " + (D.updated || "–") + " · " + candleLabel(C.granularity) + " candles · SMA "
  + C.sma_fast + "/" + C.sma_slow + " · vol target " + C.target_vol + "%"
  + " · long or flat, never short";

/* ---------- stat cards ---------- */
const cards = [
  ["Paper equity", money(st.equity), cls(st.total_return_pct)],
  ["Strategy return", pct(st.total_return_pct), cls(st.total_return_pct)],
  ["Buy &amp; hold", pct(st.buy_hold_return_pct), cls(st.buy_hold_return_pct)],
  ["Invested", (st.invested_pct||0).toFixed(0) + "%", ""],
  ["Trades", (st.trades||0) + " (" + (st.win_rate_pct||0) + "% win)", ""],
  ["Profit factor", st.profit_factor===null||st.profit_factor===undefined ? "–" : st.profit_factor, ""],
  ["Max drawdown", pct(-(st.max_drawdown_pct||0)), st.max_drawdown_pct ? "neg" : ""],
  ["Fees paid", money(st.total_fees), ""],
];
document.getElementById("stats").innerHTML = cards.map(c =>
  `<div class="card"><div class="label">${c[0]}</div><div class="value ${c[2]}">${c[1]}</div></div>`).join("");

/* ---------- candlestick chart ---------- */
function drawChart(){
  const bars = (D.bars||[]).slice(-160);
  const host = document.getElementById("chart");
  if (!bars.length){ host.innerHTML = '<div class="empty">No candles yet.</div>'; return; }
  const W = 1200, H = 420, PL = 8, PR = 74, PT = 12, PB = 26;
  const ind = D.indicators || {};
  const offset = (D.bars||[]).length - bars.length;
  const ef = (ind.sma_fast||[]).slice(offset), es = (ind.sma_slow||[]).slice(offset);
  const lo = Math.min(...bars.map(b=>b[3])), hi = Math.max(...bars.map(b=>b[2]));
  const pad = (hi-lo)*0.06 || 1;
  const y = v => PT + (hi+pad - v) / ((hi+pad)-(lo-pad)) * (H-PT-PB);
  const cw = (W-PL-PR) / bars.length;
  const x = i => PL + i*cw + cw/2;
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, preserveAspectRatio:"none", height:H});

  for (let g=0; g<=4; g++){
    const v = lo-pad + ((hi+pad)-(lo-pad))*g/4;
    svg.appendChild(el("line",{x1:PL,x2:W-PR,y1:y(v),y2:y(v),stroke:"#262b3a","stroke-width":1}));
    const t = el("text",{x:W-PR+7,y:y(v)+4,fill:"#7d879c","font-size":11});
    t.textContent = Math.round(v).toLocaleString("en-US");
    svg.appendChild(t);
  }
  bars.forEach((b,i)=>{
    const up = b[4] >= b[1], col = up ? "#26a69a" : "#ef5350";
    svg.appendChild(el("line",{x1:x(i),x2:x(i),y1:y(b[2]),y2:y(b[3]),stroke:col,"stroke-width":1}));
    const top = y(Math.max(b[1],b[4])), h = Math.max(1, Math.abs(y(b[1])-y(b[4])));
    svg.appendChild(el("rect",{x:x(i)-cw*0.32,y:top,width:Math.max(1,cw*0.64),height:h,fill:col}));
  });
  const line = (vals,col)=>{
    const pts = vals.map((v,i)=> v===null?null:`${x(i)},${y(v)}`).filter(Boolean).join(" ");
    if (pts) svg.appendChild(el("polyline",{points:pts,fill:"none",stroke:col,"stroke-width":1.4,opacity:.9}));
  };
  line(ef, "#4c9ffe"); line(es, "#f0b90b");

  // trade markers on the candles
  const tsIndex = new Map(bars.map((b,i)=>[b[0], i]));
  const nearest = ts => { let best=null, bd=Infinity;
    for (const [t,i] of tsIndex){ const d=Math.abs(t-ts); if (d<bd){bd=d;best=i;} }
    return bd <= (D.config.granularity||900)*2 ? best : null; };
  (D.signals||[]).forEach(s=>{
    if (s.kind!=="BUY" && s.kind!=="SELL") return;   // TRIM/SKIP are not entries
    const i = nearest(s.ts); if (i===null) return;
    const b = bars[i], buy = s.kind==="BUY";
    const py = buy ? y(b[3]) + 16 : y(b[2]) - 16;
    const m = el("path",{fill: buy ? "#26a69a" : "#ef5350",
      d: buy ? `M${x(i)},${py-9} l6,10 l-12,0 z` : `M${x(i)},${py+9} l6,-10 l-12,0 z`});
    svg.appendChild(m);
  });
  const lastTxt = el("text",{x:W-PR+7,y:y(bars[bars.length-1][4])+4,fill:"#d6dbe6","font-size":11,"font-weight":700});
  lastTxt.textContent = Math.round(bars[bars.length-1][4]).toLocaleString("en-US");
  svg.appendChild(lastTxt);
  host.innerHTML = ""; host.appendChild(svg);
}

/* ---------- equity curve ---------- */
function drawEquity(){
  const pts = (D.equity||[]).slice(-600);
  const host = document.getElementById("equity");
  if (pts.length < 2){ host.innerHTML = '<div class="empty">Equity curve appears after the first processed candles.</div>'; return; }
  const W=1200,H=200,PL=8,PR=74,PT=12,PB=20;
  const vals = pts.map(p=>p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals), pad=(hi-lo)*0.08||1;
  const y = v => PT + (hi+pad-v)/((hi+pad)-(lo-pad))*(H-PT-PB);
  const x = i => PL + i*(W-PL-PR)/(pts.length-1);
  const svg = el("svg",{viewBox:`0 0 ${W} ${H}`, preserveAspectRatio:"none", height:H});
  const base = (D.config||{}).start_cash;
  if (base >= lo-pad && base <= hi+pad){
    svg.appendChild(el("line",{x1:PL,x2:W-PR,y1:y(base),y2:y(base),
      stroke:"#7d879c","stroke-width":1,"stroke-dasharray":"4 4",opacity:.6}));
  }
  const d = vals.map((v,i)=>`${x(i)},${y(v)}`).join(" ");
  const up = vals[vals.length-1] >= (base||vals[0]);
  const col = up ? "#26a69a" : "#ef5350";
  svg.appendChild(el("polygon",{points:`${PL},${H-PB} ${d} ${x(pts.length-1)},${H-PB}`,
    fill:col,opacity:.12}));
  svg.appendChild(el("polyline",{points:d,fill:"none",stroke:col,"stroke-width":1.8}));
  [hi,lo].forEach(v=>{ const t=el("text",{x:W-PR+7,y:y(v)+4,fill:"#7d879c","font-size":11});
    t.textContent = "$"+Math.round(v).toLocaleString("en-US"); svg.appendChild(t); });
  host.innerHTML=""; host.appendChild(svg);
}

/* ---------- position / signals / trades ---------- */
function drawPosition(){
  const p = D.position, host = document.getElementById("position"), st = D.stats || {};
  const tw = ((p ? p.target_weight : 0) * 100).toFixed(0);
  const rv = p && p.realised_vol != null ? p.realised_vol.toFixed(0) + "%" : "–";
  if (!p){
    host.innerHTML = `<div class="empty">Flat — the ${C.sma_fast}-bar average is below the
      ${C.sma_slow}-bar one, so the strategy is sitting in cash.</div>`;
    return;
  }
  const last = st.last_price || p.entry_price;
  const upnl = (last - p.entry_price) * p.qty;
  host.innerHTML = `<table><tbody>
    <tr><td>Side</td><td style="text-align:right">LONG ${p.qty.toFixed(6)} BTC</td></tr>
    <tr><td>Average entry</td><td style="text-align:right">${money(p.entry_price)} <span class="pill">${p.entry_time || "–"}</span></td></tr>
    <tr><td>Invested</td><td style="text-align:right">${(st.invested_pct||0).toFixed(0)}% of the account</td></tr>
    <tr><td>Target weight</td><td style="text-align:right">${tw}% <span class="pill">realised vol ${rv} vs ${C.target_vol}% budget</span></td></tr>
    <tr><td>Unrealised P&amp;L</td><td style="text-align:right" class="${cls(upnl)}">${money(upnl)}</td></tr>
  </tbody></table>`;
}

function drawSignals(){
  const host = document.getElementById("signals");
  const rows = D.signals || [];
  if (!rows.length){ host.innerHTML = '<div class="empty">No signals recorded yet.</div>'; return; }
  host.innerHTML = rows.map(s =>
    `<div class="sig"><time>${s.time}</time><span class="tag ${s.kind}">${s.kind}</span><span>${s.text}</span></div>`).join("");
}

function drawTrades(){
  const host = document.getElementById("trades");
  const rows = D.trades || [];
  if (!rows.length){ host.innerHTML = '<div class="empty">No closed trades yet.</div>'; return; }
  host.innerHTML = `<table><thead><tr>
      <th>Entry</th><th>Exit</th><th>Qty</th><th>In</th><th>Out</th><th>Fees</th><th>P&amp;L</th><th>%</th><th>Reason</th>
    </tr></thead><tbody>` + rows.map(t=>`<tr>
      <td>${t.entry_time}</td><td>${t.exit_time}</td><td style="text-align:right">${t.qty.toFixed(6)}</td>
      <td style="text-align:right">${money(t.entry_price)}</td><td style="text-align:right">${money(t.exit_price)}</td>
      <td style="text-align:right">${money(t.fees)}</td>
      <td style="text-align:right" class="${cls(t.pnl)}">${money(t.pnl)}</td>
      <td style="text-align:right" class="${cls(t.pnl)}">${pct(t.pnl_pct)}</td>
      <td style="text-align:right"><span class="pill">${t.reason}</span></td></tr>`).join("") + `</tbody></table>`;
}

drawChart(); drawEquity(); drawPosition(); drawSignals(); drawTrades();
/* ---------- telegram ---------- */
(function(){
  const host = document.getElementById("alerts");
  if (!host) return;
  const url = D.actions_url || "#";
  host.innerHTML =
    '<div class="note">A message is sent when the paper position opens or closes, or when the '
    + 'drawdown brake switches on or off \u2014 roughly ten times a year. Routine volatility '
    + 'resizing is deliberately silent.</div>'
    + '<button class="btn" id="testalert" type="button">\uD83D\uDD14 Send a test alert</button>'
    + '<span id="testresult" class="live"></span>'
    + '<div class="note" id="testnote">Sends straight from this page. The bot token stays in the '
    + 'host\u2019s environment and never reaches your browser.</div>';

  const btn = document.getElementById("testalert");
  const out = document.getElementById("testresult");
  const note = document.getElementById("testnote");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.className = "live";
    out.textContent = " sending\u2026";
    try{
      const r = await fetch("/api/test-alert", {method:"POST"});
      const b = await r.json().catch(() => ({}));
      if (r.ok && b.ok){
        out.className = "live pos";
        out.textContent = " \u2713 sent \u2014 check Telegram";
      } else if (r.status === 404 || r.status === 405){
        // The serverless function is not deployed on this host. Say so, and
        // fall back to the route that always works.
        out.className = "live";
        out.textContent = "";
        note.innerHTML = 'No <code>/api/test-alert</code> on this host, so the page cannot send '
          + 'directly. Run it from GitHub instead: <a class="linky" href="' + url + '" '
          + 'target="_blank" rel="noopener">Actions \u2192 Run workflow \u2192 tick test_alert</a>.';
      } else {
        out.className = "live neg";
        out.textContent = " \u2717 " + (b.error || ("HTTP " + r.status));
      }
    }catch(err){
      out.className = "live neg";
      out.textContent = " \u2717 request failed";
    }
    setTimeout(() => { btn.disabled = false; }, 3000);
  });
})();
/* ---------- live price ----------
   The page is a snapshot of the last run. This fetches the spot price now,
   straight from a public exchange in the reader's own browser — no key, no
   backend. It deliberately does NOT touch the equity or position numbers:
   those belong to the run that produced them, and quietly repricing them
   here would invent a paper account that never existed. */
(function(){
  const link = document.getElementById("refresh");
  const box = document.getElementById("livebox");
  if (!link || !box) return;
  const sources = [
    ["coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot",
     j => parseFloat(j.data.amount)],
    ["binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
     j => parseFloat(j.price)],
    ["kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
     j => parseFloat(Object.values(j.result)[0].c[0])]
  ];
  async function go(e){
    if (e) e.preventDefault();
    box.className = "live";
    box.textContent = " …";
    for (const [name, url, pick] of sources){
      try{
        const r = await fetch(url, {cache:"no-store"});
        if (!r.ok) continue;
        const v = pick(await r.json());
        if (!isFinite(v)) continue;
        const ref = (D.stats||{}).last_price;
        const drift = (ref && isFinite(ref)) ? ((v/ref - 1) * 100) : null;
        box.className = "live " + (drift === null ? "" : cls(drift));
        box.textContent = " " + money(v) + " now"
          + (drift === null ? "" : " (" + pct(drift) + " vs last run)")
          + " · " + name;
        return;
      }catch(err){ /* try the next exchange */ }
    }
    box.className = "live neg";
    box.textContent = " could not reach any exchange";
  }
  link.addEventListener("click", go);
})();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------------------
# Markdown summary (piped into $GITHUB_STEP_SUMMARY by the workflow)
# ------------------------------------------------------------------------------
def markdown_summary(state):
    lines = ["### \u20bf BTC Auto-Trade \u2014 paper/demo", ""]
    run = state.get("last_run", {})
    st = state.get("stats", {})
    if not run:
        lines.append("_No run recorded yet._")
    elif not run.get("ok"):
        lines.append("\u26a0\ufe0f No market data this run: `%s`" % run.get("error", "unknown"))
    else:
        qty = state.get("qty", 0.0)
        pos = {"qty": qty, "entry_price": state.get("entry_price", 0.0)} if qty > 0 else None
        pf = st.get("profit_factor")
        rows = [
            ("BTC price", "$%s" % f"{st.get('last_price', 0):,.2f}"),
            ("Paper equity", "$%s" % f"{st.get('equity', 0):,.2f}"),
            ("Strategy return", "%+.2f%%" % st.get("total_return_pct", 0.0)),
            ("Buy & hold", "%+.2f%%" % st.get("buy_hold_return_pct", 0.0)),
            ("Trades (win rate)", "%d (%.0f%%)" % (st.get("trades", 0), st.get("win_rate_pct", 0.0))),
            ("Profit factor", "-" if pf is None else str(pf)),
            ("Max drawdown", "-%.2f%%" % st.get("max_drawdown_pct", 0.0)),
            ("Position", ("LONG %.6f BTC @ $%s (%.0f%% invested)"
                          % (pos["qty"], f"{pos['entry_price']:,.2f}",
                             state.get("stats", {}).get("invested_pct", 0)))
             if pos else "flat (in cash)"),
            ("Target weight", "%.0f%% (realised vol %s%%)"
             % (state.get("target_weight", 0.0) * 100,
                state.get("realised_vol", "?"))),
            ("Data source", run.get("source", "-")),
            ("New candles booked", str(run.get("bars_processed", 0))),
            ("Last candle", run.get("last_bar", "-")),
        ]
        lines += ["| metric | value |", "|---|---|"]
        lines += ["| %s | %s |" % r for r in rows]
    lines += ["", "> Simulated fills only. No broker is connected and no real order is ever placed."]
    return "\n".join(lines)


# ------------------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------------------
def dry_run(cfg):
    """
    Connectivity + sanity check: read the live market, change nothing on disk.

    An unreachable exchange exits 0 with a warning — that is weather, not a
    defect. Anything else is allowed to raise, so a broken strategy reference
    turns the build red instead of being written off as an outage. That
    distinction exists because it once was not made: a stale ema_series call
    survived a strategy change and the step failed silently under
    continue-on-error.
    """
    try:
        source, bars = fetch_bars(cfg["granularity"], cfg["history_bars"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("No exchange reachable, skipping the dry run: %s", exc)
        return 0

    closes = [b[4] for b in bars]
    want, fast, slow, vol = target_weights(bars, cfg)
    i = len(bars) - 1
    if fast[i] is None or slow[i] is None or vol[i] is None:
        logger.error("Dry run: only %d bars, not enough for a %d-bar average — "
                     "the feed cannot cover this strategy", len(bars), cfg["sma_slow"])
        return 1          # a feed that cannot cover the strategy IS a defect
    trend = "up" if fast[i] > slow[i] else "down"
    logger.info("Source         : %s", source)
    logger.info("Closed bars    : %d  (%s -> %s)", len(bars), iso(bars[0][0]), iso(bars[-1][0]))
    logger.info("Last close     : $%s", f"{closes[-1]:,.2f}")
    logger.info("SMA %-4s/ %-5s: %s / %s  (trend %s)",
                "%d" % cfg["sma_fast"], "%d" % cfg["sma_slow"],
                f"{fast[i]:,.2f}", f"{slow[i]:,.2f}", trend)
    logger.info("Realised vol   : %.1f%%  against a %.0f%% budget", vol[i], cfg["target_vol"])
    logger.info("Target weight  : %.0f%% of the account%s",
                want[i] * 100, "" if want[i] > 0 else "  (flat — trend is down)")
    logger.info("Dry run OK — nothing was written, no order was placed (there is no broker).")
    return 0


def run_once(cfg, state):
    started = time.time()
    try:
        source, bars = fetch_bars(cfg["granularity"], cfg["history_bars"])
    except Exception as exc:  # noqa: BLE001
        logger.error("No market data this run: %s", exc)
        state.setdefault("last_run", {})
        state["last_run"].update({"time": iso(started), "ok": False, "error": str(exc)[:300]})
        state["updated_utc"] = iso(started)
        save_state(state)
        render_dashboard(state)
        return False

    engine = PaperEngine(state, cfg)
    before = len(state.get("signals", []))
    processed = engine.process(bars)
    last_price = bars[-1][4]
    engine.update_stats(last_price)
    new_signals = state.get("signals", [])[before:]

    state["updated_utc"] = iso(time.time())
    state["last_run"] = {
        "time": iso(time.time()),
        "ok": True,
        "source": source,
        "bars_processed": processed,
        "last_bar": iso(state["last_bar_ts"]),
        "seconds": round(time.time() - started, 2),
    }
    save_state(state)
    write_trades_csv(state)
    render_dashboard(state)
    notify_new_signals(state, new_signals, last_price)

    st = state["stats"]
    logger.info("Processed %d new bar(s) | price $%.2f | equity $%.2f (%+.2f%%) | B&H %+.2f%% | %d trades, %s%% win",
                processed, last_price, st["equity"], st["total_return_pct"],
                st["buy_hold_return_pct"], st["trades"], st["win_rate_pct"])
    if state["qty"] > 0:
        logger.info("Open: LONG %.6f BTC @ $%.2f — %.0f%% invested (target %.0f%%, realised vol %s%%)",
                    state["qty"], state["entry_price"], st["invested_pct"],
                    state["target_weight"] * 100, state["realised_vol"])
    else:
        logger.info("Open: flat (in cash) — target weight %.0f%%", state["target_weight"] * 100)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="BTC live-data paper trading demo (no broker connected)")
    ap.add_argument("--loop", action="store_true", help="keep running locally instead of a single pass")
    ap.add_argument("--interval", type=int, default=60, help="seconds between passes in --loop mode")
    ap.add_argument("--reset", action="store_true", help="wipe the paper account and start fresh")
    ap.add_argument("--backfill", type=int, default=None,
                    help="bars of history to pull (default %d)" % CONFIG["history_bars"])
    ap.add_argument("--summary", action="store_true",
                    help="print a markdown summary of the saved state and exit (no trading)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch live candles and report what the strategy sees, "
                         "without touching the paper account or writing any file")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one Telegram test message and exit, so the wiring "
                         "can be proved without waiting for a real signal")
    args = ap.parse_args(argv)

    cfg = dict(CONFIG)
    if args.backfill:
        cfg["history_bars"] = args.backfill

    if args.test_alert:
        # A notifier nobody can test is a notifier nobody can trust. This
        # strategy trades about ten times a year, so without this you would
        # find out whether the wiring works some weeks from now, by not
        # getting a message you were never sure was coming.
        if not telegram_configured():
            logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not both set.")
            return 1
        ok = send_telegram(
            "\u2705 TEST — PAPER TRADE alerts are wired up correctly.\n"
            "\n"
            "This is not a signal. Real alerts fire only when the paper "
            "position opens, closes, or the drawdown brake switches on or off "
            "— roughly ten times a year.")
        logger.info("Test alert %s.", "delivered" if ok else "FAILED")
        return 0 if ok else 1

    if args.dry_run:
        return dry_run(cfg)

    if args.summary:
        if not os.path.exists(STATE_FILE):
            print("_No paper-account state yet \u2014 this was a tests-only run._")
            return 0
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            print(markdown_summary(json.load(fh)))
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    state = load_state(cfg, reset=args.reset)

    logger.info("=" * 78)
    logger.info("  BTC AUTO-TRADE DEMO — PAPER ACCOUNT, NO BROKER, NO REAL ORDERS")
    logger.info("  %s candles | SMA %d/%d | vol target %.0f%% | long or flat, never short",
                candle_label(cfg["granularity"]), cfg["sma_fast"], cfg["sma_slow"],
                cfg["target_vol"])
    logger.info("=" * 78)

    if not args.loop:
        ok = run_once(cfg, state)
        return 0 if ok else 0  # never fail the CI job on a transient API hiccup

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
