#!/usr/bin/env python3
"""
================================================================================
  Factor study — what does BTC actually move with?
================================================================================
  Regresses BTC returns on the things people assume drive it: US equities
  (SPY), gold (GLD), long bonds (TLT), the dollar, the 10-year yield, and the
  Fed funds rate.

  Three questions, kept apart on purpose, because mixing them is how a
  correlation gets sold as a trading edge:

    1. EXPLAINS  — do the factors move WITH BTC in the same week? This is a
                   description of what BTC is, not a way to make money. You
                   cannot trade a same-week beta: by the time you know SPY's
                   weekly return, the week is over.

    2. CHANGES   — is the relationship stable? A beta that flips sign every
                   couple of years is not a fact about BTC, it is a fact about
                   the sample someone chose.

    3. PREDICTS  — do THIS week's factors say anything about NEXT week's BTC?
                   Only this one could ever be traded, and it is the one that
                   almost always comes back empty.

  Weekly, not daily, on purpose: BTC trades every day and the ETFs do not, so
  daily alignment quietly compares a 7-day move against a 5-day one. Weeks are
  the shortest honest common unit.

  Everything is stdlib: the regression, the standard errors, the inverse.

  Usage:
      python btc_factors.py --fetch     # cache the factor series
      python btc_factors.py --study     # run the regressions
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
import urllib.request
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("btc-factors")

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "btc_demo", "history")
FACTOR_FILE = os.path.join(HISTORY_DIR, "factors.csv.gz")

DAY = 86400

# Daily closes, free, no key.
#
# Two sources, because one is not enough: Stooq answers a datacentre IP with a
# JavaScript challenge page rather than a CSV, which a naive parser reads as a
# header row full of HTML. Yahoo's chart endpoint serves JSON to the same IP.
# Order matters — Yahoo first, Stooq as the fallback for when it does not.
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=0&period2=9999999999&interval=1d"
STOOQ = "https://stooq.com/q/d/l/?s=%s&i=d"
ETFS = [
    ("SPY", "SPY", "spy.us", "美股大盤"),
    ("GLD", "GLD", "gld.us", "黃金"),
    ("TLT", "TLT", "tlt.us", "長債"),
    ("QQQ", "QQQ", "qqq.us", "科技股"),
]

# Federal Reserve's own series, free, no key — but FRED does not answer a
# GitHub runner at all: three requests, three sixty-second timeouts, three
# minutes of nothing. So it is the fallback and gets a short leash, while
# Yahoo (which does answer) carries the same instruments.
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
FRED_TIMEOUT = 25
RATES = [
    # name       yahoo        fred        label
    ("RATE3M",   "^IRX",      "DTB3",     "3 個月國庫券息率(政策利率代表)"),
    ("Y10",      "^TNX",      "DGS10",    "10 年期國債孳息"),
    ("DXY",      "DX-Y.NYB",  "DTWEXBGS", "美元指數"),
    # The actual Fed funds rate exists only on FRED. If FRED is unreachable
    # this column is simply absent, and RATE3M stands in for policy — a
    # three-month bill tracks the target closely, which is why it is the
    # stand-in rather than a second-best guess.
    ("FEDFUNDS", None,        "DFF",      "聯邦基金利率"),
]

COLUMNS = [e[0] for e in ETFS] + [r[0] for r in RATES]


# ------------------------------------------------------------------------------
# Fetch
# ------------------------------------------------------------------------------
UA = "btc-paper-demo/1.0 (+github actions; educational factor study)"


def _get_text(url, timeout=60):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def parse_csv_series(text, date_col, value_col):
    """
    A date->float map from a CSV that has a header.

    Both sources use '.' or blank for a missing day (a holiday, or a rate that
    is simply not published that day). Those rows are dropped rather than
    carried forward here; carrying forward is a decision the caller makes.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("empty CSV")
    head = [h.strip().strip('"').upper() for h in lines[0].split(",")]
    try:
        di = head.index(date_col.upper())
        vi = head.index(value_col.upper())
    except ValueError:
        raise RuntimeError("missing column: have %s, want %s/%s"
                           % (head, date_col, value_col))
    out = {}
    for line in lines[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) <= max(di, vi):
            continue
        try:
            d = datetime.strptime(parts[di][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            v = float(parts[vi])
        except ValueError:
            continue                      # '.' from FRED, or a malformed row
        out[parts[di][:10]] = v
    if not out:
        raise RuntimeError("no usable rows")
    return out


def parse_yahoo(text):
    """date -> adjusted close, from Yahoo's chart JSON."""
    doc = json.loads(text)
    res = ((doc.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise RuntimeError("no chart result")
    stamps = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    closes = None
    adj = ind.get("adjclose") or []
    if adj and adj[0].get("adjclose"):
        closes = adj[0]["adjclose"]
    elif (ind.get("quote") or [{}])[0].get("close"):
        closes = ind["quote"][0]["close"]
    if not closes or len(closes) != len(stamps):
        raise RuntimeError("timestamps and closes do not line up")
    out = {}
    for t, c in zip(stamps, closes):
        if c is None:
            continue                      # a holiday Yahoo pads with null
        out[datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")] = float(c)
    if not out:
        raise RuntimeError("no usable rows")
    return out


def fetch_fred(sid):
    text = _get_text(FRED % sid, timeout=FRED_TIMEOUT)
    head = text.splitlines()[0].split(",")
    return parse_csv_series(text, head[0].strip(), sid)


def fetch_series(ysym=None, ssym=None, fred_id=None):
    """
    Whichever source answers with data, in order of who actually replies.
    Returns (series, source name).
    """
    attempts = []
    if ysym:
        attempts.append(("yahoo", lambda: parse_yahoo(_get_text(YAHOO % ysym))))
    if ssym:
        attempts.append(("stooq", lambda: parse_csv_series(
            _get_text(STOOQ % ssym), "Date", "Close")))
    if fred_id:
        attempts.append(("fred", lambda: fetch_fred(fred_id)))
    errors = []
    for name, call in attempts:
        try:
            return call(), name
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (name, str(exc)[:80]))
    raise RuntimeError(" | ".join(errors) or "no source configured")


def fetch_etf(ysym, ssym):
    return fetch_series(ysym=ysym, ssym=ssym)


def fetch_all():
    """
    Collect what is reachable. One missing factor should cost that factor,
    not the whole study — but a completely empty result is an error, because
    that means every source is down and silence would look like success.
    """
    series, failed = {}, []
    for name, ysym, ssym, label in ETFS:
        try:
            got, src = fetch_etf(ysym, ssym)
            series[name] = got
            logger.info("  %-9s %5d days  via %-6s (%s)", name, len(got), src, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  %-9s 攞唔到 — %s", name, exc)
            failed.append(name)
    for name, ysym, fid, label in RATES:
        try:
            got, src = fetch_series(ysym=ysym, fred_id=fid)
            series[name] = got
            logger.info("  %-9s %5d days  via %-6s (%s)", name, len(got), src, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  %-9s 攞唔到 — %s", name, str(exc)[:140])
            failed.append(name)
    if not series:
        raise RuntimeError("一個因子都攞唔到")
    if failed:
        logger.warning("缺咗:%s", ", ".join(failed))
    return series


def save_factors(series, path=None):
    path = path or FACTOR_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [c for c in COLUMNS if c in series]
    days = sorted(set().union(*[set(s) for s in series.values()]))
    body = ["date," + ",".join(cols) + "\n"]
    for d in days:
        row = [d] + ["" if series[c].get(d) is None else "%.6g" % series[c][d]
                     for c in cols]
        body.append(",".join(row) + "\n")
    raw = "".join(body).encode("utf-8")
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            gz.write(raw)
    logger.info("saved %d days -> %s (%.0f KB)",
                len(days), os.path.basename(path), os.path.getsize(path) / 1024)
    return path


def load_factors(path=None):
    path = path or FACTOR_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        head = fh.readline().strip().split(",")
        cols = head[1:]
        out = {}
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) != len(head):
                continue
            out[parts[0]] = {c: (float(parts[i + 1]) if parts[i + 1] else None)
                             for i, c in enumerate(cols)}
    return out


# ------------------------------------------------------------------------------
# Least squares, by hand
# ------------------------------------------------------------------------------
def invert(mat):
    """Gauss-Jordan with partial pivoting. Returns None if singular."""
    n = len(mat)
    a = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(mat)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        d = a[col][col]
        a[col] = [x / d for x in a[col]]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col]
            if f:
                a[r] = [a[r][k] - f * a[col][k] for k in range(2 * n)]
    return [row[n:] for row in a]


def ols(y, X, names):
    """
    y ~ intercept + X. Plain OLS standard errors.

    No Newey-West here, and that matters: weekly returns have mild
    autocorrelation, so the t-statistics below are if anything a little too
    generous. A factor that fails to clear the bar on a generous test has
    certainly failed.
    """
    n = len(y)
    k = len(X[0]) + 1
    if n <= k + 2:
        return None
    Z = [[1.0] + list(row) for row in X]
    ztz = [[sum(Z[r][i] * Z[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    zty = [sum(Z[r][i] * y[r] for r in range(n)) for i in range(k)]
    inv = invert(ztz)
    if inv is None:
        return None
    beta = [sum(inv[i][j] * zty[j] for j in range(k)) for i in range(k)]
    fit = [sum(beta[i] * Z[r][i] for i in range(k)) for r in range(n)]
    resid = [y[r] - fit[r] for r in range(n)]
    rss = sum(e * e for e in resid)
    ybar = sum(y) / n
    tss = sum((v - ybar) ** 2 for v in y)
    sigma2 = rss / (n - k)
    se = [math.sqrt(max(sigma2 * inv[i][i], 0.0)) for i in range(k)]
    t = [(beta[i] / se[i]) if se[i] > 0 else 0.0 for i in range(k)]
    r2 = 1.0 - rss / tss if tss > 0 else 0.0
    return {"names": ["(截距)"] + list(names), "beta": beta, "se": se, "t": t,
            "r2": r2, "adj_r2": 1 - (1 - r2) * (n - 1) / (n - k), "n": n}


def print_ols(res, title, note=""):
    print("\n  " + title)
    if not res:
        print("    (數據唔夠)")
        return
    print("    %-14s %12s %10s %9s" % ("因子", "beta", "t 值", "顯著?"))
    for i, nm in enumerate(res["names"]):
        star = "***" if abs(res["t"][i]) > 2.6 else ("**" if abs(res["t"][i]) > 2.0 else "")
        print("    %-14s %12.4f %10.2f %9s" % (nm, res["beta"][i], res["t"][i], star))
    print("    n = %d 週   R² = %.3f   調整後 R² = %.3f" % (res["n"], res["r2"], res["adj_r2"]))
    if note:
        print("    " + note)


# ------------------------------------------------------------------------------
# Weekly alignment
# ------------------------------------------------------------------------------
PCT_COLS = ["SPY", "GLD", "TLT", "QQQ", "DXY"]     # prices -> percentage change
DIFF_COLS = ["RATE3M", "Y10", "FEDFUNDS"]           # already in %, so difference


def week_key(dt):
    iso = dt.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def weekly_last(series_by_date):
    """Last observation of each ISO week."""
    out = {}
    for ds in sorted(series_by_date):
        dt = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        out[week_key(dt)] = series_by_date[ds]
    return out


def weekly_btc(bars):
    out = {}
    for b in bars:
        dt = datetime.fromtimestamp(b[0], timezone.utc)
        out[week_key(dt)] = b[4]
    return out


def available_columns(factors):
    """Columns that are actually in the cache — a source can be down."""
    have = set()
    for row in factors.values():
        have |= {c for c, v in row.items() if v is not None}
    return [c for c in COLUMNS if c in have]


def build_panel(bars, factors, columns=None):
    """
    One row per week: BTC's return and each factor's move, all measured over
    the same calendar week. Weeks missing any factor are dropped whole — an
    unbalanced panel would let a factor's beta be estimated on a different
    period from its neighbours'.
    """
    columns = columns or available_columns(factors)
    if not columns:
        return []
    btc = weekly_btc(bars)
    cols = {}
    for c in columns:
        cols[c] = weekly_last({d: v[c] for d, v in factors.items() if v.get(c) is not None})
    weeks = sorted(set(btc) & set.intersection(*[set(cols[c]) for c in columns]))
    rows = []
    for i in range(1, len(weeks)):
        w, p = weeks[i], weeks[i - 1]
        if btc[p] <= 0:
            continue
        rec = {"week": w, "btc": (btc[w] / btc[p] - 1.0) * 100.0}
        ok = True
        for c in columns:
            if c in PCT_COLS:
                if cols[c][p] <= 0:
                    ok = False
                    break
                rec[c] = (cols[c][w] / cols[c][p] - 1.0) * 100.0
            else:
                rec[c] = cols[c][w] - cols[c][p]
        if ok:
            rows.append(rec)
    return rows


FACTOR_ORDER = ["SPY", "QQQ", "GLD", "TLT", "DXY", "RATE3M", "Y10", "FEDFUNDS"]
LABELS = {"SPY": "SPY 美股", "QQQ": "QQQ 科技", "GLD": "GLD 黃金", "TLT": "TLT 長債",
          "DXY": "美元指數", "RATE3M": "3個月息率", "Y10": "10年孳息",
          "FEDFUNDS": "聯邦基金利率"}


# ------------------------------------------------------------------------------
# The study
# ------------------------------------------------------------------------------
def _lab():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", os.path.join(HERE, "btc_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BARS_PER_DAY = 1
    return mod


def corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0:
        return 0.0
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb)


def study(sealed_years=2.0):
    lab = _lab()
    bars = lab.load_history(symbol="BTC", gran=DAY)
    bars, _ = lab.longest_continuous(bars, gran=DAY, max_gap_hours=7)
    factors = load_factors()
    have = available_columns(factors)
    order = [c for c in FACTOR_ORDER if c in have]
    missing = [c for c in FACTOR_ORDER if c not in have]
    if missing:
        logger.warning("cache 入面冇:%s —— 呢幾個因子今次唔計",
                       ", ".join(LABELS[c] for c in missing))
    rows = build_panel(bars, factors, order)
    if len(rows) < 60 or not order:
        logger.error("只有 %d 個共同星期,做唔到回歸", len(rows))
        return 1
    logger.info("共同星期 %d 個:%s → %s", len(rows), rows[0]["week"], rows[-1]["week"])

    y = [r["btc"] for r in rows]
    X = [[r[c] for c in order] for r in rows]
    names = [LABELS[c] for c in order]

    print("\n" + "=" * 92)
    print("一、BTC 同乜嘢一齊郁?(同一個星期,解釋用,唔係預測)")
    print("=" * 92)
    print("\n  單因子相關系數")
    print("    %-14s %10s" % ("因子", "相關"))
    for c in order:
        print("    %-14s %10.3f" % (LABELS[c], corr(y, [r[c] for r in rows])))
    print_ols(ols(y, X, names), "全部因子一齊擺入去",
              "beta 解讀:因子每郁 1 個單位,同一週 BTC 平均郁幾多 %。")

    print("\n" + "=" * 92)
    print("二、呢個關係穩唔穩定?(每兩年重新估一次 SPY 同 GLD 嘅 beta)")
    print("=" * 92)
    print("\n    %-14s %10s %10s %10s %8s" % ("期間", "SPY beta", "GLD beta", "R²", "週數"))
    years = sorted({r["week"][:4] for r in rows})
    for i in range(0, len(years) - 1, 2):
        span = set(years[i:i + 2])
        sub = [r for r in rows if r["week"][:4] in span]
        if len(sub) < 40:
            continue
        res = ols([r["btc"] for r in sub],
                  [[r[c] for c in order] for r in sub], names)
        if not res:
            continue
        bi = lambda c: res["beta"][1 + order.index(c)] if c in order else float("nan")
        print("    %-14s %10.3f %10.3f %10.3f %8d"
              % ("-".join(sorted(span)), bi("SPY"), bi("GLD"), res["r2"], res["n"]))

    print("\n" + "=" * 92)
    print("三、可唔可以用嚟預測?(今個星期嘅因子 → 下個星期嘅 BTC)")
    print("=" * 92)
    ly = [rows[i + 1]["btc"] for i in range(len(rows) - 1)]
    lX = [[rows[i][c] for c in order] for i in range(len(rows) - 1)]
    print_ols(ols(ly, lX, names), "全部因子(滯後一週)",
              "呢個先係唯一可以攞去買賣嘅回歸。")

    # Split it, because a predictive relationship that only exists in one half
    # of the sample is not a predictive relationship.
    cut = int(len(ly) * 0.7)
    print_ols(ols(ly[:cut], lX[:cut], names), "頭 70% 樣本")
    print_ols(ols(ly[cut:], lX[cut:], names), "尾 30% 樣本(封存)")

    print("\n" + "=" * 92)
    print("四、逐個因子單獨預測(避免多因子互相抵銷)")
    print("=" * 92)
    print("    %-14s %10s %8s %12s %10s" % ("因子", "beta", "t 值", "頭70% t", "尾30% t"))
    tested, passed, flipped = 0, 0, 0
    for c in order:
        full = ols(ly, [[r[c]] for r in [rows[i] for i in range(len(rows) - 1)]], [LABELS[c]])
        a = ols(ly[:cut], [[rows[i][c]] for i in range(cut)], [LABELS[c]])
        b = ols(ly[cut:], [[rows[i][c]] for i in range(cut, len(ly))], [LABELS[c]])
        if not (full and a and b):
            continue
        tested += 3
        passed += sum(1 for r in (full, a, b) if abs(r["t"][1]) > 2.0)
        if a["beta"][1] * b["beta"][1] < 0:
            flipped += 1
        print("    %-14s %10.4f %8.2f %12.2f %10.2f %s"
              % (LABELS[c], full["beta"][1], full["t"][1], a["t"][1], b["t"][1],
                 "← 兩半符號相反" if a["beta"][1] * b["beta"][1] < 0 else ""))

    # The count matters more than any single row. At the 5% level, one test in
    # twenty clears |t| > 2 by luck alone, so "we found a significant factor"
    # means nothing until you say how many you looked at.
    print("\n    一共試咗 %d 個係數,|t| > 2 嘅有 %d 個。" % (tested, passed))
    print("    純靠彩數預期會有 %.1f 個。" % (tested * 0.05))
    print("    %d / %d 個因子喺兩半樣本嘅符號係相反嘅。" % (flipped, len(order)))

    print("\n讀法:|t| > 2 先叫「唔似係彩數」。")
    print("      第三、四節如果冇嘢過到 2,就即係呢啲因子解釋到 BTC 係乜,")
    print("      但講唔到佢聽日會點 —— 呢兩件事完全唔同。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="BTC factor regressions")
    ap.add_argument("--fetch", action="store_true", help="download and cache the factor series")
    ap.add_argument("--study", action="store_true", help="run the regressions")
    ap.add_argument("--sealed-years", type=float, default=2.0)
    args = ap.parse_args(argv)

    if args.fetch:
        try:
            save_factors(fetch_all())
        except Exception as exc:  # noqa: BLE001
            logger.error("因子數據攞唔到:%s", exc)
            return 1
        return 0
    if args.study:
        return study(args.sealed_years)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
