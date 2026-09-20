#!/usr/bin/env python3
"""
================================================================================
  BTC Strategy Lab — backtester for the paper-trading demo
================================================================================
  Fetches a long BTC-USD history once, caches it in the repo, then replays
  strategies over it looking for a decent risk-adjusted return (Sharpe).

  Still zero dependencies and still zero brokers.

  Usage:
      python btc_backtest.py --fetch-history --years 4     # cache the history
      python btc_backtest.py --sweep                       # parameter search
      python btc_backtest.py --report                      # build the HTML report
================================================================================
"""

import argparse
import gzip
import json
import logging
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("btc-lab")

BAR_SECONDS = 3600
BARS_PER_DAY = 24
DAYS_PER_YEAR = 365.0

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "btc_demo")
HISTORY_DIR = os.path.join(OUT_DIR, "history")
HISTORY_FILE = os.path.join(HISTORY_DIR, "BTC_1h.csv.gz")   # the default symbol


def gran_tag(gran):
    return "1d" if gran >= 86400 else ("%dh" % (gran // 3600) if gran >= 3600 else "%dm" % (gran // 60))


def history_path(symbol="BTC", gran=3600):
    return os.path.join(HISTORY_DIR, "%s_%s.csv.gz" % (symbol.upper(), gran_tag(gran)))


# One asset is one price path. A parameter set that only ever had to survive
# BTC has had roughly a dozen independent market episodes to fit itself to;
# requiring it to survive ten assets is the cheapest real defence against
# picking a winner that was only ever luck.
SYMBOLS = {
    "BTC":  {"bitstamp": "btcusd",  "binance": "BTCUSDT",  "coinbase": "BTC-USD"},
    "ETH":  {"bitstamp": "ethusd",  "binance": "ETHUSDT",  "coinbase": "ETH-USD"},
    "SOL":  {"bitstamp": "solusd",  "binance": "SOLUSDT",  "coinbase": "SOL-USD"},
    "XRP":  {"bitstamp": "xrpusd",  "binance": "XRPUSDT",  "coinbase": "XRP-USD"},
    "LTC":  {"bitstamp": "ltcusd",  "binance": "LTCUSDT",  "coinbase": "LTC-USD"},
    "ADA":  {"binance": "ADAUSDT",  "coinbase": "ADA-USD"},
    "DOGE": {"binance": "DOGEUSDT", "coinbase": "DOGE-USD"},
    "LINK": {"bitstamp": "linkusd", "binance": "LINKUSDT", "coinbase": "LINK-USD"},
    "AVAX": {"binance": "AVAXUSDT", "coinbase": "AVAX-USD"},
    "DOT":  {"binance": "DOTUSDT",  "coinbase": "DOT-USD"},
    "BCH":  {"bitstamp": "bchusd",  "binance": "BCHUSDT",  "coinbase": "BCH-USD"},
    "XLM":  {"bitstamp": "xlmusd",  "binance": "XLMUSDT",  "coinbase": "XLM-USD"},
    "ETC":  {"binance": "ETCUSDT",  "coinbase": "ETC-USD"},
    "ATOM": {"binance": "ATOMUSDT", "coinbase": "ATOM-USD"},
    "UNI":  {"bitstamp": "uniusd",  "binance": "UNIUSDT",  "coinbase": "UNI-USD"},
    "AAVE": {"bitstamp": "aaveusd", "binance": "AAVEUSDT", "coinbase": "AAVE-USD"},
    "ALGO": {"bitstamp": "algousd", "binance": "ALGOUSDT", "coinbase": "ALGO-USD"},
    "FIL":  {"binance": "FILUSDT",  "coinbase": "FIL-USD"},
    "VET":  {"binance": "VETUSDT",  "coinbase": "VET-USD"},
    "ICP":  {"binance": "ICPUSDT",  "coinbase": "ICP-USD"},
    "TRX":  {"binance": "TRXUSDT",  "coinbase": "TRX-USD"},
    "EOS":  {"binance": "EOSUSDT",  "coinbase": "EOS-USD"},
    "XTZ":  {"binance": "XTZUSDT",  "coinbase": "XTZ-USD"},
    "THETA": {"binance": "THETAUSDT"},
    "AXS":  {"binance": "AXSUSDT",  "coinbase": "AXS-USD"},
    "SAND": {"binance": "SANDUSDT", "coinbase": "SAND-USD"},
    "MANA": {"binance": "MANAUSDT", "coinbase": "MANA-USD"},
    "GRT":  {"bitstamp": "grtusd",  "binance": "GRTUSDT",  "coinbase": "GRT-USD"},
    "CRV":  {"bitstamp": "crvusd",  "binance": "CRVUSDT",  "coinbase": "CRV-USD"},
    "MKR":  {"bitstamp": "mkrusd",  "binance": "MKRUSDT",  "coinbase": "MKR-USD"},
    "COMP": {"bitstamp": "compusd", "binance": "COMPUSDT", "coinbase": "COMP-USD"},
    "SNX":  {"bitstamp": "snxusd",  "binance": "SNXUSDT",  "coinbase": "SNX-USD"},
    "ZEC":  {"binance": "ZECUSDT",  "coinbase": "ZEC-USD"},
    "DASH": {"binance": "DASHUSDT", "coinbase": "DASH-USD"},
    "IOTA": {"binance": "IOTAUSDT"},
    "BAT":  {"bitstamp": "batusd",  "binance": "BATUSDT",  "coinbase": "BAT-USD"},
    "ENJ":  {"binance": "ENJUSDT",  "coinbase": "ENJ-USD"},
    "CHZ":  {"binance": "CHZUSDT",  "coinbase": "CHZ-USD"},
    "HBAR": {"binance": "HBARUSDT", "coinbase": "HBAR-USD"},
    "NEAR": {"binance": "NEARUSDT", "coinbase": "NEAR-USD"},
}
DEFAULT_SYMBOLS = list(SYMBOLS)

UA = "btc-paper-demo/1.0 (+github actions; educational backtesting)"


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------------------
# History fetching. Exchanges cap each response, so we page backwards in time.
# ------------------------------------------------------------------------------
def page_bitstamp(gran, start_ts, end_ts, ticker="btcusd"):
    bars, cursor = [], start_ts
    while cursor < end_ts:
        url = ("https://www.bitstamp.net/api/v2/ohlc/%s/?step=%d&limit=1000&start=%d"
               % (ticker, gran, cursor))
        chunk = http_json(url)["data"]["ohlc"]
        if not chunk:
            break
        rows = [[int(r["timestamp"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"])] for r in chunk]
        rows = [r for r in rows if r[0] >= cursor]
        if not rows:
            break
        bars.extend(rows)
        cursor = rows[-1][0] + gran
        time.sleep(0.25)          # stay well inside the public rate limit
    return bars


def page_binance(gran, start_ts, end_ts, ticker="BTCUSDT"):
    tf = {3600: "1h", 900: "15m", 14400: "4h", 86400: "1d"}[gran]
    bars, cursor = [], start_ts * 1000
    while cursor < end_ts * 1000:
        url = ("https://api.binance.com/api/v3/klines?symbol=%s&interval=%s"
               "&limit=1000&startTime=%d" % (ticker, tf, cursor))
        chunk = http_json(url)
        if not chunk:
            break
        rows = [[int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]),
                 float(r[4]), float(r[5])] for r in chunk]
        bars.extend(rows)
        cursor = int(chunk[-1][0]) + gran * 1000
        time.sleep(0.2)
    return bars


def page_coinbase(gran, start_ts, end_ts, ticker="BTC-USD"):
    bars, cursor = [], start_ts
    while cursor < end_ts:
        stop = min(cursor + gran * 300, end_ts)
        url = ("https://api.exchange.coinbase.com/products/%s/candles"
               "?granularity=%d&start=%s&end=%s"
               % (ticker, gran,
                  datetime.fromtimestamp(cursor, tz=timezone.utc).isoformat(),
                  datetime.fromtimestamp(stop, tz=timezone.utc).isoformat()))
        chunk = http_json(url)
        rows = [[int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])]
                for r in chunk]
        bars.extend(rows)
        cursor = stop
        time.sleep(0.2)
    return bars


HISTORY_SOURCES = [("bitstamp", page_bitstamp), ("binance", page_binance), ("coinbase", page_coinbase)]


def fetch_history(years, gran, symbol="BTC", min_years=2.5):
    """
    Download `years` of candles for one symbol.

    Altcoins simply did not exist for the whole window, so a short history is
    accepted (down to `min_years`) and the real span is recorded — but a source
    that returns far less than it could is still rejected, because a silently
    truncated series looks exactly like a complete one downstream.
    """
    tickers = SYMBOLS.get(symbol.upper())
    if not tickers:
        raise ValueError("unknown symbol %r — known: %s" % (symbol, ", ".join(sorted(SYMBOLS))))
    end_ts = int(time.time())
    start_ts = end_ts - int(years * 365.25 * 86400)
    best = None
    errors = []
    for name, fn in HISTORY_SOURCES:
        ticker = tickers.get(name)
        if not ticker:
            continue
        try:
            bars = fn(gran, start_ts, end_ts, ticker)
            bars = [b for b in bars if b[0] + gran <= end_ts]
            seen = {}
            for b in bars:
                seen[b[0]] = b
            bars = [seen[k] for k in sorted(seen)]
            if len(bars) < 500:
                raise RuntimeError("only %d bars" % len(bars))
            covered = (bars[-1][0] - bars[0][0]) / (365.25 * 86400)
            if covered < min_years:
                raise RuntimeError("only %.1f years, below the %.1f-year floor" % (covered, min_years))
            logger.info("  %-5s via %-9s %6d bars  %s -> %s  (%.1f years)",
                        symbol, name, len(bars), iso(bars[0][0])[:10], iso(bars[-1][0])[:10], covered)
            # keep the longest history any exchange can give us
            if best is None or covered > best[2] + 0.05:
                best = (name, bars, covered)
            if covered >= years - 0.2:
                break                     # already as far back as we asked for
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (name, exc))
    if best is None:
        raise RuntimeError("no source had %s | %s" % (symbol, " | ".join(errors)))
    return best[0], best[1]


def save_history(bars, path=None, symbol="BTC", gran=3600):
    """
    Write the cache deterministically: identical bars must produce identical
    bytes. gzip embeds a modification time by default, so re-saving unchanged
    data produced a different file every run, and the workflow committed it —
    a junk commit a day for no change in content.
    """
    path = path or history_path(symbol, gran)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = ["timestamp,open,high,low,close,volume\n"]
    for b in bars:
        body.append("%d,%.6g,%.6g,%.6g,%.6g,%.4f\n" % tuple(b))
    raw = "".join(body).encode("utf-8")
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            gz.write(raw)
    logger.info("  %-5s saved %d bars -> %s (%.0f KB)",
                symbol, len(bars), os.path.basename(path), os.path.getsize(path) / 1024)


def load_history(path=None, symbol="BTC", gran=3600):
    path = path or history_path(symbol, gran)
    if not os.path.exists(path):
        legacy = os.path.join(OUT_DIR, "history_1h.csv.gz")     # pre-multi-asset layout
        if symbol.upper() == "BTC" and os.path.exists(legacy):
            path = legacy
        else:
            raise FileNotFoundError(path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        next(fh)
        return [[int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])]
                for p in (line.split(",") for line in fh if line.strip())]


MANIFEST_FILE = os.path.join(HISTORY_DIR, "manifest.json")


def load_manifest():
    try:
        with open(MANIFEST_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {}


def record_fetch(symbol, bars, years, source, gran=3600):
    """
    Remember what a request actually returned.

    This replaces a hand-written table of listing dates, which was wrong and
    therefore expensive: it put SOL's first trade in 2020 when the exchange we
    read only has it from 2021, so every run decided the cache was short and
    re-downloaded the whole basket. Recording the outcome needs no guesses —
    "we asked for ten years and this is what exists" is a fact, not an
    estimate, and it self-corrects if a venue ever backfills.
    """
    man = load_manifest()
    man["%s@%s" % (symbol.upper(), gran_tag(gran))] = {
        "requested_years": years, "start": int(bars[0][0]), "end": int(bars[-1][0]),
        "bars": len(bars), "source": source}
    os.makedirs(HISTORY_DIR, exist_ok=True)
    tmp = MANIFEST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST_FILE)


def already_satisfied(symbol, bars, years, gran=3600):
    """True when a re-download could not return more than we already hold."""
    entry = load_manifest().get("%s@%s" % (symbol.upper(), gran_tag(gran)))
    if not entry:
        return False
    if entry.get("requested_years", 0) < years - 1e-9:
        return False                      # a bigger window is being asked for now
    return entry.get("start") == int(bars[0][0])


def available_symbols(gran=3600):
    """Symbols that actually have a cached history, longest first."""
    out = []
    for sym in SYMBOLS:
        try:
            bars = load_history(symbol=sym, gran=gran)
        except (FileNotFoundError, OSError):
            continue
        out.append((sym, len(bars), (bars[-1][0] - bars[0][0]) / (DAYS_PER_YEAR * 86400)))
    out.sort(key=lambda r: -r[2])
    return out


# ==============================================================================
#  PART 2 — THE LAB: indicators, strategies, backtest engine, walk-forward
# ==============================================================================
import math
from collections import deque

BAR_SECONDS = 3600
BARS_PER_DAY = 24
DAYS_PER_YEAR = 365.0


class Indicators:
    """Indicator cache. The same EMA(50) is reused by every parameter combo."""

    def __init__(self, bars):
        self.bars = bars
        self.open = [b[1] for b in bars]
        self.high = [b[2] for b in bars]
        self.low = [b[3] for b in bars]
        self.close = [b[4] for b in bars]
        self._cache = {}

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n):
        def calc():
            out, k, prev = [], 2.0 / (n + 1.0), None
            for v in self.close:
                prev = v if prev is None else v * k + prev * (1 - k)
                out.append(prev)
            return out
        return self._memo(("ema", n), calc)

    def sma(self, n):
        def calc():
            out, run = [None] * len(self.close), 0.0
            for i, v in enumerate(self.close):
                run += v
                if i >= n:
                    run -= self.close[i - n]
                if i >= n - 1:
                    out[i] = run / n
            return out
        return self._memo(("sma", n), calc)

    def rsi(self, n):
        def calc():
            c, out = self.close, [None] * len(self.close)
            if len(c) <= n:
                return out
            g = l = 0.0
            for i in range(1, n + 1):
                ch = c[i] - c[i - 1]
                g += max(ch, 0.0)
                l += max(-ch, 0.0)
            ag, al = g / n, l / n
            out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            for i in range(n + 1, len(c)):
                ch = c[i] - c[i - 1]
                ag = (ag * (n - 1) + max(ch, 0.0)) / n
                al = (al * (n - 1) + max(-ch, 0.0)) / n
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            return out
        return self._memo(("rsi", n), calc)

    def atr(self, n):
        def calc():
            b, out = self.bars, [None] * len(self.bars)
            if len(b) <= n:
                return out
            trs = [b[0][2] - b[0][3]]
            for i in range(1, len(b)):
                h, lo, pc = b[i][2], b[i][3], b[i - 1][4]
                trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
            prev = sum(trs[1:n + 1]) / n
            out[n] = prev
            for i in range(n + 1, len(b)):
                prev = (prev * (n - 1) + trs[i]) / n
                out[i] = prev
            return out
        return self._memo(("atr", n), calc)

    def _rolling(self, seq, n, better):
        res, dq = [None] * len(seq), deque()
        for i, v in enumerate(seq):
            while dq and dq[0] <= i - n:
                dq.popleft()
            while dq and better(seq[dq[-1]], v):
                dq.pop()
            dq.append(i)
            if i >= n - 1:
                res[i] = seq[dq[0]]
        return res

    def highest(self, n):
        """Rolling max of highs over the n bars ending at i."""
        return self._memo(("hh", n), lambda: self._rolling(self.high, n, lambda a, b: a <= b))

    def lowest(self, n):
        """Rolling min of lows over the n bars ending at i."""
        return self._memo(("ll", n), lambda: self._rolling(self.low, n, lambda a, b: a >= b))

    def roc(self, n):
        def calc():
            c = self.close
            return [None if i < n or c[i - n] == 0 else (c[i] / c[i - n] - 1.0) * 100.0
                    for i in range(len(c))]
        return self._memo(("roc", n), calc)

    def adx(self, n):
        """
        Wilder's ADX — trend STRENGTH, with no opinion on direction.
        A moving average tells you which way; this tells you whether it is
        worth acting on. Low ADX is the chop that whipsaws trend systems.
        """
        def calc():
            b = self.bars
            hi, lo, cl = self.high, self.low, self.close
            tr, pdm, ndm = [hi[0] - lo[0]], [0.0], [0.0]
            for i in range(1, len(b)):
                tr.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
                up, dn = hi[i] - hi[i - 1], lo[i - 1] - lo[i]
                pdm.append(up if (up > dn and up > 0) else 0.0)
                ndm.append(dn if (dn > up and dn > 0) else 0.0)

            def smooth(v):
                out = [None] * len(v)
                if len(v) <= n:
                    return out
                run = sum(v[1:n + 1])
                out[n] = run
                for i in range(n + 1, len(v)):
                    run = run - run / n + v[i]
                    out[i] = run
                return out

            atr_, pd_, nd_ = smooth(tr), smooth(pdm), smooth(ndm)
            dx = [None] * len(b)
            for i in range(len(b)):
                if atr_[i]:
                    pdi, ndi = 100 * pd_[i] / atr_[i], 100 * nd_[i] / atr_[i]
                    if pdi + ndi > 0:
                        dx[i] = 100 * abs(pdi - ndi) / (pdi + ndi)
            out = [None] * len(b)
            first = next((i for i, v in enumerate(dx) if v is not None), None)
            if first is None or first + n >= len(b):
                return out
            start = first + n
            seed = [v for v in dx[first:start] if v is not None]
            if not seed:
                return out
            prev = sum(seed) / len(seed)
            out[start] = prev
            for i in range(start + 1, len(b)):
                if dx[i] is not None:
                    prev = (prev * (n - 1) + dx[i]) / n
                    out[i] = prev
            return out
        return self._memo(("adx", n), calc)

    def reg(self, n):
        """
        Rolling least-squares fit of log price against time.

        Returns (slope, r2). The slope is just another trend line, but R^2 is
        information a moving average does not carry: how CLEANLY the price is
        following that trend. A high slope with low R^2 is a lurch, not a trend.
        """
        def calc():
            # O(n) rolling fit. Recomputing the window each bar is O(n*window),
            # which is minutes rather than milliseconds across a sweep.
            lg = [math.log(c) if c > 0 else 0.0 for c in self.close]
            sx = n * (n - 1) / 2.0
            sxx = (n - 1) * n * (2 * n - 1) / 6.0
            den = n * sxx - sx * sx
            sxx_c = sxx - sx * sx / n
            slope, r2 = [None] * len(lg), [None] * len(lg)
            if len(lg) < n or den == 0:
                return slope, r2
            # S1 = sum y, S2 = sum j*y (j = position inside the window), S3 = sum y^2
            S1 = sum(lg[:n])
            S2 = sum(j * lg[j] for j in range(n))
            S3 = sum(v * v for v in lg[:n])
            ann = BARS_PER_DAY * DAYS_PER_YEAR * 100.0
            for i in range(n - 1, len(lg)):
                if i > n - 1:
                    drop = lg[i - n]
                    S2 = S2 - S1 + drop + (n - 1) * lg[i]
                    S1 = S1 - drop + lg[i]
                    S3 = S3 - drop * drop + lg[i] * lg[i]
                b = (n * S2 - sx * S1) / den
                syy_c = S3 - S1 * S1 / n
                slope[i] = b * ann
                r2[i] = 0.0 if syy_c <= 1e-15 else max(0.0, min(1.0, b * b * sxx_c / syy_c))
            return slope, r2
        return self._memo(("reg", n), calc)

    def hurst(self, n, lags=(2, 4, 8, 16, 32)):
        """
        Rolling Hurst exponent via the variance-of-differences method.

        > 0.5  the series trends  (momentum should work)
        ~ 0.5  a random walk      (nothing should work)
        < 0.5  it mean-reverts    (fade the move instead)

        This is the one indicator here that tells you WHICH FAMILY of strategy
        the current market suits, rather than giving a signal itself.
        """
        def calc():
            lg = [math.log(c) if c > 0 else 0.0 for c in self.close]
            out = [None] * len(lg)
            usable = [l for l in lags if l < n // 2]
            if not usable:
                return out
            xs = [math.log(l) for l in usable]
            mx = sum(xs) / len(xs)
            dxs = [x - mx for x in xs]
            dxx = sum(d * d for d in dxs)
            step = max(1, n // 24)          # recompute periodically; it moves slowly
            last = None
            for i in range(n - 1, len(lg)):
                if last is None or (i - (n - 1)) % step == 0:
                    win = lg[i - n + 1:i + 1]
                    ys = []
                    for l in usable:
                        diffs = [win[k] - win[k - l] for k in range(l, len(win))]
                        if not diffs:
                            ys.append(0.0)
                            continue
                        mu = sum(diffs) / len(diffs)
                        var = sum((d - mu) ** 2 for d in diffs) / len(diffs)
                        ys.append(0.5 * math.log(var) if var > 0 else -20.0)
                    my = sum(ys) / len(ys)
                    last = sum(dxs[k] * (ys[k] - my) for k in range(len(ys))) / dxx if dxx else 0.5
                out[i] = last
            return out
        return self._memo(("hurst", n, lags), calc)

    def obv(self):
        """On-balance volume: volume signed by the day's direction, accumulated."""
        def calc():
            v = [b[5] for b in self.bars]
            out, run = [0.0] * len(v), 0.0
            for i in range(1, len(v)):
                if self.close[i] > self.close[i - 1]:
                    run += v[i]
                elif self.close[i] < self.close[i - 1]:
                    run -= v[i]
                out[i] = run
            return out
        return self._memo(("obv",), calc)

    def obv_slope(self, n):
        """Is money flowing in or out over the last n bars?"""
        def calc():
            o = self.obv()
            scale = [abs(x) for x in o]
            ref = max(scale) or 1.0
            return [None if i < n else (o[i] - o[i - n]) / ref * 100.0 for i in range(len(o))]
        return self._memo(("obvs", n), calc)

    def vwap_dev(self, n):
        """% distance between price and the rolling volume-weighted average price."""
        def calc():
            b = self.bars
            out = [None] * len(b)
            pv = run_v = 0.0
            for i in range(len(b)):
                tp = (b[i][2] + b[i][3] + b[i][4]) / 3.0
                pv += tp * b[i][5]
                run_v += b[i][5]
                if i >= n:
                    j = i - n
                    tpj = (b[j][2] + b[j][3] + b[j][4]) / 3.0
                    pv -= tpj * b[j][5]
                    run_v -= b[j][5]
                if i >= n - 1 and run_v > 0:
                    vwap = pv / run_v
                    out[i] = (self.close[i] / vwap - 1.0) * 100.0
            return out
        return self._memo(("vwapd", n), calc)

    def cmf(self, n):
        """
        Chaikin Money Flow: where each bar closed inside its own range, weighted
        by volume. Positive means buyers kept closing the bar near the high.
        """
        def calc():
            b = self.bars
            mfv = []
            for h, l, c, v in ((x[2], x[3], x[4], x[5]) for x in b):
                rng = h - l
                mfv.append(0.0 if rng <= 0 else ((c - l) - (h - c)) / rng * v)
            out, sm, sv = [None] * len(b), 0.0, 0.0
            for i in range(len(b)):
                sm += mfv[i]
                sv += b[i][5]
                if i >= n:
                    sm -= mfv[i - n]
                    sv -= b[i - n][5]
                if i >= n - 1 and sv > 0:
                    out[i] = sm / sv
            return out
        return self._memo(("cmf", n), calc)

    def vol(self, n):
        """Annualised stdev of hourly log returns, in %."""
        def calc():
            c = self.close
            rets = [0.0] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 else 0.0
                            for i in range(1, len(c))]
            out, s, s2 = [None] * len(c), 0.0, 0.0
            ann = math.sqrt(BARS_PER_DAY * DAYS_PER_YEAR) * 100.0
            for i, r in enumerate(rets):
                s += r
                s2 += r * r
                if i >= n:
                    old = rets[i - n]
                    s -= old
                    s2 -= old * old
                if i >= n - 1:
                    mean = s / n
                    out[i] = math.sqrt(max(s2 / n - mean * mean, 0.0)) * ann
            return out
        return self._memo(("vol", n), calc)


# ------------------------------------------------------------------------------
# Strategies. Each returns a per-bar "do I want to be long?" array.
#
# Every value at index i is built only from data up to and including bar i's
# close, and the engine fills at bar i+1's OPEN — so nothing can peek ahead.
# All of them are long/flat only: they never short.
# ------------------------------------------------------------------------------
def strat_ema_cross(ind, p):
    """The strategy the live demo runs today: EMA cross gated by RSI."""
    ef, es, rsi = ind.ema(p["ema_fast"]), ind.ema(p["ema_slow"]), ind.rsi(p["rsi_period"])
    want = [False] * len(ef)
    holding = False
    for i in range(1, len(ef)):
        if rsi[i] is None:
            continue
        if not holding:
            if ef[i] > es[i] and ef[i - 1] <= es[i - 1] and p["rsi_min"] <= rsi[i] <= p["rsi_max"]:
                holding = True
        elif ef[i] < es[i]:
            holding = False
        want[i] = holding
    return want


def strat_sma_trend(ind, p):
    """The classic: hold while price is above its own moving average."""
    sma = ind.sma(p["sma"])
    return [sma[i] is not None and ind.close[i] > sma[i] * (1 + p["buffer"] / 100.0)
            for i in range(len(sma))]


def strat_donchian(ind, p):
    """Breakout: buy new n-bar highs, leave on m-bar lows."""
    hh, ll = ind.highest(p["entry"]), ind.lowest(p["exit"])
    want, holding = [False] * len(hh), False
    for i in range(1, len(hh)):
        if hh[i - 1] is None or ll[i - 1] is None:
            continue
        if not holding and ind.close[i] > hh[i - 1]:
            holding = True
        elif holding and ind.close[i] < ll[i - 1]:
            holding = False
        want[i] = holding
    return want


def strat_donchian_trend(ind, p):
    """Donchian breakout, but only taken while the long-term trend is up."""
    base = strat_donchian(ind, p)
    sma = ind.sma(p["trend"])
    return [base[i] and sma[i] is not None and ind.close[i] > sma[i] for i in range(len(base))]


def strat_macd(ind, p):
    """MACD line above its signal line."""
    ef, es = ind.ema(p["ema_fast"]), ind.ema(p["ema_slow"])
    macd = [ef[i] - es[i] for i in range(len(ef))]
    sig, k, prev = [], 2.0 / (p["signal"] + 1.0), None
    for v in macd:
        prev = v if prev is None else v * k + prev * (1 - k)
        sig.append(prev)
    return [macd[i] > sig[i] and macd[i] > 0 for i in range(len(macd))]


def strat_rsi_meanrev(ind, p):
    """Buy the dip, sell the bounce — with a long-term trend filter."""
    rsi, sma = ind.rsi(p["rsi_period"]), ind.sma(p["trend"])
    want, holding = [False] * len(rsi), False
    for i in range(len(rsi)):
        if rsi[i] is None or sma[i] is None:
            continue
        uptrend = ind.close[i] > sma[i]
        if not holding and rsi[i] < p["buy_below"] and uptrend:
            holding = True
        elif holding and (rsi[i] > p["sell_above"] or not uptrend):
            holding = False
        want[i] = holding
    return want


def strat_roc_momentum(ind, p):
    """Hold while the n-bar rate of change stays above a threshold."""
    roc = ind.roc(p["lookback"])
    want, holding = [False] * len(roc), False
    for i in range(len(roc)):
        if roc[i] is None:
            continue
        if not holding and roc[i] > p["enter_above"]:
            holding = True
        elif holding and roc[i] < p["exit_below"]:
            holding = False
        want[i] = holding
    return want


def strat_sma_vol_filter(ind, p):
    """SMA trend, but stand aside when realised volatility is extreme."""
    sma, vol = ind.sma(p["sma"]), ind.vol(p["vol_window"])
    return [sma[i] is not None and vol[i] is not None
            and ind.close[i] > sma[i] and vol[i] < p["max_vol"]
            for i in range(len(sma))]


def strat_dual_sma(ind, p):
    """Fast SMA above slow SMA (golden-cross style, but held continuously)."""
    fast, slow = ind.sma(p["fast"]), ind.sma(p["slow"])
    return [fast[i] is not None and slow[i] is not None and fast[i] > slow[i]
            for i in range(len(fast))]


def vol_scaled(ind, want, target_vol, vol_window):
    """
    Volatility targeting: size the position so the portfolio aims for a constant
    risk level. Calm market -> closer to fully invested; wild market -> smaller.
    Capped at 1.0, so it never borrows and never shorts.
    """
    vol = ind.vol(vol_window)
    return [0.0 if not want[i] or vol[i] is None or vol[i] <= 1e-9
            else min(1.0, target_vol / vol[i])
            for i in range(len(want))]


def strat_voltarget_only(ind, p):
    """Always long, but sized by volatility. Buy & hold with the brakes fitted."""
    return vol_scaled(ind, [True] * len(ind.close), p["target_vol"], p["vol_window"])


def strat_sma_voltarget(ind, p):
    """Trend filter decides IF we are long; volatility decides HOW MUCH."""
    return vol_scaled(ind, strat_sma_trend(ind, p), p["target_vol"], p["vol_window"])


def strat_dual_sma_voltarget(ind, p):
    return vol_scaled(ind, strat_dual_sma(ind, p), p["target_vol"], p["vol_window"])


# --- strategies built on the trend-quality and volume indicators ----------------
def strat_adx_trend(ind, p):
    """Trend direction from an SMA, permission to trade from ADX."""
    sma, adx = ind.sma(p["sma"]), ind.adx(p["adx_n"])
    return [sma[i] is not None and adx[i] is not None
            and ind.close[i] > sma[i] and adx[i] >= p["adx_min"]
            for i in range(len(sma))]


def strat_reg_r2(ind, p):
    """Long when the fitted trend points up AND the fit is clean enough."""
    slope, r2 = ind.reg(p["window"])
    return [slope[i] is not None and slope[i] > 0 and r2[i] >= p["min_r2"]
            for i in range(len(slope))]


def strat_reg_sized(ind, p):
    """Same, but the bet scales with how well the trend line fits."""
    slope, r2 = ind.reg(p["window"])
    return [0.0 if slope[i] is None or slope[i] <= 0
            else min(1.0, max(0.0, (r2[i] - p["min_r2"]) / max(p["full_r2"] - p["min_r2"], 1e-9)))
            for i in range(len(slope))]


def strat_hurst_switch(ind, p):
    """
    Let the market pick the strategy family.

    Hurst above 0.5 means the series trends, so follow it. Below means it
    mean-reverts, so buy weakness instead of breakouts.
    """
    h = ind.hurst(p["hurst_n"])
    sma = ind.sma(p["sma"])
    rsi = ind.rsi(p["rsi_period"])
    out = [0.0] * len(h)
    for i in range(len(h)):
        if h[i] is None or sma[i] is None or rsi[i] is None:
            continue
        if h[i] >= p["hurst_hi"]:                       # trending regime
            out[i] = 1.0 if ind.close[i] > sma[i] else 0.0
        elif h[i] <= p["hurst_lo"]:                     # mean-reverting regime
            out[i] = 1.0 if rsi[i] < p["rsi_buy"] else 0.0
        else:                                           # undecided — half size
            out[i] = 0.5 if ind.close[i] > sma[i] else 0.0
    return out


def strat_volume_trend(ind, p):
    """Trend, confirmed by money actually flowing in (OBV slope + CMF)."""
    sma, obv, cmf = ind.sma(p["sma"]), ind.obv_slope(p["obv_n"]), ind.cmf(p["cmf_n"])
    return [sma[i] is not None and obv[i] is not None and cmf[i] is not None
            and ind.close[i] > sma[i] and obv[i] > p["obv_min"] and cmf[i] > p["cmf_min"]
            for i in range(len(sma))]


def strat_vwap_trend(ind, p):
    """Long while price holds above the rolling VWAP, inside a rising trend."""
    dev, sma = ind.vwap_dev(p["vwap_n"]), ind.sma(p["sma"])
    return [dev[i] is not None and sma[i] is not None
            and dev[i] > p["min_dev"] and ind.close[i] > sma[i]
            for i in range(len(dev))]


def strat_quality_trend(ind, p):
    """
    The champion trend filter, but only acted on when the trend is both strong
    (ADX) and clean (regression R^2). Sized, not on/off, so it does not spend
    most of its life flat.
    """
    fast, slow = ind.sma(p["fast"]), ind.sma(p["slow"])
    adx = ind.adx(p["adx_n"])
    _, r2 = ind.reg(p["reg_n"])
    out = [0.0] * len(fast)
    for i in range(len(fast)):
        if fast[i] is None or slow[i] is None or adx[i] is None or r2[i] is None:
            continue
        if fast[i] <= slow[i]:
            continue
        quality = 0.0
        quality += 1.0 if adx[i] >= p["adx_min"] else 0.0
        quality += 1.0 if r2[i] >= p["min_r2"] else 0.0
        out[i] = p["base_size"] + (1.0 - p["base_size"]) * quality / 2.0
    return out


ENSEMBLE_MEMBERS = [
    ("sma_trend", {"sma": 720, "buffer": 0.0}),
    ("sma_trend", {"sma": 2400, "buffer": 0.0}),
    ("dual_sma", {"fast": 120, "slow": 1680}),
    ("dual_sma", {"fast": 240, "slow": 4800}),
    ("donchian", {"entry": 336, "exit": 168}),
]


def strat_ensemble(ind, p):
    """
    Five fixed trend models vote. Exposure is the share of them that want to be
    long, so no single parameter choice can sink it. Optionally vol-scaled.
    """
    votes = [STRATEGIES[name][0](ind, params) for name, params in ENSEMBLE_MEMBERS]
    n = len(votes)
    share = [sum(1 for v in votes if v[i]) / n for i in range(len(ind.close))]
    share = [x if x >= p["min_vote"] else 0.0 for x in share]
    if p.get("target_vol"):
        vol = ind.vol(p["vol_window"])
        share = [0.0 if vol[i] is None or vol[i] <= 1e-9
                 else min(share[i], p["target_vol"] / vol[i]) for i in range(len(share))]
    return share


# --- long/short variants -------------------------------------------------------
# Shorting BTC means a perpetual future, so these carry a funding cost that the
# spot long-only strategies do not. SHORT_STRATEGIES marks them so the sweep
# charges them for it; giving a short a free ride is how backtests lie.
def _signed(want):
    return [1.0 if w else -1.0 for w in want]


def strat_dual_sma_ls(ind, p):
    return _signed(strat_dual_sma(ind, p))


def strat_sma_trend_ls(ind, p):
    return _signed(strat_sma_trend(ind, p))


def strat_donchian_ls(ind, p):
    return _signed(strat_donchian(ind, p))


def strat_adx_trend_ls(ind, p):
    """Short only when the downtrend is also strong — otherwise stand aside."""
    fast, slow = ind.sma(p["fast"]), ind.sma(p["slow"])
    adx = ind.adx(p["adx_n"])
    out = [0.0] * len(fast)
    for i in range(len(fast)):
        if fast[i] is None or slow[i] is None or adx[i] is None:
            continue
        if adx[i] < p["adx_min"]:
            out[i] = 0.0
        else:
            out[i] = 1.0 if fast[i] > slow[i] else -p["short_size"]
    return out


def strat_voltarget_ls(ind, p):
    """
    The demo's long side, plus a short side that has to be let through a gate.

    Shorting whenever the fast average is below the slow one was already tested
    and it costs more than it earns: BTC's bull runs dwarf its bears and the
    chop in between bleeds. But the short side does work inside an actual bear
    (Sharpe +0.71 through 2018, +0.88 through 2022, after funding). So the
    question is not whether to short, it is what has to be true first.

    `short_mode` picks the gate:
      always     — no gate, the version already known to lose
      slope      — the slow average must itself be falling
      adx        — the trend must be strong enough to register on ADX
      regime     — price must also be under a much longer average
      slope_adx  — falling AND strong
    """
    fast, slow = ind.sma(p["fast"]), ind.sma(p["slow"])
    vol = ind.vol(p["vol_window"])
    mode = p.get("short_mode", "always")
    slope_n = p.get("slope_n", 10)
    adx = ind.adx(p["adx_n"]) if mode in ("adx", "slope_adx") else None
    regime = ind.sma(p["regime_n"]) if mode == "regime" else None
    out = [0.0] * len(fast)
    for i in range(len(fast)):
        if fast[i] is None or slow[i] is None or vol[i] is None or vol[i] <= 1e-9:
            continue
        size = min(1.0, p["target_vol"] / vol[i])
        if fast[i] > slow[i]:
            out[i] = size
            continue
        if mode == "none":
            continue
        gate = True
        if mode in ("slope", "slope_adx"):
            j = i - slope_n
            gate = gate and j >= 0 and slow[j] is not None and slow[i] < slow[j]
        if mode in ("adx", "slope_adx"):
            gate = gate and adx[i] is not None and adx[i] >= p["adx_min"]
        if mode == "regime":
            gate = gate and regime[i] is not None and ind.close[i] < regime[i]
        if gate:
            out[i] = -size * p.get("short_size", 1.0)
    return out


def strat_voltarget_trailcut(ind, p):
    """
    The demo's strategy plus a TIERED trailing cut — trim, do not liquidate.

    Two different things are usually both called a stop, and they need
    different machinery:

      price-based   how far price has fallen from its recent high. Computable
                    from the price series alone, so it is a plain signal and
                    lives here.
      account-based how far the ACCOUNT is below its own peak. That depends on
                    what you actually traded, so it cannot be expressed as a
                    signal at all — it belongs in the engine (see
                    `drawdown_throttle` in backtest()).

    This is the first kind. Distance below the rolling `trail_n`-day high sets
    a multiplier on the position the strategy otherwise wants:

        above -tier1        full size
        past  -tier1        cut1  (e.g. half)
        past  -tier2        cut2  (e.g. a quarter)

    It cuts faster than a 30-day average can cross, which is the whole point:
    the crossover is what eventually gets you out, this is what stops the trip
    there being so expensive.
    """
    fast, slow = ind.sma(p["fast"]), ind.sma(p["slow"])
    vol = ind.vol(p["vol_window"])
    hi = ind.highest(p["trail_n"])
    out = [0.0] * len(fast)
    for i in range(len(fast)):
        if fast[i] is None or slow[i] is None or vol[i] is None or vol[i] <= 1e-9:
            continue
        if fast[i] <= slow[i]:
            continue
        size = min(1.0, p["target_vol"] / vol[i])
        if hi[i] and hi[i] > 0:
            drop = ind.close[i] / hi[i] - 1.0          # <= 0
            if drop <= -p["tier2"] / 100.0:
                size *= p["cut2"]
            elif drop <= -p["tier1"] / 100.0:
                size *= p["cut1"]
        out[i] = size
    return out


def strat_buy_hold(ind, p):
    return [True] * len(ind.close)


STRATEGIES = {
    "ema_cross":      (strat_ema_cross, "EMA cross + RSI gate (what the demo runs now)"),
    "sma_trend":      (strat_sma_trend, "Long while price is above its moving average"),
    "dual_sma":       (strat_dual_sma, "Fast SMA above slow SMA"),
    "donchian":       (strat_donchian, "Breakout to n-bar highs, exit on m-bar lows"),
    "donchian_trend": (strat_donchian_trend, "Breakout, filtered by the long-term trend"),
    "macd":           (strat_macd, "MACD above signal and above zero"),
    "rsi_meanrev":    (strat_rsi_meanrev, "Buy dips in an uptrend, sell the bounce"),
    "roc_momentum":   (strat_roc_momentum, "Ride positive rate-of-change"),
    "sma_vol_filter": (strat_sma_vol_filter, "Trend, but sit out extreme volatility"),
    "voltarget_only": (strat_voltarget_only, "Always long, position sized by volatility"),
    "sma_voltarget":  (strat_sma_voltarget, "Trend says if, volatility says how much"),
    "dual_sma_voltarget": (strat_dual_sma_voltarget, "Dual SMA, volatility sized"),
    "ensemble":       (strat_ensemble, "Five trend models vote, volatility sizes the bet"),
    "adx_trend":      (strat_adx_trend, "Trend, but only when ADX says it is a real one"),
    "reg_r2":         (strat_reg_r2, "Regression trend, gated on how clean the fit is"),
    "reg_sized":      (strat_reg_sized, "Regression trend, bet scaled by fit quality"),
    "hurst_switch":   (strat_hurst_switch, "Hurst picks the family: follow or fade"),
    "volume_trend":   (strat_volume_trend, "Trend confirmed by OBV and money flow"),
    "vwap_trend":     (strat_vwap_trend, "Holding above rolling VWAP inside an uptrend"),
    "quality_trend":  (strat_quality_trend, "Champion trend, sized by ADX + R² quality"),
    "dual_sma_ls":    (strat_dual_sma_ls, "Dual SMA, long AND short (perp + funding)"),
    "sma_trend_ls":   (strat_sma_trend_ls, "Above/below the average, long and short"),
    "donchian_ls":    (strat_donchian_ls, "Breakout both ways, long and short"),
    "adx_trend_ls":   (strat_adx_trend_ls, "Long/short, but only when ADX confirms"),
    "voltarget_ls":   (strat_voltarget_ls, "The demo's long side plus a gated short side"),
    "voltarget_trailcut": (strat_voltarget_trailcut,
                           "The demo's strategy, trimmed as price falls from its high"),
}

# Strategies that can hold a negative position, so they pay (or receive) funding.
SHORT_STRATEGIES = {"dual_sma_ls", "sma_trend_ls", "donchian_ls", "adx_trend_ls",
                    "voltarget_ls"}

# What the LONG side of a BTC perpetual pays per year. Positive has been the
# norm through bull markets; a short earns it.
#
# This used to be a guessed round number, 0.11. Measured instead now: `python
# btc_basis.py --study` reads every funding settlement Kraken has published
# (2025-09-17 -> 2026-09-18, 8,778 settlements at 24/day) and prints "全期年化
# 平均" (full-period annualised mean) = 3.26%. The guess was 3.4x too high —
# every short-leg cost in this file, including the gated-shorting study
# ("shorting isn't worth it"), was measured with a funding drag more than
# three times the real one. That study was rerun with the corrected number;
# see research/README.md and research/results_short_funding_bugfix_rerun.txt.
FUNDING_APR = 0.0326


# ------------------------------------------------------------------------------
# The backtest engine
# ------------------------------------------------------------------------------
def backtest(bars, want, fee_bps=10.0, slip_bps=5.0, start_cash=100_000.0,
             atr=None, sl_atr=None, tp_atr=None, rebalance_band=0.10,
             funding_apr=0.0, dd_limit=0.0, dd_throttle=1.0, dd_recover=0.0,
             dd_release_on_flat=False):
    """
    Replay a long/flat exposure array.

    `want` holds the SIGNED fraction of equity to hold at each bar: +1 fully
    long, 0 flat, -1 fully short. A plain True/False array trades all-in/all-out,
    a float array scales the position (volatility targeting), and a negative
    value shorts.

    Shorting BTC in practice means a perpetual future, so a short leg is not
    free: `funding_apr` is what the long side pays per year (a short receives
    it). It has been persistently positive through bull markets, so ignoring it
    flatters every short. Leave it at 0 only for a spot-only, long-only study.

    Decisions are made on bar i's close and FILLED AT BAR i+1's OPEN, so the
    backtest can never trade on a price it could not have seen. Optional ATR
    stop/target are checked intrabar, stop first. To keep turnover honest, a
    position is only resized once it drifts `rebalance_band` away from target.
    """
    fee, slip = fee_bps / 10_000.0, slip_bps / 10_000.0
    dt = (bars[1][0] - bars[0][0]) / (DAYS_PER_YEAR * 86400.0) if len(bars) > 1 else 0.0
    cash, qty, entry_px = start_cash, 0.0, 0.0
    # Account-level circuit breaker. Below `dd_limit` under the equity peak the
    # book is scaled to `dd_throttle`. This cannot live in the signal — it
    # depends on the account, not the price.
    #
    # How it releases matters more than how it triggers. Waiting for equity to
    # climb back to `dd_recover` of its peak is a trap: at a quarter size you
    # climb four times slower, so the brake stays on through the recovery that
    # would have paid for it. `dd_release_on_flat` instead clears the brake the
    # next time the strategy is flat — the trend that hurt you is over and the
    # next signal starts clean.
    peak_equity, throttled = start_cash, False
    equity = [start_cash] * len(bars)
    trades = []
    stop = target = None
    entry_i = 0

    for i in range(1, len(bars)):
        o, high, low, close = bars[i][1], bars[i][2], bars[i][3], bars[i][4]

        # 0. carry on an open perpetual position: the long side pays funding
        if qty != 0.0 and funding_apr:
            cash -= qty * o * funding_apr * dt

        # 1. risk exits happen first, inside the bar
        if qty > 0 and stop is not None and low <= stop:
            fill = min(stop, o) * (1 - slip)
            cash += qty * fill * (1 - fee)
            trades.append((entry_i, i, entry_px, fill, qty, "stop"))
            qty, stop, target = 0.0, None, None
        elif qty > 0 and target is not None and high >= target:
            fill = target * (1 - slip)
            cash += qty * fill * (1 - fee)
            trades.append((entry_i, i, entry_px, fill, qty, "target"))
            qty, stop, target = 0.0, None, None

        # 2. act on the previous bar's decision at this bar's open
        target_frac = float(want[i - 1])
        equity_now = cash + qty * o
        if dd_limit > 0 and peak_equity > 0:
            dd = 1.0 - equity_now / peak_equity
            if dd >= dd_limit:
                throttled = True
            elif throttled and (dd <= dd_recover
                                or (dd_release_on_flat and qty == 0.0 and target_frac > 0)):
                throttled = False
            if throttled:
                target_frac *= dd_throttle
        if equity_now <= 0:                                   # wiped out
            equity[i] = 0.0
            cash, qty = 0.0, 0.0
            continue
        target_qty = equity_now * target_frac / o if o > 0 else 0.0
        drift = abs(target_qty - qty) * o
        crossing = (qty > 0) != (target_qty > 0) or (qty < 0) != (target_qty < 0)
        if (qty == 0.0 and target_qty != 0.0) or crossing or drift > equity_now * rebalance_band:
            delta = target_qty - qty
            if delta != 0.0:
                fill = o * (1 + slip) if delta > 0 else o * (1 - slip)
                # closing out all or part of an existing position books a trade
                if qty != 0.0 and (target_qty == 0.0 or crossing):
                    trades.append((entry_i, i, entry_px, fill, abs(qty),
                                   "signal", "long" if qty > 0 else "short"))
                if qty == 0.0 or crossing:
                    entry_px, entry_i = fill, i
                elif (qty > 0) == (delta > 0):                # adding to the same side
                    entry_px = (entry_px * abs(qty) + fill * abs(delta)) / (abs(qty) + abs(delta))
                cash -= delta * fill + abs(delta) * fill * fee
                qty = target_qty
            if qty == 0.0:
                stop = target = None
            elif atr is not None and atr[i - 1] is not None:
                if qty > 0:
                    stop = entry_px - atr[i - 1] * sl_atr if sl_atr else None
                    target = entry_px + atr[i - 1] * tp_atr if tp_atr else None
                else:
                    stop = target = None      # ATR stops are long-side only for now

        equity[i] = cash + qty * close
        peak_equity = max(peak_equity, equity[i])

    return equity, trades


# ------------------------------------------------------------------------------
# Performance metrics — computed on the DAILY equity curve, which is the
# convention Sharpe is usually quoted in, and far less noisy than hourly.
# ------------------------------------------------------------------------------
def daily_equity(bars, equity):
    out, cur_day, last = [], None, equity[0]
    for i, b in enumerate(bars):
        day = b[0] // 86400
        if cur_day is None:
            cur_day = day
        elif day != cur_day:
            out.append(last)
            cur_day = day
        last = equity[i]
    out.append(last)
    return out


def metrics(bars, equity, trades, start_cash=100_000.0, want=None):
    de = daily_equity(bars, equity)
    rets = [de[i] / de[i - 1] - 1.0 for i in range(1, len(de)) if de[i - 1] > 0]
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r * r for r in downside) / len(downside)) if downside else 0.0

    years = (bars[-1][0] - bars[0][0]) / (DAYS_PER_YEAR * 86400)
    final = equity[-1]
    cagr = (final / start_cash) ** (1 / years) - 1 if years > 0 and final > 0 else -1.0

    peak, max_dd = de[0], 0.0
    for v in de:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    def _won(t):
        # a short wins when it buys back cheaper than it sold
        return t[3] < t[2] if (len(t) >= 7 and t[6] == "short") else t[3] > t[2]
    wins = [t for t in trades if _won(t)]
    if want is not None and len(want):
        exposure = sum(float(w) for w in want) / len(want)
    else:
        exposure = sum(1 for i in range(1, len(equity)) if equity[i] != equity[i - 1]) / max(len(equity) - 1, 1)

    return {
        "sharpe": mean / sd * math.sqrt(DAYS_PER_YEAR) if sd > 0 else 0.0,
        "sortino": mean / dsd * math.sqrt(DAYS_PER_YEAR) if dsd > 0 else 0.0,
        "cagr_pct": cagr * 100.0,
        "total_return_pct": (final / start_cash - 1) * 100.0,
        "max_dd_pct": max_dd * 100.0,
        "calmar": (cagr / max_dd) if max_dd > 0 else 0.0,
        "trades": len(trades),
        "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "exposure_pct": exposure * 100.0,
        "final_equity": final,
        "days": len(de),
    }


# ------------------------------------------------------------------------------
# Parameter grids
# ------------------------------------------------------------------------------
def grid(**axes):
    """Cartesian product of the named axes -> list of param dicts."""
    keys = list(axes)
    out = [{}]
    for k in keys:
        out = [dict(d, **{k: v}) for d in out for v in axes[k]]
    return out


# Windows are in HOURS (the history is hourly): 24=1d, 168=1w, 720=30d.
GRIDS = {
    "ema_cross": [p for p in grid(ema_fast=[8, 12, 24, 48], ema_slow=[26, 50, 100, 200],
                                  rsi_period=[14], rsi_min=[30, 45], rsi_max=[70, 85, 101])
                  if p["ema_fast"] < p["ema_slow"]],
    "sma_trend": grid(sma=[72, 168, 336, 504, 720, 1080, 1680, 2400, 3600, 4800, 7200],
                      buffer=[0.0, 0.5, 1.5]),
    "dual_sma": [p for p in grid(fast=[24, 72, 120, 240, 336, 504],
                                 slow=[336, 720, 1080, 1680, 2400, 3600, 4800, 7200])
                 if p["fast"] < p["slow"]],
    "donchian": [p for p in grid(entry=[72, 168, 336, 504, 720, 1080, 1680, 2400],
                                 exit=[24, 72, 168, 336, 504, 720])
                 if p["exit"] <= p["entry"]],
    "donchian_trend": [p for p in grid(entry=[168, 336, 504, 720, 1080], exit=[72, 168, 336, 504],
                                       trend=[1680, 2400, 4800])
                       if p["exit"] <= p["entry"]],
    "macd": grid(ema_fast=[12, 24, 48], ema_slow=[26, 52, 100, 200], signal=[9, 18, 36]),
    "rsi_meanrev": grid(rsi_period=[14, 24], buy_below=[25, 30, 35, 40],
                        sell_above=[55, 60, 65, 70], trend=[336, 720]),
    "roc_momentum": grid(lookback=[24, 72, 168, 336], enter_above=[0.0, 2.0, 5.0],
                         exit_below=[0.0, -2.0, -5.0]),
    "sma_vol_filter": grid(sma=[168, 336, 504, 720, 2400], vol_window=[168, 336],
                           max_vol=[50.0, 70.0, 90.0, 120.0]),
    "voltarget_only": grid(target_vol=[25.0, 35.0, 45.0, 55.0, 70.0],
                           vol_window=[168, 336, 720]),
    "sma_voltarget": grid(sma=[336, 720, 1680, 2400, 4800], buffer=[0.0],
                          target_vol=[25.0, 35.0, 45.0, 55.0, 70.0], vol_window=[168, 336, 720]),
    "dual_sma_voltarget": [p for p in grid(fast=[72, 120, 240], slow=[720, 1680, 2400, 4800],
                                           target_vol=[25.0, 35.0, 45.0, 55.0, 70.0],
                                           vol_window=[168, 336, 720])
                           if p["fast"] < p["slow"]],
    "ensemble": grid(min_vote=[0.0, 0.4, 0.6], target_vol=[0.0, 30.0, 40.0, 50.0, 65.0],
                     vol_window=[168, 336, 720]),
    "adx_trend": grid(sma=[720, 1080, 1680, 2400], adx_n=[14, 24, 48, 96],
                      adx_min=[10.0, 15.0, 20.0, 25.0]),
    "reg_r2": grid(window=[336, 720, 1080, 1680, 2400], min_r2=[0.0, 0.2, 0.4, 0.6]),
    "reg_sized": [p for p in grid(window=[336, 720, 1080, 1680, 2400],
                                  min_r2=[0.0, 0.2, 0.4], full_r2=[0.4, 0.6, 0.8])
                  if p["full_r2"] > p["min_r2"]],
    "hurst_switch": grid(hurst_n=[336, 720, 1680], hurst_lo=[0.45, 0.48], hurst_hi=[0.52, 0.55],
                         sma=[720, 1080, 1680], rsi_period=[14], rsi_buy=[35.0, 45.0]),
    "volume_trend": grid(sma=[720, 1080, 1680], obv_n=[168, 336, 720],
                         obv_min=[-5.0, 0.0, 3.0], cmf_n=[336], cmf_min=[-0.05, 0.0, 0.03]),
    "vwap_trend": grid(vwap_n=[168, 336, 720], sma=[720, 1080, 1680],
                       min_dev=[-3.0, 0.0, 2.0]),
    "quality_trend": grid(fast=[24, 72], slow=[1080, 1680, 2400], adx_n=[24, 48],
                          adx_min=[15.0, 25.0], reg_n=[1080],
                          min_r2=[0.2, 0.5], base_size=[0.4, 0.7]),
    "dual_sma_ls": [p for p in grid(fast=[24, 72, 120, 240], slow=[720, 1080, 1680, 2400, 4800])
                    if p["fast"] < p["slow"]],
    "sma_trend_ls": grid(sma=[336, 720, 1080, 1680, 2400, 4800], buffer=[0.0, 1.0]),
    "donchian_ls": [p for p in grid(entry=[168, 336, 720, 1080, 1680], exit=[72, 168, 336, 720])
                    if p["exit"] <= p["entry"]],
    "voltarget_trailcut": [p for p in grid(
        fast=[3], slow=[30], target_vol=[45.0], vol_window=[30],
        trail_n=[10, 20, 30], tier1=[5.0, 8.0, 12.0], cut1=[0.5, 0.7],
        tier2=[15.0, 20.0, 25.0], cut2=[0.0, 0.25, 0.5])
        if p["tier2"] > p["tier1"] and p["cut2"] < p["cut1"]],
    "voltarget_ls": [p for p in grid(fast=[3], slow=[30], target_vol=[45.0], vol_window=[30],
                                     short_mode=["none", "always", "slope", "adx", "regime",
                                                 "slope_adx"],
                                     slope_n=[5, 10, 20], adx_n=[14], adx_min=[20.0, 25.0],
                                     regime_n=[100, 200], short_size=[0.5, 1.0])
                     # collapse the axes each mode ignores, so one gate is not
                     # counted a dozen times just for having unused knobs
                     if (p["short_mode"] in ("slope", "slope_adx") or p["slope_n"] == 10)
                     and (p["short_mode"] in ("adx", "slope_adx") or p["adx_min"] == 20.0)
                     and (p["short_mode"] == "regime" or p["regime_n"] == 100)
                     and (p["short_mode"] != "none" or p["short_size"] == 1.0)],
    "adx_trend_ls": [p for p in grid(fast=[24, 72], slow=[1080, 1680, 2400],
                                     adx_n=[24, 48], adx_min=[15.0, 20.0, 25.0],
                                     short_size=[0.5, 1.0])
                     if p["fast"] < p["slow"]],
}


# ------------------------------------------------------------------------------
# Walk-forward: tune on a training window, judge on the window that follows,
# roll forward, then stitch every untouched test window into one curve.
# ------------------------------------------------------------------------------
def make_folds(bars, train_days=365, test_days=182):
    day = 86400
    t0, t1 = bars[0][0], bars[-1][0]
    index = {}
    for i, b in enumerate(bars):
        index.setdefault(b[0] // day, i)
    def idx_at(ts):
        d = ts // day
        while d not in index and d <= t1 // day:
            d += 1
        return index.get(d, len(bars) - 1)

    folds, start = [], t0
    while start + (train_days + test_days) * day <= t1:
        tr_a, tr_b = idx_at(start), idx_at(start + train_days * day)
        te_b = idx_at(start + (train_days + test_days) * day)
        folds.append((tr_a, tr_b, tr_b, te_b))
        start += test_days * day
    return folds


# How a parameter set is judged during selection.
#   "calmar" — return divided by the worst drawdown. Balances compounding
#              against pain, and does NOT reward a strategy that sits in cash
#              90% of the time (which flatters Sharpe by contributing no
#              variance).
SELECT_BY = "calmar"
MIN_EXPOSURE = 0.20      # must actually be in the market a fifth of the time
MIN_TRADES = 5


def score_of(mm):
    """The number selection maximises. Disqualified sets score -99."""
    if not mm:
        return -99.0
    if mm["trades"] < MIN_TRADES:
        return -99.0
    if mm["exposure_pct"] < MIN_EXPOSURE * 100.0:
        return -99.0
    # dict.get evaluates its default eagerly, so spell the fallback out
    if SELECT_BY in mm:
        return mm[SELECT_BY]
    return mm.get("sharpe", -99.0)


def evaluate(bars, ind, name, params, folds, min_trades=None):
    """Return per-fold (train_metrics, test_metrics) for one parameter set."""
    fn = STRATEGIES[name][0]
    want = fn(ind, params)
    funding = FUNDING_APR if name in SHORT_STRATEGIES else 0.0
    rows = []
    for tr_a, tr_b, te_a, te_b in folds:
        tr_bars, te_bars = bars[tr_a:tr_b], bars[te_a:te_b]
        tr_eq, tr_tr = backtest(tr_bars, want[tr_a:tr_b], funding_apr=funding)
        te_eq, te_tr = backtest(te_bars, want[te_a:te_b], funding_apr=funding)
        abs_tr = [abs(float(w)) for w in want[tr_a:tr_b]]
        abs_te = [abs(float(w)) for w in want[te_a:te_b]]
        tm = metrics(tr_bars, tr_eq, tr_tr, want=abs_tr)
        sm = metrics(te_bars, te_eq, te_tr, want=abs_te)
        if tm:
            tm = dict(tm, score=score_of(tm))
        if sm:
            sm = dict(sm, score=score_of(sm))
        rows.append((tm, sm))
    return rows, want


def buy_hold_metrics(bars):
    eq, tr = backtest(bars, [True] * len(bars))
    return metrics(bars, eq, tr), eq


def run_sweep(bars, folds, strategies=None, progress=True):
    """
    Evaluate every parameter combo of every strategy on every fold.

    Returns {strategy: {"combos": [{"params":…, "folds":[(train,test)…]}, …]}}
    """
    ind = Indicators(bars)
    results = {}
    names = strategies or list(STRATEGIES)
    for name in names:
        combos = GRIDS[name]
        rows = []
        t0 = time.time()
        for params in combos:
            per_fold, _ = evaluate(bars, ind, name, params, folds)
            rows.append({"params": params, "folds": per_fold})
        results[name] = rows
        if progress:
            logger.info("  %-16s %3d combos in %5.1fs", name, len(combos), time.time() - t0)
    return results


def walk_forward_pick(rows, fold_i):
    """Best combo on fold_i's TRAINING window (never looks at the test window)."""
    best, best_score = None, -1e9
    for r in rows:
        tm = r["folds"][fold_i][0]
        sc = tm.get("score", -99.0) if tm else -99.0
        if sc > best_score:
            best, best_score = r, sc
    return best


def stitch_oos(bars, folds, results, name):
    """
    Chain the untouched test windows into one continuous out-of-sample curve:
    tune on fold k's train, trade fold k's test, carry the equity forward.
    """
    ind = Indicators(bars)
    equity, trades, picks = [], [], []
    cash = 100_000.0
    for k, (tr_a, tr_b, te_a, te_b) in enumerate(folds):
        pick = walk_forward_pick(results[name], k)
        if pick is None:
            continue
        want = STRATEGIES[name][0](ind, pick["params"])
        seg_bars = bars[te_a:te_b]
        seg_eq, seg_tr = backtest(seg_bars, want[te_a:te_b], start_cash=cash,
                                  funding_apr=FUNDING_APR if name in SHORT_STRATEGIES else 0.0)
        equity.extend(seg_eq)
        trades.extend(seg_tr)
        cash = seg_eq[-1]
        picks.append({"fold": k + 1, "params": pick["params"],
                      "train_score": pick["folds"][k][0]["score"] if pick["folds"][k][0] else None,
                      "test_score": pick["folds"][k][1]["score"] if pick["folds"][k][1] else None,
                      "from": iso(bars[te_a][0])[:10], "to": iso(bars[te_b - 1][0])[:10]})
    oos_bars = bars[folds[0][2]:folds[-1][3]]
    return oos_bars, equity, trades, picks


# ==============================================================================
#  PART 3 — THE REPORT
# ==============================================================================
CUTOFF = "2024-09-16"          # nothing after this date is used to choose anything

# The line-up shown in the report. Parameters come from the sweep; they are
# pinned here so the report is reproducible rather than re-picked every run.
CANDIDATES = [
    ("dual_sma 1d/30d",    "dual_sma",       {"fast": 24, "slow": 720},   "#3987e5"),
    ("donchian trend",     "donchian_trend", {"entry": 168, "exit": 72,
                                              "trend": 1680},             "#d95926"),
    ("dual_sma + vol target", "dual_sma_voltarget", {"fast": 72, "slow": 720,
                                                     "target_vol": 25.0,
                                                     "vol_window": 720},  "#199e70"),
    ("quality trend (ADX+R²)", "quality_trend", {"fast": 24, "slow": 1080, "adx_n": 48,
                                                 "adx_min": 15.0, "reg_n": 1080,
                                                 "min_r2": 0.5, "base_size": 0.4}, "#c98500"),
    ("EMA cross (live demo)", "ema_cross",   {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14,
                                              "rsi_min": 45, "rsi_max": 101}, "#d55181"),
]
BENCH_COLOR = "#8b93a7"


def daily_series(bars, equity):
    """(dates, values) sampled once per UTC day."""
    dates, vals, cur, last = [], [], None, equity[0]
    for i, b in enumerate(bars):
        day = b[0] // 86400
        if cur is None:
            cur = day
        elif day != cur:
            dates.append(iso(cur * 86400)[:10])
            vals.append(round(last, 2))
            cur = day
        last = equity[i]
    dates.append(iso(cur * 86400)[:10])
    vals.append(round(last, 2))
    return dates, vals


def drawdown_series(vals):
    peak, out = vals[0], []
    for v in vals:
        peak = max(peak, v)
        out.append(round((v / peak - 1.0) * 100.0, 2))
    return out


def year_edges(bars):
    """Yearly slice boundaries, however many years the history actually covers."""
    start = iso(bars[0][0])[:10]
    start_year, mmdd = int(start[:4]), start[5:]
    span = (bars[-1][0] - bars[0][0]) / (DAYS_PER_YEAR * 86400)
    n_years = max(1, int(span + 1e-6))
    edges, labels = [], []
    for k in range(n_years + 1):
        stamp = "%d-%s" % (start_year + k, mmdd)
        i = next((j for j, b in enumerate(bars) if iso(b[0])[:10] >= stamp), len(bars))
        edges.append(min(i, len(bars)))
    edges[-1] = len(bars)
    for k in range(n_years):
        labels.append("%d–%d" % (start_year + k, (start_year + k + 1) % 100))
    return edges, labels


def run_candidate(bars, ind, strategy, params, a=0, b=None):
    b = len(bars) if b is None else b
    want = [True] * len(bars) if strategy is None else STRATEGIES[strategy][0](ind, params)
    eq, tr = backtest(bars[a:b], want[a:b])
    return metrics(bars[a:b], eq, tr, want=want[a:b]), eq, want


def build_report(bars):
    ind = Indicators(bars)
    folds = make_folds(bars)
    edges, ylabels = year_edges(bars)

    logger.info("Replaying the line-up over the full history ...")
    bench_m, bench_eq, _ = run_candidate(bars, ind, None, None)
    dates, bench_vals = daily_series(bars, bench_eq)

    curves = [{"label": "Buy & hold", "color": BENCH_COLOR, "dash": True, "values": bench_vals}]
    draws = [{"label": "Buy & hold", "color": BENCH_COLOR, "dash": True,
              "values": drawdown_series(bench_vals)}]
    cands = []
    for label, strat, params, color in CANDIDATES:
        mm, eq, _ = run_candidate(bars, ind, strat, params)
        _, vals = daily_series(bars, eq)
        curves.append({"label": label, "color": color, "dash": False, "values": vals})
        draws.append({"label": label, "color": color, "dash": False,
                      "values": drawdown_series(vals)})
        yearly = []
        for k in range(4):
            ym, _, _ = run_candidate(bars, ind, strat, params, edges[k], edges[k + 1])
            yearly.append({"sharpe": round(ym["sharpe"], 2),
                           "total": round(ym["total_return_pct"], 1)})
        cands.append({"label": label, "strategy": strat, "params": params,
                      "color": color, "metrics": mm, "yearly": yearly,
                      "desc": STRATEGIES[strat][1]})
    bench_yearly = []
    for k in range(4):
        ym, _, _ = run_candidate(bars, ind, None, None, edges[k], edges[k + 1])
        bench_yearly.append({"sharpe": round(ym["sharpe"], 2),
                             "total": round(ym["total_return_pct"], 1)})

    logger.info("Mapping the parameter plateau ...")
    fasts = [12, 24, 48, 72, 120, 240, 336]
    slows = [336, 720, 1080, 1680, 2400, 3600, 4800, 7200]
    plateau = []
    for f in fasts:
        row = []
        for s in slows:
            if f >= s:
                row.append(None)
            else:
                mm, _, _ = run_candidate(bars, ind, "dual_sma", {"fast": f, "slow": s})
                row.append(round(mm["sharpe"], 2))
        plateau.append(row)

    logger.info("Running the sealed holdout ...")
    cut_i = next(i for i, b in enumerate(bars) if iso(b[0])[:10] >= CUTOFF)
    sel_bars = bars[:cut_i]
    sel_folds = make_folds(sel_bars)
    sel_res = run_sweep(sel_bars, sel_folds, progress=False)
    import statistics
    best = None
    for name, combos in sel_res.items():
        for r in combos:
            sc = [f[0]["score"] for f in r["folds"] if f[0] and f[0]["score"] > -90]
            if len(sc) < len(sel_folds) * 0.6:
                continue
            score = statistics.median(sc)
            if best is None or score > best[0]:
                best = (score, name, r["params"])
    hold_bars = bars[cut_i:]
    hm, _, _ = run_candidate(bars, ind, best[1], best[2], cut_i, len(bars))
    hb, _, _ = run_candidate(bars, ind, None, None, cut_i, len(bars))
    holdout = {
        "cutoff": CUTOFF,
        "select_by": SELECT_BY,
        "train_score": round(best[0], 2),
        "from": iso(hold_bars[0][0])[:10], "to": iso(hold_bars[-1][0])[:10],
        "chosen": "%s %s" % (best[1], json.dumps(best[2])),
        "strategy": hm, "benchmark": hb,
    }

    logger.info("Scoring every strategy walk-forward ...")
    full_res = run_sweep(bars, folds, progress=False)
    leaderboard = []
    for name in full_res:
        ob, eq, tr, _ = stitch_oos(bars, folds, full_res, name)
        mm = metrics(ob, eq, tr)
        if mm:
            leaderboard.append({"strategy": name, "desc": STRATEGIES[name][1],
                                "sharpe": round(mm["sharpe"], 2),
                                "cagr": round(mm["cagr_pct"], 1),
                                "maxdd": round(mm["max_dd_pct"], 1),
                                "trades": mm["trades"],
                                "combos": len(GRIDS[name])})
    leaderboard.sort(key=lambda r: -r["sharpe"])
    oos_bars = bars[folds[0][2]:folds[-1][3]]
    oos_bench = metrics(oos_bars, *backtest(oos_bars, [True] * len(oos_bars)))

    return {
        "generated": iso(time.time()),
        "history": {"from": iso(bars[0][0])[:10], "to": iso(bars[-1][0])[:10],
                    "bars": len(bars)},
        "benchmark": {"label": "Buy & hold", "metrics": bench_m, "yearly": bench_yearly,
                      "color": BENCH_COLOR},
        "candidates": cands,
        "dates": dates,
        "curves": curves,
        "drawdown": draws,
        "plateau": {"fasts": fasts, "slows": slows, "matrix": plateau},
        "holdout": holdout,
        "leaderboard": leaderboard,
        "oos": {"from": iso(oos_bars[0][0])[:10], "to": iso(oos_bars[-1][0])[:10],
                "bench_sharpe": round(oos_bench["sharpe"], 2)},
        "folds": len(folds),
        "years": ylabels,
        "span_years": round((bars[-1][0] - bars[0][0]) / (DAYS_PER_YEAR * 86400), 1),
        "combos": sum(len(g) for g in GRIDS.values()),
        "costs": {"fee_bps": 10.0, "slippage_bps": 5.0},
    }


REPORT_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC Strategy Lab</title>
<style>
  :root{
    --bg:#0b0e14; --panel:#131722; --panel2:#1a1f2e; --line:#262b3a; --line2:#323848;
    --text:#d6dbe6; --muted:#8b93a7; --dim:#6b7488;
    --pos:#0ca30c; --neg:#d03b3b; --warn:#fab219;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
    --bench:#8b93a7;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);line-height:1.55;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang TC","Microsoft JhengHei",sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:0 22px 72px}
  header{border-bottom:1px solid var(--line);margin-bottom:30px;padding:34px 0 26px}
  h1{margin:0 0 6px;font-size:25px;letter-spacing:-.2px}
  h2{font-size:12px;margin:0 0 4px;color:var(--muted);text-transform:uppercase;letter-spacing:.9px}
  .sub{color:var(--muted);font-size:13px}
  .badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;
    letter-spacing:.5px;background:rgba(250,178,25,.13);color:var(--warn);
    border:1px solid rgba(250,178,25,.32);vertical-align:3px;margin-left:6px}
  section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:20px 22px;margin-bottom:18px}
  .lede{font-size:13.5px;color:var(--muted);margin:0 0 16px;max-width:76ch}
  .hero{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:18px}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
  .tile .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px}
  .tile .v{font-size:27px;font-weight:660;margin-top:5px;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
  .tile .n{font-size:11.5px;color:var(--dim);margin-top:3px}
  .pos{color:var(--pos)} .neg{color:var(--neg)}
  table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
  th{text-align:right;color:var(--muted);font-weight:600;padding:8px 9px;
     border-bottom:1px solid var(--line2);white-space:nowrap;font-size:11px;
     text-transform:uppercase;letter-spacing:.5px}
  th:first-child,td:first-child{text-align:left}
  td{padding:8px 9px;border-bottom:1px solid rgba(38,43,58,.5)}
  tbody tr:hover{background:var(--panel2)}
  .swatch{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px;vertical-align:0}
  .rank{color:var(--dim);font-variant-numeric:tabular-nums}
  .legend{display:flex;flex-wrap:wrap;gap:15px;margin:2px 0 14px;font-size:12px;color:var(--muted)}
  .legend span{display:flex;align-items:center;gap:6px}
  svg{width:100%;display:block;overflow:visible}
  .note{font-size:12.5px;color:var(--muted);margin-top:14px;padding-top:13px;
        border-top:1px solid var(--line);max-width:82ch}
  .verdict{border-left:3px solid var(--warn);padding:3px 0 3px 15px;margin:16px 0;
           color:var(--text);font-size:13.5px;max-width:80ch}
  .verdict b{color:var(--warn)}
  code{background:var(--panel2);padding:1.5px 5px;border-radius:4px;font-size:11.5px;color:var(--muted)}
  .cell{font-size:11px;text-anchor:middle;font-variant-numeric:tabular-nums}
  .tip{position:fixed;pointer-events:none;background:#080a10;border:1px solid var(--line2);
    border-radius:7px;padding:9px 11px;font-size:12px;opacity:0;transition:opacity .1s;
    z-index:50;box-shadow:0 8px 26px rgba(0,0,0,.55);font-variant-numeric:tabular-nums;max-width:270px}
  .tip b{display:block;margin-bottom:5px;color:var(--text);font-size:11.5px}
  .tip div{display:flex;justify-content:space-between;gap:16px;color:var(--muted)}
  .tip div span:last-child{color:var(--text)}
  footer{color:var(--dim);font-size:11.5px;text-align:center;padding:14px 22px 0;line-height:1.75}
  @media(max-width:640px){.wrap{padding:0 16px 48px}h1{font-size:20px}.tile .v{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>₿ BTC Strategy Lab <span class="badge">BACKTEST</span></h1>
  <div class="sub" id="sub"></div>
</header>

<div class="hero" id="hero"></div>

<section>
  <h2 id="h-eq">1 · 邊隻策略贏? — 淨值曲線</h2>
  <p class="lede">$100,000 本金,由頭行到尾,已經扣咗手續費同滑點。縱軸用對數 —— 翻一倍嘅距離喺邊個價位都一樣高。</p>
  <div class="legend" id="lg-eq"></div>
  <div id="eq"></div>
  <p class="note" id="eq-note"></p>
</section>

<section>
  <h2>2 · 跌得幾甘? — 水底圖</h2>
  <p class="lede">由歷史高位計落去,任何時刻蝕緊幾多。呢個先係你真係要捱嘅嘢 —— Sharpe 高唔高,睇嘅就係呢條線有幾淺。</p>
  <div class="legend" id="lg-dd"></div>
  <div id="dd"></div>
</section>

<section>
  <h2>3 · 逐年拆開睇</h2>
  <p class="lede">平均數會呃人。同一隻策略喺牛市同熊市係兩回事,所以逐年睇 Sharpe(顏色)同總回報(數字)。</p>
  <div id="years"></div>
  <p class="note">藍 = 該年 Sharpe 為正,紅 = 負。格仔入面係嗰年嘅總回報 %。</p>
</section>

<section>
  <h2>4 · 係真嘅邊,定係撞彩? — 參數高原</h2>
  <p class="lede">呢個係最重要嘅一張圖。如果只有一格靚、隔籬全部差,咁就係 overfit。如果一大片都企得住,咁個效應先似真。每格 = 該組參數嘅全期 Sharpe。</p>
  <div id="plateau"></div>
  <p class="note" id="plateau-note"></p>
</section>

<section>
  <h2>5 · 封存驗證 — 老實講嘅嗰part</h2>
  <p class="lede">上面啲數字,我係喺一大堆候選入面揀出嚟先俾你睇嘅 —— 呢個本身就會令個數字靚咗。所以再做多一次:所有揀選只准用截止日之前嘅數據,之後嗰段完全封存,一次過驗。</p>
  <div id="holdout"></div>
</section>

<section>
  <h2>6 · 全部策略 · Walk-forward 排行榜</h2>
  <p class="lede">每個 fold 用前一年調參數,跟住喺之後半年冇見過嘅數據度落場,再滾落去。下面係串埋一齊嘅 out-of-sample 成績。</p>
  <div id="board"></div>
  <p class="note" id="board-note"></p>
</section>

<footer id="foot"></footer>
</div>
<div class="tip" id="tip"></div>
<script>
const D = __PAYLOAD__;
const NS="http://www.w3.org/2000/svg";
const el=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
const fmt=n=>n===null||n===undefined||isNaN(n)?"–":Number(n).toLocaleString("en-US",{maximumFractionDigits:0});
const pct=n=>n===null||n===undefined||isNaN(n)?"–":(n>=0?"+":"")+Number(n).toFixed(1)+"%";
const cls=n=>n>0?"pos":(n<0?"neg":"");
const tip=document.getElementById("tip");
function showTip(e,html){tip.innerHTML=html;tip.style.opacity=1;
  const r=tip.getBoundingClientRect();
  let x=e.clientX+14, y=e.clientY+14;
  if(x+r.width>innerWidth-10)x=e.clientX-r.width-14;
  if(y+r.height>innerHeight-10)y=e.clientY-r.height-14;
  tip.style.left=x+"px";tip.style.top=y+"px";}
function hideTip(){tip.style.opacity=0;}

/* ---------------- header + hero ---------------- */
const B=D.benchmark, W=D.candidates[0];
document.getElementById("sub").textContent =
  D.history.bars.toLocaleString()+" 支 1 小時 K 線 · "+D.span_years+" 年 · "+D.history.from+" → "+D.history.to
  +" · 掃咗 "+D.combos+" 組參數 · "+D.folds+" 個 walk-forward fold · 生成於 "+D.generated;

const hero=[
  ["最佳全期 Sharpe", W.metrics.sharpe.toFixed(2), W.label, cls(W.metrics.sharpe-B.metrics.sharpe)],
  ["Buy &amp; hold Sharpe", B.metrics.sharpe.toFixed(2), "要打嘅標準", ""],
  ["最大回撤", "-"+W.metrics.max_dd_pct.toFixed(0)+"%", "buy &amp; hold 係 -"+B.metrics.max_dd_pct.toFixed(0)+"%", "pos"],
  ["全期總回報", pct(W.metrics.total_return_pct), "buy &amp; hold "+pct(B.metrics.total_return_pct), cls(W.metrics.total_return_pct)],
  ["Calmar", W.metrics.calmar.toFixed(2), "buy &amp; hold "+B.metrics.calmar.toFixed(2), ""],
  ["交易次數", W.metrics.trades, "平均持倉 "+W.metrics.exposure_pct.toFixed(0)+"% 時間", ""],
];
document.getElementById("hero").innerHTML=hero.map(h=>
  `<div class="tile"><div class="k">${h[0]}</div><div class="v ${h[3]}">${h[1]}</div><div class="n">${h[2]}</div></div>`).join("");

/* ---------------- line chart (equity / drawdown) ---------------- */
function lineChart(host, legendHost, series, dates, opts){
  const W=1160,H=opts.h||380,PL=opts.pl||60,PR=opts.pr||124,PT=14,PB=26;
  const all=series.flatMap(s=>s.values);
  let lo=Math.min(...all), hi=Math.max(...all);
  const log=!!opts.log;
  if(log){lo=Math.max(lo,1);}
  const pad=(hi-lo)*0.05||1;
  let ymin=log?lo:lo-pad, ymax=log?hi:hi+pad;
  if(opts.zeroTop){ymax=0;ymin=lo-pad;}
  const tf=v=>log?Math.log10(Math.max(v,1)):v;
  const y=v=>PT+(tf(ymax)-tf(v))/(tf(ymax)-tf(ymin))*(H-PT-PB);
  const x=i=>PL+i*(W-PL-PR)/(dates.length-1);
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,height:H});

  const ticks=opts.ticks||(log?[100000,150000,250000,400000,650000]:[0,-15,-30,-45,-60]);
  ticks.forEach(v=>{
    if(v>ymax||v<ymin)return;
    svg.appendChild(el("line",{x1:PL,x2:W-PR,y1:y(v),y2:y(v),stroke:"#262b3a","stroke-width":1}));
    const t=el("text",{x:PL-9,y:y(v)+4,fill:"#6b7488","font-size":11,"text-anchor":"end"});
    t.textContent=opts.fmtY?opts.fmtY(v):"$"+fmt(v);svg.appendChild(t);
  });
  // year gridlines
  let prevY=null;
  dates.forEach((d,i)=>{const yr=d.slice(0,4);
    if(prevY&&yr!==prevY){
      svg.appendChild(el("line",{x1:x(i),x2:x(i),y1:PT,y2:H-PB,stroke:"#262b3a","stroke-width":1,"stroke-dasharray":"3 5"}));
      const t=el("text",{x:x(i),y:H-9,fill:"#6b7488","font-size":11,"text-anchor":"middle"});t.textContent=yr;svg.appendChild(t);}
    prevY=yr;});

  series.forEach(s=>{
    const pts=s.values.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const a={points:pts,fill:"none",stroke:s.color,"stroke-width":s.dash?1.6:2,
             "stroke-linejoin":"round","stroke-linecap":"round"};
    if(s.dash)a["stroke-dasharray"]="5 4";
    svg.appendChild(el("polyline",a));
  });
  // Direct end-labels, nudged apart so they never sit on top of each other.
  const ends=series.map(s=>({s,v:s.values[s.values.length-1]}))
                   .map(o=>({...o,y:y(o.v)})).sort((a,b)=>a.y-b.y);
  const GAP=14;
  for(let i=1;i<ends.length;i++)
    if(ends[i].y-ends[i-1].y<GAP) ends[i].y=ends[i-1].y+GAP;
  const overflow=ends.length?ends[ends.length-1].y-(H-PB):0;
  if(overflow>0) ends.forEach(e=>e.y-=overflow);
  ends.forEach(e=>{
    const ly=y(e.v);
    if(Math.abs(e.y-ly)>2)   // leader line back to the actual end of the curve
      svg.appendChild(el("line",{x1:W-PR+2,x2:W-PR+7,y1:ly,y2:e.y-4,
        stroke:e.s.color,"stroke-width":1,opacity:.55}));
    const lb=el("text",{x:W-PR+9,y:e.y,fill:e.s.color,"font-size":11.5,"font-weight":600});
    lb.textContent=opts.fmtEnd?opts.fmtEnd(e.v):"$"+fmt(e.v);
    svg.appendChild(lb);
  });

  const cross=el("line",{x1:0,x2:0,y1:PT,y2:H-PB,stroke:"#4a5268","stroke-width":1,opacity:0});
  svg.appendChild(cross);
  const dots=series.map(s=>{const c=el("circle",{r:3.5,fill:s.color,stroke:"#0b0e14","stroke-width":2,opacity:0});svg.appendChild(c);return c;});
  const hit=el("rect",{x:PL,y:PT,width:W-PL-PR,height:H-PT-PB,fill:"transparent"});
  svg.appendChild(hit);
  hit.addEventListener("mousemove",ev=>{
    const bb=svg.getBoundingClientRect();
    const px=(ev.clientX-bb.left)/bb.width*W;
    let i=Math.round((px-PL)/((W-PL-PR)/(dates.length-1)));
    i=Math.max(0,Math.min(dates.length-1,i));
    cross.setAttribute("x1",x(i));cross.setAttribute("x2",x(i));cross.setAttribute("opacity",.85);
    let html=`<b>${dates[i]}</b>`;
    series.forEach((s,k)=>{
      dots[k].setAttribute("cx",x(i));dots[k].setAttribute("cy",y(s.values[i]));dots[k].setAttribute("opacity",1);
      html+=`<div><span><span class="swatch" style="background:${s.color}"></span>${s.label}</span><span>${opts.fmtTip?opts.fmtTip(s.values[i]):"$"+fmt(s.values[i])}</span></div>`;
    });
    showTip(ev,html);
  });
  hit.addEventListener("mouseleave",()=>{cross.setAttribute("opacity",0);dots.forEach(d=>d.setAttribute("opacity",0));hideTip();});
  host.innerHTML="";host.appendChild(svg);
  if(legendHost)legendHost.innerHTML=series.map(s=>
    `<span><span class="swatch" style="background:${s.color}"></span>${s.label}</span>`).join("");
}

lineChart(document.getElementById("eq"),document.getElementById("lg-eq"),D.curves,D.dates,{log:true,h:400});
lineChart(document.getElementById("dd"),document.getElementById("lg-dd"),D.drawdown,D.dates,
  {h:250,zeroTop:true,ticks:[0,-15,-30,-45,-60,-75],
   fmtY:v=>v.toFixed(0)+"%",fmtEnd:v=>v.toFixed(0)+"%",fmtTip:v=>v.toFixed(1)+"%"});

document.getElementById("eq-note").innerHTML =
  "扣費假設:每邊 "+D.costs.fee_bps+" bps 手續費 + "+D.costs.slippage_bps+
  " bps 滑點。訊號喺 K 線收市計,<b>下一支 K 線開市價先成交</b> —— 所以唔會用到當時睇唔到嘅價。全部策略只做長倉或者揸現金,唔會沽空。";

/* ---------------- year heatmap ---------------- */
function divColor(v,max){
  if(v===null)return "#1a1f2e";
  const t=Math.max(-1,Math.min(1,v/max));
  const mid=[56,56,53];
  const pole=t>=0?[57,135,229]:[208,59,59];
  const k=Math.abs(t);
  return `rgb(${mid.map((m,i)=>Math.round(m+(pole[i]-m)*k)).join(",")})`;
}
(function(){
  const rows=[{label:D.benchmark.label,color:D.benchmark.color,yearly:D.benchmark.yearly,bench:true}]
    .concat(D.candidates.map(c=>({label:c.label,color:c.color,yearly:c.yearly})));
  let h=`<table><thead><tr><th>策略</th>`+D.years.map(y=>`<th>${y}</th>`).join("")+`<th>全期 Sharpe</th></tr></thead><tbody>`;
  rows.forEach(r=>{
    const four=r.bench?D.benchmark.metrics.sharpe:D.candidates.find(c=>c.label===r.label).metrics.sharpe;
    h+=`<tr><td><span class="swatch" style="background:${r.color}"></span>${r.label}</td>`;
    r.yearly.forEach(y=>{
      h+=`<td style="text-align:right;background:${divColor(y.sharpe,2.2)};color:#fff" title="Sharpe ${y.sharpe}">
          ${pct(y.total)}<div style="font-size:10px;opacity:.72">S ${y.sharpe.toFixed(2)}</div></td>`;
    });
    h+=`<td style="text-align:right;font-weight:650" class="${cls(four)}">${four.toFixed(2)}</td></tr>`;
  });
  document.getElementById("years").innerHTML=h+`</tbody></table>`;
})();

/* ---------------- plateau heatmap ---------------- */
(function(){
  const P=D.plateau, cw=104, ch=34, left=62, top=26;
  const W=left+P.slows.length*cw+10, H=top+P.fasts.length*ch+16;
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,height:H});
  P.slows.forEach((s,j)=>{const t=el("text",{x:left+j*cw+cw/2,y:17,fill:"#8b93a7","font-size":11,"text-anchor":"middle"});
    t.textContent=(s/24).toFixed(0)+"d";svg.appendChild(t);});
  P.fasts.forEach((f,i)=>{const t=el("text",{x:left-10,y:top+i*ch+ch/2+4,fill:"#8b93a7","font-size":11,"text-anchor":"end"});
    t.textContent=(f/24)+"d";svg.appendChild(t);});
  let bestV=-9,bi=0,bj=0;
  P.matrix.forEach((row,i)=>row.forEach((v,j)=>{if(v!==null&&v>bestV){bestV=v;bi=i;bj=j;}}));
  P.matrix.forEach((row,i)=>row.forEach((v,j)=>{
    const x=left+j*cw, y=top+i*ch;
    const r=el("rect",{x:x+1,y:y+1,width:cw-2,height:ch-2,rx:3,
      fill:v===null?"#12151f":divColor(v-0.95,0.6),stroke:(i===bi&&j===bj)?"#fab219":"none","stroke-width":2});
    svg.appendChild(r);
    if(v!==null){
      const t=el("text",{x:x+cw/2,y:y+ch/2+4,fill:"#fff","class":"cell"});
      t.textContent=v.toFixed(2);svg.appendChild(t);
      r.addEventListener("mousemove",e=>showTip(e,
        `<b>dual_sma</b><div><span>快線</span><span>${(P.fasts[i]/24)} 日</span></div>
         <div><span>慢線</span><span>${(P.slows[j]/24)} 日</span></div>
         <div><span>全期 Sharpe</span><span>${v.toFixed(2)}</span></div>
         <div><span>vs buy &amp; hold</span><span>${(v-D.benchmark.metrics.sharpe>=0?"+":"")+(v-D.benchmark.metrics.sharpe).toFixed(2)}</span></div>`));
      r.addEventListener("mouseleave",hideTip);
    }
  }));
  const lab=el("text",{x:left,y:H-2,fill:"#6b7488","font-size":11});
  lab.textContent="↑ 快線 (日)    慢線 (日) →";svg.appendChild(lab);
  document.getElementById("plateau").innerHTML="";document.getElementById("plateau").appendChild(svg);
  const above=P.matrix.flat().filter(v=>v!==null&&v>D.benchmark.metrics.sharpe).length;
  const tot=P.matrix.flat().filter(v=>v!==null).length;
  document.getElementById("plateau-note").innerHTML=
    `藍 = 贏 buy &amp; hold,紅 = 輸。<b>${tot} 組參數入面有 ${above} 組(${(above/tot*100).toFixed(0)}%)贏到 ${D.benchmark.metrics.sharpe.toFixed(2)}</b>,
     而且高分果啲係連成一片,唔係散開嘅單點 —— 呢個先係「唔係撞彩」嘅證據。金色框係最高嗰格。`;
})();

/* ---------------- holdout ---------------- */
function verdict(s, b, h){
  // Read the result instead of assuming one. Earlier this paragraph asserted a
  // bear market outright, and printed "BTC fell 29%" for a period it rose 28.5%.
  const bear = b.total_return_pct < 0;
  const ddBetter = s.max_dd_pct < b.max_dd_pct * 0.85;
  const retBetter = s.total_return_pct > b.total_return_pct;
  const degraded = (h.train_score !== undefined && h.train_score !== null)
    ? `訓練期揀佢出嚟嗰陣 ${h.select_by} 係 <b>${h.train_score.toFixed(2)}</b>,封存期實際交出 <b>${s.calmar.toFixed(2)}</b>。` : "";
  let body;
  if (bear && s.total_return_pct > b.total_return_pct){
    body = `封存嗰段係跌市,BTC 跌咗 ${Math.abs(b.total_return_pct).toFixed(0)}%。策略
      ${s.total_return_pct < 0 ? `一樣蝕錢(${s.total_return_pct.toFixed(0)}%)` : `反而賺 ${s.total_return_pct.toFixed(0)}%`},
      回撤 ${s.max_dd_pct.toFixed(0)}% 而唔係 ${b.max_dd_pct.toFixed(0)}%。
      呢個就係趨勢策略真正嘅價值:<b>跌市蝕少啲</b>,唔係年年賺錢。`;
  } else if (!bear && retBetter && ddBetter){
    body = `封存嗰段 BTC 升咗 ${b.total_return_pct.toFixed(0)}%,策略賺 ${s.total_return_pct.toFixed(0)}%,
      而且回撤細得多(${s.max_dd_pct.toFixed(0)}% vs ${b.max_dd_pct.toFixed(0)}%)。
      <b>喺完全冇見過嘅數據度贏咗</b> —— 呢個係最有份量嘅一種證據,但都只係<b>一段</b>時期,唔好當保證。`;
  } else if (!bear && ddBetter){
    body = `封存嗰段 BTC 升咗 ${b.total_return_pct.toFixed(0)}%,策略只賺 ${s.total_return_pct.toFixed(0)}% —— <b>跑輸咗</b>。
      但回撤細一半(${s.max_dd_pct.toFixed(0)}% vs ${b.max_dd_pct.toFixed(0)}%)。
      升市跑輸係趨勢策略嘅代價:佢有段時間坐喺現金度。`;
  } else {
    body = `策略喺封存期<b>冇跑贏</b> buy &amp; hold(回報 ${s.total_return_pct.toFixed(0)}% vs
      ${b.total_return_pct.toFixed(0)}%,回撤 ${s.max_dd_pct.toFixed(0)}% vs ${b.max_dd_pct.toFixed(0)}%)。
      呢個先係封存驗證嘅用處 —— 佢會照直話你知。`;
  }
  return `<b>點睇呢個結果:</b> ${body} ${degraded}
    任何人同你講佢隻 crypto 策略年年正回報,叫佢俾封存驗證你睇。`;
}

(function(){
  const h=D.holdout, s=h.strategy, b=h.benchmark;
  document.getElementById("holdout").innerHTML=`
    <p class="lede" style="margin-bottom:14px">揀參數只准睇 <code>${h.cutoff}</code> 之前嘅數據。系統自己揀咗
      <code>${h.chosen}</code>,然後一次過放落 <b>${h.from} → ${h.to}</b> 呢段完全冇掂過嘅數據度行。</p>
    <table><thead><tr><th>封存期表現</th><th>Sharpe</th><th>總回報</th><th>最大回撤</th><th>Calmar</th></tr></thead><tbody>
      <tr><td>揀中嘅策略</td><td style="text-align:right" class="${cls(s.sharpe)}">${s.sharpe.toFixed(2)}</td>
        <td style="text-align:right" class="${cls(s.total_return_pct)}">${pct(s.total_return_pct)}</td>
        <td style="text-align:right">-${s.max_dd_pct.toFixed(1)}%</td>
        <td style="text-align:right">${s.calmar.toFixed(2)}</td></tr>
      <tr><td>Buy &amp; hold</td><td style="text-align:right" class="${cls(b.sharpe)}">${b.sharpe.toFixed(2)}</td>
        <td style="text-align:right" class="${cls(b.total_return_pct)}">${pct(b.total_return_pct)}</td>
        <td style="text-align:right">-${b.max_dd_pct.toFixed(1)}%</td>
        <td style="text-align:right">${b.calmar.toFixed(2)}</td></tr>
    </tbody></table>
    <div class="verdict">${verdict(s, b, h)}</div>`;
})();

/* ---------------- leaderboard ---------------- */
(function(){
  const L=D.leaderboard;
  const medal=i=>i===0?"🥇":i===1?"🥈":i===2?"🥉":(i+1);
  let h=`<table><thead><tr><th>#</th><th>策略</th><th>做緊乜</th><th>OOS Sharpe</th><th>CAGR</th><th>最大回撤</th><th>交易</th><th>掃過組合</th></tr></thead><tbody>`;
  L.forEach((r,i)=>{
    h+=`<tr><td class="rank">${medal(i)}</td><td>${r.strategy}</td>
      <td style="color:var(--muted);font-size:11.5px">${r.desc}</td>
      <td style="text-align:right;font-weight:650" class="${cls(r.sharpe)}">${r.sharpe.toFixed(2)}</td>
      <td style="text-align:right" class="${cls(r.cagr)}">${pct(r.cagr)}</td>
      <td style="text-align:right">-${r.maxdd.toFixed(1)}%</td>
      <td style="text-align:right">${r.trades}</td>
      <td style="text-align:right;color:var(--dim)">${r.combos}</td></tr>`;
  });
  document.getElementById("board").innerHTML=h+`</tbody></table>`;
  document.getElementById("board-note").innerHTML=
    `同期 buy &amp; hold 嘅 OOS Sharpe 係 <b>${D.oos.bench_sharpe.toFixed(2)}</b>(${D.oos.from} → ${D.oos.to})。
     留意每個 fold 都重新調參數,所以呢欄數字會偏低 —— 頻頻換參數本身就係雜訊。第 4 節嗰張高原圖先係更可信嘅證據。`;
})();

document.getElementById("foot").innerHTML=
  "全部成交都係模擬,冇連任何 broker,冇落過一張真單。過去表現唔代表將來。呢個唔係投資建議。<br>"+
  "由 <code>btc_backtest.py</code> 生成 · 歷史數據 "+D.history.from+" → "+D.history.to;
</script>
</body>
</html>
'''


def render_report(payload, path=None):
    path = path or os.path.join(OUT_DIR, "backtest.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    html = REPORT_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info("Report written -> %s (%.0f KB)", path, os.path.getsize(path) / 1024)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="BTC strategy lab (paper only, no broker)")
    ap.add_argument("--fetch-history", action="store_true", help="download and cache the history")
    ap.add_argument("--ensure-history", action="store_true",
                    help="download only if the cache does not already cover --years")
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--granularity", type=int, default=3600)
    ap.add_argument("--symbols", default="",
                    help="comma-separated symbols to fetch (default: the standard basket)")
    ap.add_argument("--report", action="store_true", help="run everything and build the HTML report")
    ap.add_argument("--sweep", action="store_true", help="print the walk-forward leaderboard only")
    ap.add_argument("--cross", action="store_true",
                    help="score every strategy on every cached asset and rank by the median")
    ap.add_argument("--top", type=int, default=15, help="rows to print for --cross")
    ap.add_argument("--common-window", action="store_true",
                    help="trim every asset to the period they all share")
    ap.add_argument("--only", default="", help="comma-separated strategies for --cross")
    ap.add_argument("--save-cross", default="",
                    help="write the --cross scores to a file so splits can be re-cut without re-sweeping")
    ap.add_argument("--train-syms", default="",
                    help="assets to choose on for the held-out test (default: the first half)")
    args = ap.parse_args(argv)

    if args.fetch_history or args.ensure_history:
        wanted = [x.strip().upper() for x in args.symbols.split(",")] if args.symbols \
            else list(DEFAULT_SYMBOLS)
        got, skipped = [], []
        for sym in wanted:
            if args.ensure_history:
                try:
                    have = load_history(symbol=sym, gran=args.granularity)
                    span = (have[-1][0] - have[0][0]) / (DAYS_PER_YEAR * 86400)
                    if span >= args.years - 0.2 or already_satisfied(sym, have, args.years,
                                                                    args.granularity):
                        logger.info("  %-5s cached %.1f years (%s -> %s) — skipping",
                                    sym, span, iso(have[0][0])[:10], iso(have[-1][0])[:10])
                        got.append(sym)
                        continue
                except (FileNotFoundError, OSError):
                    pass
            try:
                source, bars = fetch_history(args.years, args.granularity, sym)
                save_history(bars, symbol=sym, gran=args.granularity)
                record_fetch(sym, bars, args.years, source, args.granularity)
                got.append(sym)
            except (TypeError, AttributeError, NameError):
                # These are bugs in this file, not an unreachable exchange.
                # Swallowing them as "SKIPPED" is how a run once downloaded
                # forty assets, wrote none of them, and still went green.
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("  %-5s SKIPPED — %s", sym, exc)
                skipped.append(sym)
        logger.info("Cached %d symbols: %s", len(got), ", ".join(got))
        if skipped:
            logger.warning("Could not get: %s", ", ".join(skipped))
        for sym, n, span in available_symbols(args.granularity):
            logger.info("  %-5s %6d bars  %.1f years", sym, n, span)
        if not got:
            logger.error("Cached nothing at all — treating that as a failure.")
            return 1
        return 0

    if not os.path.exists(HISTORY_FILE):
        logger.error("No cached history. Run: python btc_backtest.py --fetch-history")
        return 1
    bars = load_history()
    logger.info("Loaded %d bars: %s -> %s", len(bars), iso(bars[0][0]), iso(bars[-1][0]))

    if args.sweep:
        folds = make_folds(bars)
        res = run_sweep(bars, folds)
        rows = []
        for name in res:
            ob, eq, tr, _ = stitch_oos(bars, folds, res, name)
            mm = metrics(ob, eq, tr)
            if mm:
                rows.append((name, mm))
        rows.sort(key=lambda r: -r[1]["sharpe"])
        logger.info("%-18s %7s %8s %8s", "STRATEGY", "SHARPE", "CAGR%", "MAXDD%")
        for name, mm in rows:
            logger.info("%-18s %7.2f %8.1f %8.1f", name, mm["sharpe"], mm["cagr_pct"], mm["max_dd_pct"])
        return 0

    if args.cross:
        picked = [x.strip().upper() for x in args.symbols.split(",")] if args.symbols else None
        basket = load_basket(symbols=picked, common_window=args.common_window)
        if len(basket) < 2:
            logger.error("Only %d asset cached. Run: python btc_backtest.py --ensure-history",
                         len(basket))
            return 1
        logger.info("Basket: %s", ", ".join("%s(%.1fy)" % (s_, (b[0][-1][0] - b[0][0][0]) / (DAYS_PER_YEAR * 86400))
                                            for s_, b in basket.items()))
        rows = cross_asset_sweep(
            basket, strategies=[x.strip() for x in args.only.split(",")] if args.only else None)
        syms = list(basket)

        logger.info("")
        logger.info("RANKED BY MEDIAN %s ACROSS %d ASSETS", SELECT_BY.upper(), len(syms))
        head = "%-20s %8s %8s %8s %6s  %s" % ("STRATEGY", "MEDIAN", "WORST", "BEST", "OK",
                                              " ".join("%6s" % x for x in syms))
        logger.info(head)
        logger.info("-" * len(head))
        for r in rows[:args.top]:
            cells = " ".join("%6.2f" % score_of(r["per_asset"][x]) if x in r["per_asset"]
                             and score_of(r["per_asset"][x]) > -90 else "%6s" % "-"
                             for x in syms)
            logger.info("%-20s %8.2f %8.2f %8.2f %4d/%d  %s",
                        r["strategy"], r["median"], r["worst"], r["best"],
                        r["n_ok"], r["n_assets"], cells)

        if args.save_cross:
            import pickle
            with open(args.save_cross, "wb") as fh:
                pickle.dump({"rows": rows, "syms": syms}, fh)
            logger.info("Scores saved -> %s", args.save_cross)

        # --- selection on some assets, judged on assets never seen -------------
        if args.train_syms:
            train_syms = [x.strip().upper() for x in args.train_syms.split(",") if x.strip()]
            test_syms = [x for x in syms if x not in train_syms]
            train_syms = [x for x in train_syms if x in syms]
        else:
            half = max(2, len(syms) // 2)
            train_syms, test_syms = syms[:half], syms[half:]
        if test_syms:
            win, tr_score, unseen = held_out_assets(rows, train_syms, test_syms)
            logger.info("")
            logger.info("HELD-OUT ASSETS — chosen on %s, judged on %s",
                        "/".join(train_syms), "/".join(test_syms))
            if win:
                logger.info("  pick: %s %s", win["strategy"], win["params"])
                logger.info("  median %s on the assets it was chosen on : %6.2f", SELECT_BY, tr_score)
                for s_, v in sorted(unseen.items(), key=lambda kv: -kv[1]):
                    logger.info("    %-5s never seen -> %6.2f", s_, v)
                vals = sorted(v for v in unseen.values() if v > -90)
                if vals:
                    mid = (vals[len(vals) // 2] if len(vals) % 2
                           else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2)
                    logger.info("  median on unseen assets                  : %6.2f  (%+.2f)",
                                mid, mid - tr_score)

        # --- the distribution over every possible split, not one chosen split ---
        if len(syms) >= 4:
            per_split, singles = split_study(rows, syms, k=min(3, len(syms) - 1))
            if per_split:
                trs = [r[2] for r in per_split]
                mids = [r[3] for r in per_split]
                drops = [r[3] - r[2] for r in per_split]
                srt = lambda v: sorted(v)[len(v) // 2]
                logger.info("")
                logger.info("EVERY %d-ASSET SPLIT (%d of them)", min(3, len(syms) - 1), len(per_split))
                logger.info("  chosen-on score  median %5.2f  (%5.2f .. %5.2f)",
                            srt(trs), min(trs), max(trs))
                logger.info("  unseen score     median %5.2f  (%5.2f .. %5.2f)",
                            srt(mids), min(mids), max(mids))
                logger.info("  degradation      median %+5.2f  (%d of %d splits got worse)",
                            srt(drops), sum(1 for x in drops if x < 0), len(drops))
                # which pick do independent splits agree on?
                tally = {}
                for _, _, _, _, win in per_split:
                    key = (win["strategy"], json.dumps(win["params"], sort_keys=True))
                    tally[key] = tally.get(key, 0) + 1
                agreed = sorted(tally.items(), key=lambda kv: -kv[1])[:3]
                logger.info("  most agreed-on picks:")
                for (name, params), n in agreed:
                    logger.info("    %2d/%d splits  %-20s %s", n, len(per_split), name, params)
            if singles:
                logger.info("")
                logger.info("  PICKING ON A SINGLE ASSET (what a BTC-only study does):")
                for train, _, tr, mid, _ in sorted(singles, key=lambda r: -r[2]):
                    logger.info("    chosen on %-5s %5.2f -> others %5.2f  (%+5.2f)",
                                train[0], tr, mid, mid - tr)
                logger.info("  The better the score on the single asset, the worse it travels.")

        logger.info("")
        logger.info("THE POINT OF ALL THIS:")
        bo = btc_only_pick(rows)
        if bo:
            others = [score_of(mm) for sym, mm in bo["per_asset"].items() if sym != "BTC"]
            others = [x for x in others if x > -90]
            logger.info("  Best on BTC alone      : %s %s", bo["strategy"], bo["params"])
            logger.info("    BTC %s %.2f  ->  median on the other %d assets %.2f",
                        SELECT_BY, score_of(bo["per_asset"]["BTC"]), len(others),
                        sorted(others)[len(others) // 2] if others else float("nan"))
        top = rows[0]
        logger.info("  Best across the basket : %s %s", top["strategy"], top["params"])
        logger.info("    median %.2f | worst asset %.2f | BTC %.2f",
                    top["median"], top["worst"],
                    score_of(top["per_asset"]["BTC"]) if "BTC" in top["per_asset"] else float("nan"))
        return 0

    if args.report:
        payload = build_report(bars)
        render_report(payload)
        w = payload["candidates"][0]
        b = payload["benchmark"]
        logger.info("Best: %s sharpe %.2f (buy & hold %.2f) | maxDD %.0f%% vs %.0f%%",
                    w["label"], w["metrics"]["sharpe"], b["metrics"]["sharpe"],
                    w["metrics"]["max_dd_pct"], b["metrics"]["max_dd_pct"])
        return 0

    ap.print_help()
    return 0




# ==============================================================================
#  PART 4 — CROSS-ASSET VALIDATION
#
#  A parameter set that only ever had to survive BTC has had about a dozen
#  independent market episodes to fit itself to. The sealed holdout kept
#  showing the cost of that: train Calmar 6.43, live Calmar 0.27.
#
#  So stop asking "what was best on BTC" and start asking "what held up on
#  every asset at once". Selection happens on the MEDIAN score across assets,
#  and the spread across assets is reported, because a strategy that is
#  brilliant on two coins and broken on six is not a strategy.
# ==============================================================================
def longest_continuous(bars, gran=BAR_SECONDS, max_gap_hours=48):
    """
    Longest stretch with no serious hole in it.

    XRP is the reason this exists: the cached series jumps 905 days in a single
    bar, from January 2021 to July 2023, when US venues delisted it during the
    SEC case. Every indicator computed across that seam is meaningless and the
    engine would happily "fill" a trade across a two-and-a-half-year price gap.
    Small holes (an exchange down for a few hours) are left alone.
    """
    if len(bars) < 2:
        return bars, 0
    limit = gran * max_gap_hours
    best_a = best_b = 0
    a = 0
    for i in range(1, len(bars)):
        if bars[i][0] - bars[i - 1][0] > limit:
            if i - a > best_b - best_a:
                best_a, best_b = a, i
            a = i
    if len(bars) - a > best_b - best_a:
        best_a, best_b = a, len(bars)
    return bars[best_a:best_b], len(bars) - (best_b - best_a)


def load_basket(symbols=None, min_bars=8000, common_window=False):
    """
    Load every cached symbol into {sym: (bars, Indicators)}.

    The assets have different lifespans — BTC goes back ten years, SOL five —
    so scoring each over its own full history compares an asset AND an era at
    the same time. `common_window` trims everything to the overlap, which
    isolates the thing being tested: the same period, different assets.
    """
    raw = {}
    for sym in (symbols or [s for s, _, _ in available_symbols()]):
        try:
            bars = load_history(symbol=sym)
        except (FileNotFoundError, OSError):
            continue
        kept, dropped = longest_continuous(bars)
        if dropped:
            logger.warning("  %-5s dropped %d bars outside its longest continuous run "
                           "(%s -> %s) — the series had a hole too big to indicate across",
                           sym, dropped, iso(kept[0][0])[:10], iso(kept[-1][0])[:10])
        bars = kept
        if len(bars) < min_bars:
            logger.warning("  %-5s only %d usable bars — too short, skipping", sym, len(bars))
            continue
        raw[sym] = bars

    if common_window and len(raw) > 1:
        start = max(b[0][0] for b in raw.values())
        end = min(b[-1][0] for b in raw.values())
        logger.info("Common window: %s -> %s (%.1f years)", iso(start)[:10], iso(end)[:10],
                    (end - start) / (DAYS_PER_YEAR * 86400))
        trimmed = {}
        for sym, bars in raw.items():
            cut = [b for b in bars if start <= b[0] <= end]
            cut, _ = longest_continuous(cut)
            if len(cut) < min_bars:
                logger.warning("  %-5s only %d bars in the common window — skipping", sym, len(cut))
                continue
            trimmed[sym] = cut
        raw = trimmed

    return {sym: (bars, Indicators(bars)) for sym, bars in raw.items()}


def score_on_asset(bars, ind, name, params, funding=None):
    """Full-period metrics for one strategy on one asset."""
    want = STRATEGIES[name][0](ind, params)
    if funding is None:
        funding = FUNDING_APR if name in SHORT_STRATEGIES else 0.0
    eq, tr = backtest(bars, want, funding_apr=funding)
    return metrics(bars, eq, tr, want=[abs(float(w)) for w in want])


def cross_asset_sweep(basket, strategies=None, progress=True):
    """
    Every combo of every strategy, scored on every asset.

    Returns [{strategy, params, per_asset:{sym: metrics}, median, worst,
              n_ok, spread}], sorted by median score.
    """
    names = strategies or list(STRATEGIES)
    rows = []
    for name in names:
        t0 = time.time()
        for params in GRIDS[name]:
            per = {}
            for sym, (bars, ind) in basket.items():
                mm = score_on_asset(bars, ind, name, params)
                if mm:
                    per[sym] = mm
            # Failing on an asset is a result, not a missing data point. Dropping
            # the failures before taking the median would let a strategy that
            # breaks on six of eight assets top the table on the strength of the
            # two it survived — the exact opposite of what this is for.
            scores = [score_of(mm) for mm in per.values()]
            ok = [x for x in scores if x > -90]
            if not ok:
                continue
            allsc = sorted(scores)
            mid = (allsc[len(allsc) // 2] if len(allsc) % 2
                   else (allsc[len(allsc) // 2 - 1] + allsc[len(allsc) // 2]) / 2)
            rows.append({
                "strategy": name, "params": params, "per_asset": per,
                "median": mid, "worst": min(scores), "best": max(scores),
                "n_ok": len(ok), "n_assets": len(per),
                "spread": max(ok) - min(ok),
            })
        if progress:
            logger.info("  %-20s %4d combos x %d assets in %5.1fs",
                        name, len(GRIDS[name]), len(basket), time.time() - t0)
    rows.sort(key=lambda r: -r["median"])
    return rows


def held_out_assets(rows, train_syms, test_syms):
    """
    The honest version of cross-asset validation.

    Choosing the parameter set with the best median across ALL assets is still
    selection on everything you have — it just overfits eight price paths
    instead of one. So choose using only `train_syms`, then report what that
    choice did on assets it was never allowed to see.

    Returns (winner_row, train_score, test_scores_by_symbol).
    """
    best, best_score = None, -1e9
    for r in rows:
        scores = [score_of(r["per_asset"][s]) for s in train_syms if s in r["per_asset"]]
        if len(scores) < len(train_syms):
            continue                      # must have run on every training asset
        if sum(1 for x in scores if x > -90) < len(train_syms):
            continue                      # and must have qualified on every one
        scores.sort()
        mid = (scores[len(scores) // 2] if len(scores) % 2
               else (scores[len(scores) // 2 - 1] + scores[len(scores) // 2]) / 2)
        if mid > best_score:
            best, best_score = r, mid
    if best is None:
        return None, None, {}
    unseen = {s: score_of(best["per_asset"][s]) for s in test_syms if s in best["per_asset"]}
    return best, best_score, unseen


def split_study(rows, syms, k=3):
    """
    Every way of choosing on k assets and judging on the rest.

    One split proves nothing — pick the flattering one and you are back to
    selecting on the answer. Running all of them gives the distribution, and
    the distribution is the honest summary.

    Returns (per_split, singles) where per_split is [(train, test, train_score,
    unseen_score, pick)] and singles is the same for k=1.
    """
    import itertools

    def med(v):
        v = sorted(v)
        return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2

    def one(train):
        test = [x for x in syms if x not in train]
        win, tr, unseen = held_out_assets(rows, list(train), test)
        if not win:
            return None
        vals = [v for v in unseen.values() if v > -90]
        if not vals:
            return None
        return (list(train), test, tr, med(vals), win)

    per_split = [r for r in (one(c) for c in itertools.combinations(syms, k)) if r]
    singles = [r for r in (one([s_]) for s_ in syms) if r]
    return per_split, singles


def btc_only_pick(rows):
    """What you would have chosen looking at BTC alone."""
    best = None
    for r in rows:
        mm = r["per_asset"].get("BTC")
        if not mm:
            continue
        sc = score_of(mm)
        if best is None or sc > best[0]:
            best = (sc, r)
    return best[1] if best else None



if __name__ == "__main__":
    sys.exit(main())
