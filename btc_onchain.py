#!/usr/bin/env python3
"""
================================================================================
  On-chain study — does what happens ON the Bitcoin network predict the price?
================================================================================
  The question this answers is "do whale addresses / on-chain flows help?",
  as far as free data allows it to be answered at all.

  What is NOT here, and why:

    Real whale tracking needs address clustering — deciding which addresses
    belong to the same entity — and that is inference, not fact. The clustered
    datasets are commercial (Glassnode, CryptoQuant). Worse, most large
    transfers are exchanges moving their own coins between wallets, which
    looks identical to accumulation from the outside.

  What IS here: the free aggregate series from blockchain.com, plus the
  closest honest stand-in for whale activity — the AVERAGE SIZE of a
  transaction, in dollars. When large holders move, the average transfer
  swells; that is the part of "whales are doing something" which is visible
  without guessing who owns what.

  The test is deliberately hostile. Every signal is turned into a gate on the
  strategy the bot already runs, and then compared against the SAME gate
  rotated to a random point in time. Rotation keeps the gate's exposure and
  its run-lengths exactly, and destroys only its alignment with the price. A
  signal that cannot beat its own rotations is telling you nothing; it is
  just keeping you out of the market, which flatters any filter in a bear
  market.

  Usage:
      python btc_onchain.py --fetch          # cache the on-chain series
      python btc_onchain.py --study          # run the gate study
================================================================================
"""

import argparse
import gzip
import json
import logging
import os
import random
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("btc-chain")

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "btc_demo", "history")
CHAIN_FILE = os.path.join(HISTORY_DIR, "BTC_onchain.csv.gz")

DAY = 86400
API = "https://api.blockchain.info/charts/%s?timespan=all&format=json&sampled=false"

# Every one of these is free and needs no key. The names are blockchain.com's.
CHARTS = [
    ("addresses", "n-unique-addresses",             "每日活躍地址數"),
    ("txcount",   "n-transactions",                 "每日鏈上交易筆數"),
    ("txvolume",  "estimated-transaction-volume-usd", "每日鏈上轉帳金額 (USD)"),
    ("fees",      "transaction-fees-usd",           "每日手續費總額 (USD)"),
    ("hashrate",  "hash-rate",                      "算力"),
    ("mcap",      "market-cap",                     "市值 (USD)"),
]
COLUMNS = [c[0] for c in CHARTS]


# ------------------------------------------------------------------------------
# Fetch and cache
# ------------------------------------------------------------------------------
def _get(url, timeout=60):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "btc-lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_chart(slug):
    """One daily series as {day_start_epoch: value}."""
    doc = _get(API % slug)
    vals = doc.get("values") or []
    out = {}
    for point in vals:
        try:
            t = int(point["x"])
            y = float(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
        out[t - t % DAY] = y            # snap to the start of the UTC day
    if not out:
        raise RuntimeError("%s returned no values" % slug)
    return out


def fetch_all():
    series = {}
    for key, slug, label in CHARTS:
        series[key] = fetch_chart(slug)
        logger.info("  %-10s %6d days  (%s)", key, len(series[key]), label)
    return series


def save_chain(series, path=None):
    """
    Deterministic, same as the price cache: identical data must produce
    identical bytes, or the workflow commits a new file every single run.
    """
    path = path or CHAIN_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    days = sorted(set().union(*[set(s) for s in series.values()]))
    body = ["date," + ",".join(COLUMNS) + "\n"]
    for d in days:
        row = [datetime.fromtimestamp(d, timezone.utc).strftime("%Y-%m-%d")]
        for col in COLUMNS:
            v = series[col].get(d)
            row.append("" if v is None else "%.6g" % v)
        body.append(",".join(row) + "\n")
    raw = "".join(body).encode("utf-8")
    with open(path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            gz.write(raw)
    logger.info("saved %d days -> %s (%.0f KB)",
                len(days), os.path.basename(path), os.path.getsize(path) / 1024)
    return path


def load_chain(path=None):
    """{day_epoch: {column: float or None}}, oldest first."""
    path = path or CHAIN_FILE
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
            try:
                d = int(datetime.strptime(parts[0], "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                continue
            row = {}
            for i, c in enumerate(cols):
                txt = parts[i + 1]
                row[c] = float(txt) if txt else None
            out[d] = row
    return out


# ------------------------------------------------------------------------------
# Derived series
# ------------------------------------------------------------------------------
def derive(chain):
    """
    avgtx  — average dollars per on-chain transaction. The free stand-in for
             "are large holders moving coins": when whales transact, the mean
             transfer swells even though the number of transfers does not.
    nvt    — market cap divided by the dollars actually settled on chain. High
             means the price is large relative to the network's real use.
    feerate— dollars of fee per transaction: how badly people want block space.
    """
    for d, row in chain.items():
        tv, tc, mc, fe = row.get("txvolume"), row.get("txcount"), \
            row.get("mcap"), row.get("fees")
        row["avgtx"] = (tv / tc) if (tv and tc) else None
        row["nvt"] = (mc / tv) if (mc and tv and tv > 0) else None
        row["feerate"] = (fe / tc) if (fe and tc) else None
    return chain


DERIVED = ["avgtx", "nvt", "feerate"]


# ------------------------------------------------------------------------------
# The study
# ------------------------------------------------------------------------------
def _lab():
    """Import the backtester without making it a hard dependency of --fetch."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", os.path.join(HERE, "btc_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BARS_PER_DAY = 1
    return mod


YEAR = 365.25 * DAY
STRAT_P = {"fast": 3, "slow": 30, "target_vol": 45.0, "vol_window": 30}
COST = dict(fee_bps=10.0, slip_bps=5.0, rebalance_band=0.10,
            dd_limit=0.15, dd_throttle=0.50, dd_recover=0.05)

# (column, direction, label). direction +1 means "high is bullish".
SIGNALS = [
    ("avgtx",     +1, "平均每筆轉帳金額 ↑  ← whale 代用指標"),
    ("addresses", +1, "活躍地址數 ↑"),
    ("txcount",   +1, "交易筆數 ↑"),
    ("txvolume",  +1, "鏈上轉帳金額 ↑"),
    ("fees",      +1, "手續費總額 ↑"),
    ("feerate",   +1, "每筆手續費 ↑"),
    ("hashrate",  +1, "算力 ↑"),
    ("nvt",       -1, "NVT ↓(價格相對使用量平)"),
]
WINDOWS = [30, 90]
ROTATIONS = 500


def align(bars, chain):
    """One on-chain row per price bar, by UTC day. Missing days carry None."""
    return [chain.get(b[0] - b[0] % DAY, {}) for b in bars]


def gate_from(rows, column, direction, window):
    """
    True when the metric sits on the bullish side of its own trailing average.

    The average is trailing and EXCLUDES nothing that was not already public:
    day i's value is complete at the end of day i, and the position it implies
    is taken at day i+1's open, exactly like a closing price.
    """
    vals = [r.get(column) if r else None for r in rows]
    out = [False] * len(vals)
    buf = []
    for i, v in enumerate(vals):
        if v is not None:
            buf.append(v)
            if len(buf) > window:
                buf.pop(0)
        if v is not None and len(buf) == window:
            avg = sum(buf) / window
            out[i] = (v > avg) if direction > 0 else (v < avg)
    return out


def calmar_of(lab, bars, want, gate):
    w = [want[i] if gate[i] else 0.0 for i in range(len(want))]
    eq, tr = lab.backtest(bars, w, **COST)
    mm = lab.metrics(bars, eq, tr, want=w)
    return mm


def rotate(gate, k):
    k %= len(gate)
    return gate[-k:] + gate[:-k] if k else list(gate)


def percentile_vs_rotations(lab, bars, want, gate, trials=ROTATIONS, seed=0):
    """
    How good is this gate compared with itself, moved to a random point in
    history? Rotation preserves exposure and run-lengths exactly, so anything
    left is timing — which is the only thing a signal can actually claim.
    """
    real = calmar_of(lab, bars, want, gate)
    if real is None:
        return None
    rng = random.Random(seed)
    n = len(gate)
    scores = []
    for _ in range(trials):
        k = rng.randrange(60, n - 60) if n > 200 else rng.randrange(n)
        mm = calmar_of(lab, bars, want, rotate(gate, k))
        if mm:
            scores.append(mm["calmar"])
    if not scores:
        return None
    scores.sort()
    beat = sum(1 for s in scores if real["calmar"] > s)
    return {"real": real, "pct": beat / len(scores) * 100.0,
            "median": scores[len(scores) // 2],
            "p90": scores[int(len(scores) * 0.9)],
            "exposure": sum(1 for g in gate if g) / len(gate) * 100.0}


def split_bars(bars, years=2):
    cut = bars[-1][0] - int(years * YEAR)
    lo = next(i for i, b in enumerate(bars) if b[0] >= cut)
    return bars[:lo], bars[max(0, lo - 120):]


def study(years_sealed=2, trials=ROTATIONS):
    lab = _lab()
    chain = derive(load_chain())
    bars = lab.load_history(symbol="BTC", gran=DAY)
    bars, _ = lab.longest_continuous(bars, gran=DAY, max_gap_hours=7)
    have = [b for b in bars if (b[0] - b[0] % DAY) in chain]
    logger.info("價格 %d 日,鏈上數據 %d 日,對得上 %d 日", len(bars), len(chain), len(have))
    bars = have

    windows = [("揀參數期(封存期之前)", split_bars(bars, years_sealed)[0]),
               ("封存期(最後 %d 年)" % years_sealed, split_bars(bars, years_sealed)[1])]

    for title, seg in windows:
        if len(seg) < 200:
            logger.warning("%s 資料唔夠,跳過", title)
            continue
        want = lab.STRATEGIES["dual_sma_voltarget"][0](lab.Indicators(seg), STRAT_P)
        base = lab.metrics(seg, *lab.backtest(seg, want, **COST), want=want)
        print("\n" + "=" * 96)
        print("%s   —   %s → %s" % (title, _d(seg[0][0]), _d(seg[-1][0])))
        print("=" * 96)
        print("  基準(乜鏈上數據都唔睇):Calmar %.2f  回報 %+.0f%%  場內 %.0f%%"
              % (base["calmar"], base["total_return_pct"], base["exposure_pct"]))
        print("\n  %-34s %4s %7s %8s %9s %9s %9s"
              % ("鏈上訊號", "窗", "場內%", "Calmar", "隨機中位", "隨機90%", "打贏隨機"))
        print("  " + "-" * 92)
        rows = []
        for col, direction, label in SIGNALS:
            for w in WINDOWS:
                gate = gate_from(align(seg, chain), col, direction, w)
                if sum(1 for g in gate if g) < 30:
                    continue
                r = percentile_vs_rotations(lab, seg, want, gate, trials=trials,
                                            seed=hash((col, w)) & 0xFFFF)
                if not r:
                    continue
                rows.append((col, w, label, r))
                print("  %-34s %4d %6.0f%% %8.2f %9.2f %9.2f %8.0f%%"
                      % (label, w, r["exposure"], r["real"]["calmar"],
                         r["median"], r["p90"], r["pct"]))
        strong = [r for r in rows if r[3]["pct"] >= 90.0]
        print("\n  打贏自己 90%% 以上旋轉版本嘅:%d / %d" % (len(strong), len(rows)))
        for col, w, label, r in sorted(strong, key=lambda x: -x[3]["pct"]):
            print("    %-34s %d 日窗  贏 %.0f%%  Calmar %.2f  回報 %+.0f%%"
                  % (label, w, r["pct"], r["real"]["calmar"], r["real"]["total_return_pct"]))
    print("\n讀法:「打贏隨機」低過 90% 就當佢冇料。")
    print("      喺揀參數期高分、封存期跌返落嚟,就係過擬合,唔係訊號。")
    return 0


def _d(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bitcoin on-chain signal study")
    ap.add_argument("--fetch", action="store_true", help="download and cache the free series")
    ap.add_argument("--study", action="store_true", help="run the gate study")
    ap.add_argument("--sealed-years", type=float, default=2.0)
    ap.add_argument("--rotations", type=int, default=ROTATIONS)
    args = ap.parse_args(argv)

    if args.fetch:
        try:
            save_chain(fetch_all())
        except Exception as exc:  # noqa: BLE001
            logger.error("鏈上數據攞唔到:%s", exc)
            return 1
        return 0
    if args.study:
        return study(args.sealed_years, args.rotations)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
