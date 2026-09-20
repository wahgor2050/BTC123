#!/usr/bin/env python3
"""
================================================================================
  Basis carry — the one crypto trade that pays you for a service, not a guess
================================================================================
  Buy spot, short the perpetual future against it. The two legs cancel, so the
  price can do anything; what is left is the funding rate, which longs pay
  shorts whenever the perp trades above spot. It usually does, because retail
  leverage in crypto is overwhelmingly long.

  This repo has been quoting FUNDING_APR = 0.11 as the cost of a short leg
  since the first backtest. That number was a guess. This measures it.

  THE PART EVERY "20% APR" POST LEAVES OUT

  The short leg needs collateral. Delta neutral means perp notional equals
  spot notional, so with capital C at leverage L you can only put

      S = C · L / (L + 1)

  into the position — at 1x that is half your money, and the headline funding
  yield halves with it. Raising L raises the yield and moves the liquidation
  price closer. Both are reported, along with whether that liquidation price
  was ever actually reached.

  Usage:
      python btc_basis.py --fetch      # cache Binance funding history
      python btc_basis.py --study      # run the carry study
================================================================================
"""

import argparse
import gzip
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("btc-basis")

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "btc_demo", "history")
FUNDING_FILE = os.path.join(HISTORY_DIR, "BTC_funding.csv.gz")

DAY = 86400
YEAR = 365.25 * DAY
DEFAULT_PER_DAY = 3             # most venues settle every 8 hours
UA = "btc-paper-demo/1.0 (+github actions; educational basis study)"


# ------------------------------------------------------------------------------
# Fetch
# ------------------------------------------------------------------------------
def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _binance(symbol="BTCUSDT", max_pages=60):
    """8-hourly. Geo-blocks US addresses with a 451, so it rarely answers CI."""
    url = ("https://fapi.binance.com/fapi/v1/fundingRate"
           "?symbol=%s&startTime=%d&limit=1000")
    out, seen, cursor = [], set(), 0
    for _ in range(max_pages):
        page = _get_json(url % (symbol, cursor))
        if not page:
            break
        fresh = 0
        for row in page:
            try:
                t, rate = int(row["fundingTime"]), float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append((t, rate))
            fresh += 1
        if fresh == 0:
            break
        cursor = max(seen) + 1
        time.sleep(0.25)
    return out, 3


def _bybit(symbol="BTCUSDT", max_pages=80):
    """8-hourly. Pages backwards from now."""
    url = ("https://api.bybit.com/v5/market/funding/history"
           "?category=linear&symbol=%s&limit=200&endTime=%d")
    out, seen = [], set()
    cursor = int(time.time() * 1000)
    for _ in range(max_pages):
        doc = _get_json(url % (symbol, cursor))
        page = ((doc.get("result") or {}).get("list")) or []
        if not page:
            break
        fresh = 0
        for row in page:
            try:
                t = int(row["fundingRateTimestamp"])
                rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append((t, rate))
            fresh += 1
        if fresh == 0:
            break
        cursor = min(seen) - 1
        time.sleep(0.25)
    return out, 3


def _kraken(symbol="PF_XBTUSD", max_pages=1):
    """
    Hourly, and the whole history arrives in one response. Kraken is a US
    venue, which is exactly why it is worth having here: the others answer a
    GitHub runner with a geo-block.
    """
    url = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol=%s"
    doc = _get_json(url % symbol)
    out = []
    for row in doc.get("rates") or []:
        try:
            ts = row["timestamp"]
            rate = float(row["relativeFundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            t = int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
        out.append((t, rate))
    return out, 24


def _okx(symbol="BTC-USD-SWAP", max_pages=60):
    """8-hourly. Pages backwards."""
    url = ("https://www.okx.com/api/v5/public/funding-rate-history"
           "?instId=%s&limit=100&before=&after=%d")
    out, seen = [], set()
    cursor = int(time.time() * 1000)
    for _ in range(max_pages):
        doc = _get_json(url % (symbol, cursor))
        page = doc.get("data") or []
        if not page:
            break
        fresh = 0
        for row in page:
            try:
                t, rate = int(row["fundingTime"]), float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append((t, rate))
            fresh += 1
        if fresh == 0:
            break
        cursor = min(seen) - 1
        time.sleep(0.25)
    return out, 3


SOURCES = [("kraken", _kraken, "PF_XBTUSD"),
           ("bybit", _bybit, "BTCUSDT"),
           ("binance", _binance, "BTCUSDT"),
           ("okx", _okx, "BTC-USD-SWAP")]


def fetch_funding(symbol=None, sources=None):
    """
    Whichever venue answers, with how often it settles.

    The settlement interval is not a detail: annualising an hourly rate as if
    it were 8-hourly overstates the yield eightfold, so it is carried with the
    data rather than assumed by whoever reads it later.
    """
    errors, best = [], None
    for name, fn, default_symbol in (sources or SOURCES):
        try:
            rows, per_day = fn(symbol or default_symbol)
            if len(rows) < 100:
                raise RuntimeError("only %d rows" % len(rows))
            rows.sort()
            span = (rows[-1][0] - rows[0][0]) / 1000.0 / YEAR
            logger.info("  %-8s %6d settlements, %2d/day, %.1f years",
                        name, len(rows), per_day, span)
            if best is None or span > best[3]:
                best = (rows, per_day, name, span)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  %-8s 冇料 — %s", name, str(exc)[:70])
            errors.append("%s: %s" % (name, str(exc)[:70]))
    if best is None:
        raise RuntimeError(" | ".join(errors))
    # Longest history wins, not first to answer. Kraken replies to a US runner
    # when the others will not, but only keeps a rolling year; a venue with
    # five years of settlements is worth more than one that answers first.
    logger.info("用 %s(%.1f 年)", best[2], best[3])
    return best[0], best[1], best[2]


def save_funding(rows, per_day=DEFAULT_PER_DAY, source="unknown", path=None):
    """Deterministic, like every other cache here, and it carries its own units."""
    path = path or FUNDING_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = ["# source=%s per_day=%d\n" % (source, per_day),
            "funding_time_ms,rate\n"]
    for t, rate in rows:
        body.append("%d,%.10g\n" % (t, rate))
    raw = "".join(body).encode("utf-8")
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            gz.write(raw)
    logger.info("saved %d settlements (%s, %d/day) -> %s (%.0f KB)",
                len(rows), source, per_day, os.path.basename(path),
                os.path.getsize(path) / 1024)
    return path


def load_funding(path=None):
    """Returns (rows, per_day, source). Older caches without the metadata
    line fall back to the 8-hourly default they were written with."""
    path = path or FUNDING_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    rows, per_day, source = [], DEFAULT_PER_DAY, "unknown"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                for part in line[1:].split():
                    k, _, v = part.partition("=")
                    if k == "per_day" and v.isdigit():
                        per_day = int(v)
                    elif k == "source":
                        source = v
                continue
            if line.startswith("funding_time_ms"):
                continue
            parts = line.split(",")
            if len(parts) != 2:
                continue
            try:
                rows.append((int(parts[0]), float(parts[1])))
            except ValueError:
                continue
    rows.sort()
    return rows, per_day, source


# ------------------------------------------------------------------------------
# The trade
# ------------------------------------------------------------------------------
def _lab():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", os.path.join(HERE, "btc_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BARS_PER_DAY = 1
    return mod


def spot_at(bars, ts_sec):
    """Last daily close at or before a settlement. Never a later one."""
    lo, hi = 0, len(bars) - 1
    if ts_sec < bars[0][0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid][0] <= ts_sec:
            lo = mid
        else:
            hi = mid - 1
    return bars[lo][4]


def carry(rows, bars, leverage=1.0, fee_bps=10.0, slip_bps=5.0,
          maint_margin=0.005, start_cash=100_000.0, rebalance_band=0.05,
          per_day=DEFAULT_PER_DAY):
    """
    Long spot, short perp, same notional. Returns the equity curve and what
    went wrong along the way.

    Capital splits between the spot leg and the margin backing the short, so
    the position is smaller than the account: S = C·L/(L+1). This is the step
    that turns a headline funding yield into an actual return on money.

    A rising price loses on the short leg and gains on the spot leg, so the
    pair is flat — but the margin account drains while the spot gains sit in
    the other leg. Liquidation is checked against the margin account alone,
    which is how it actually works on an exchange.
    """
    fee = (fee_bps + slip_bps) / 1e4
    share = leverage / (leverage + 1.0)
    equity = start_cash
    notional = equity * share
    qty = None
    entry = None
    margin = equity - notional
    curve, liquidations, funding_paid = [], 0, 0.0
    peak, max_dd = start_cash, 0.0
    negatives = 0

    for t_ms, rate in rows:
        ts = t_ms // 1000
        price = spot_at(bars, ts)
        if price is None or price <= 0:
            continue
        if qty is None:                                   # open both legs
            qty = notional / price
            entry = price
            equity -= notional * fee * 2                  # spot buy + perp sell
            curve.append((ts, equity))
            continue

        # Mark both legs. Spot gains what the perp loses, so only funding and
        # costs move the total — but the margin account feels the short alone.
        short_pnl = (entry - price) * qty
        margin_now = margin + short_pnl
        if margin_now <= notional * maint_margin:
            # The short is liquidated: the loss is realised, the spot leg is
            # sold, and the position is reopened at the new price.
            liquidations += 1
            equity = max(0.0, equity + short_pnl + (price - entry) * qty
                         - notional * fee * 2)
            notional = equity * share
            margin = equity - notional
            qty = notional / price if price > 0 else 0.0
            entry = price
            curve.append((ts, equity))
            continue

        cash = rate * qty * price                         # shorts receive rate > 0
        funding_paid += cash
        equity += cash
        margin += cash
        if rate < 0:
            negatives += 1

        # Drift back to delta neutral when the legs separate enough.
        want = equity * share / price
        if abs(want - qty) * price > equity * rebalance_band:
            equity -= abs(want - qty) * price * fee * 2
            qty = want
            entry = price
            notional = equity * share
            margin = equity - notional

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
        curve.append((ts, equity))

    if not curve:
        return None
    years = (curve[-1][0] - curve[0][0]) / YEAR
    total = curve[-1][1] / start_cash - 1.0
    cagr = (curve[-1][1] / start_cash) ** (1 / years) - 1 if years > 0 and curve[-1][1] > 0 else -1.0
    rets = [curve[i][1] / curve[i - 1][1] - 1
            for i in range(1, len(curve)) if curve[i - 1][1] > 0]
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    return {"curve": curve, "total_pct": total * 100, "cagr_pct": cagr * 100,
            "max_dd_pct": max_dd * 100, "years": years,
            "sharpe": mu / sd * math.sqrt(per_day * 365) if sd > 0 else 0.0,
            "liquidations": liquidations, "negative_pct": negatives / len(rows) * 100,
            "funding_pct": funding_paid / start_cash * 100}


def raw_yield(rows, per_day=DEFAULT_PER_DAY):
    """
    The number people quote: mean funding, annualised, ignoring collateral.

    per_day is not optional in spirit — an hourly rate annualised as if it
    settled every eight hours is eight times too big.
    """
    if not rows:
        return 0.0
    mean = sum(r for _, r in rows) / len(rows)
    return mean * per_day * 365 * 100


def by_year(rows):
    out = {}
    for t_ms, rate in rows:
        y = datetime.fromtimestamp(t_ms // 1000, timezone.utc).year
        out.setdefault(y, []).append(rate)
    return out


# ------------------------------------------------------------------------------
# Report
# ------------------------------------------------------------------------------
def _d(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def study(sealed_years=2.0):
    lab = _lab()
    rows, per_day, source = load_funding()
    bars = lab.load_history(symbol="BTC", gran=DAY)
    bars, _ = lab.longest_continuous(bars, gran=DAY, max_gap_hours=7)
    rows = [r for r in rows if r[0] // 1000 >= bars[0][0]]
    if len(rows) < 500:
        logger.error("只有 %d 個資金費結算,唔夠做研究", len(rows))
        return 1
    logger.info("資金費結算 %d 次(%s,每日 %d 次):%s → %s",
                len(rows), source, per_day,
                _d(rows[0][0] // 1000), _d(rows[-1][0] // 1000))

    print("\n" + "=" * 88)
    print("一、資金費本身 —— 大家 quote 嗰個數")
    print("=" * 88)
    print("  全期年化平均:%.2f%%   (來源 %s,每日結算 %d 次)"
          % (raw_yield(rows, per_day), source, per_day))
    print("  (呢個 repo 由第一日起就假設 11%% —— 而家有真數對返)")
    print("\n  %-8s %10s %10s %12s" % ("年", "年化%", "負數比例", "結算次數"))
    for y, rates in sorted(by_year(rows).items()):
        neg = sum(1 for r in rates if r < 0) / len(rates) * 100
        print("  %-8d %10.2f %9.0f%% %12d"
              % (y, sum(rates) / len(rates) * per_day * 365 * 100, neg, len(rates)))

    print("\n" + "=" * 88)
    print("二、實際落手做會賺幾多 —— 抵押品食咗一半")
    print("=" * 88)
    print("  現貨同永續同等名義,所以用 L 倍槓桿,倉位只可以係本金嘅 L/(L+1)。")
    print("\n  %-10s %9s %9s %9s %9s %9s %8s"
          % ("槓桿", "總回報%", "年化%", "最大回撤%", "Sharpe", "爆倉次數", "倉位佔本金"))
    results = {}
    for lev in (1.0, 2.0, 3.0, 5.0):
        r = carry(rows, bars, leverage=lev, per_day=per_day)
        if not r:
            continue
        results[lev] = r
        print("  %-10s %9.1f %9.2f %9.1f %9.2f %9d %7.0f%%"
              % ("%.0fx" % lev, r["total_pct"], r["cagr_pct"], r["max_dd_pct"],
                 r["sharpe"], r["liquidations"], lev / (lev + 1) * 100))

    print("\n" + "=" * 88)
    print("三、同其他做法比(同一段期間)")
    print("=" * 88)
    base = results.get(1.0)
    if base:
        t0, t1 = base["curve"][0][0], base["curve"][-1][0]
        seg = [b for b in bars if t0 <= b[0] <= t1]
        bh = seg[-1][4] / seg[0][4] - 1.0 if len(seg) > 1 else 0.0
        pk, dd = seg[0][4], 0.0
        for b in seg:
            pk = max(pk, b[4])
            dd = max(dd, (pk - b[4]) / pk)
        P = {"fast": 3, "slow": 30, "target_vol": 45.0, "vol_window": 30}
        want = lab.STRATEGIES["dual_sma_voltarget"][0](lab.Indicators(seg), P)
        mm = lab.metrics(seg, *lab.backtest(seg, want, fee_bps=10.0, slip_bps=5.0,
                                            rebalance_band=0.10, dd_limit=0.15,
                                            dd_throttle=0.50, dd_recover=0.05), want=want)
        print("  %-26s %10s %10s %10s" % ("", "總回報%", "最大回撤%", "Sharpe"))
        print("  %-26s %10.1f %10.1f %10.2f"
              % ("期現套利 1x", base["total_pct"], base["max_dd_pct"], base["sharpe"]))
        if 3.0 in results:
            r3 = results[3.0]
            print("  %-26s %10.1f %10.1f %10.2f"
                  % ("期現套利 3x", r3["total_pct"], r3["max_dd_pct"], r3["sharpe"]))
        print("  %-26s %10.1f %10.1f %10.2f"
              % ("趨勢策略(而家上線嗰個)", mm["total_return_pct"], mm["max_dd_pct"], mm["sharpe"]))
        print("  %-26s %10.1f %10.1f %10s" % ("BTC 買咗唔郁", bh * 100, dd * 100, "—"))

    print("\n" + "=" * 88)
    print("四、封存期(最後 %g 年)—— 呢段數據冇參與過任何決定" % sealed_years)
    print("=" * 88)
    cut = rows[-1][0] - int(sealed_years * YEAR * 1000)
    tail = [r for r in rows if r[0] >= cut]
    print("  封存期年化資金費:%.2f%%  (全期 %.2f%%)"
          % (raw_yield(tail, per_day), raw_yield(rows, per_day)))
    print("\n  %-10s %9s %9s %9s %9s" % ("槓桿", "總回報%", "年化%", "最大回撤%", "爆倉次數"))
    for lev in (1.0, 2.0, 3.0):
        r = carry(tail, bars, leverage=lev, per_day=per_day)
        if r:
            print("  %-10s %9.1f %9.2f %9.1f %9d"
                  % ("%.0fx" % lev, r["total_pct"], r["cagr_pct"],
                     r["max_dd_pct"], r["liquidations"]))

    print("\n呢個研究冇計嘅風險:交易所倒閉、提現凍結、永續合約同現貨脫鈎、")
    print("借幣利息、以及稅。歷史資金費講唔到呢啲嘢。")
    print("\nDONE")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="BTC basis / funding carry study")
    ap.add_argument("--fetch", action="store_true", help="cache Binance funding history")
    ap.add_argument("--study", action="store_true", help="run the carry study")
    ap.add_argument("--symbol", default="", help="override the venue default")
    ap.add_argument("--sealed-years", type=float, default=2.0)
    args = ap.parse_args(argv)

    if args.fetch:
        try:
            rows, per_day, source = fetch_funding(args.symbol or None)
            save_funding(rows, per_day=per_day, source=source)
        except Exception as exc:  # noqa: BLE001
            logger.error("資金費數據攞唔到:%s", str(exc)[:160])
            return 1
        return 0
    if args.study:
        return study(args.sealed_years)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
