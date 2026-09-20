#!/usr/bin/env python3
"""
================================================================================
  BTC Forward Lab — 30/90/180-day review exporter (deterministic, read-only)
================================================================================
  Produces the pre-registered checkpoint review defined in
  btc_demo/lab/evaluation_protocol.md. It reads ONLY the append-only archives
  (orders_*.csv / equity_*.csv / opportunities_*.csv) plus state.json, fetches
  nothing, and uses the LAST EQUITY SNAPSHOT as its "as of" clock — the same
  archives always yield the same report, byte for byte.

  Verdict fields are hard-gated: before 30 forward days every verdict reads
  "N/A — 未夠30日" (the only early rule is the anytime MDD retire line R1).
  Escalation is only ever "flag for a fresh matched-control study + adversarial
  audit" — this tool never recommends allocation, deployment, or any claim.

  The forward-vs-backtest section reads btc_demo/lab/backtest_reference.json
  if (and only if) it exists; otherwise it prints N/A. It never synthesises a
  reference distribution.

  Usage:
      python btc_lab_review.py                # markdown to stdout
      python btc_lab_review.py --out FILE.md  # also write to FILE.md

  Dependencies: Python 3.9+ standard library only.
================================================================================
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LAB_DIR = os.path.join(HERE, "btc_demo", "lab")
HOUR = 3600
DAY = 86400

MDD_RETIRE_PCT = -30.0          # R1, anytime
RET180_RETIRE_PCT = -20.0       # R2, at >=180d checkpoint
B_RETIRE_MIN_EPISODES = 10      # R3 / R3c (episodic strategies: B and C)
B_ESCALATE_MIN_EPISODES = 8     # E4 / E4c
FUNDING_RETIRE_MIN_PLACED = 12  # R5c: resting orders placed …
FUNDING_RETIRE_FILLRATE_PCT = 25.0   # … with fill rate at/below this, at T+180
VERDICT_MIN_DAYS = 30
ESCALATE_MIN_DAYS = 90


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _read_csvs(lab_dir, prefix):
    rows = []
    if not os.path.isdir(lab_dir):
        return rows
    for name in sorted(os.listdir(lab_dir)):
        if name.startswith(prefix) and name.endswith(".csv"):
            with open(os.path.join(lab_dir, name), encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
    return rows


def load_inputs(lab_dir):
    with open(os.path.join(lab_dir, "state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    return {"state": state,
            "orders": _read_csvs(lab_dir, "orders_"),
            "opps": _read_csvs(lab_dir, "opportunities_"),
            "equity": _read_csvs(lab_dir, "equity_")}


def protocol_sha256(lab_dir):
    p = os.path.join(lab_dir, "evaluation_protocol.md")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def episodes_from_orders(orders):
    """Completed flat-to-flat episodes: [{'pnl':…, 'entry_ts':…, 'exit_ts':…}].
    An episode opens on a BUY from qty_before==0 and completes on the SELL that
    leaves qty_after==0; realized_pnl_delta accumulates across partial sells.
    A still-open position is NOT a completed episode."""
    done, cur = [], None
    for o in orders:
        side = o["side"]
        qty_before = float(o["qty_before"] or 0)
        qty_after = float(o["qty_after"] or 0)
        if side == "BUY" and qty_before <= 1e-12 and cur is None:
            cur = {"pnl": 0.0, "entry_ts": int(o["decision_ts"]), "exit_ts": None}
        if cur is not None and side == "SELL":
            cur["pnl"] += float(o["realized_pnl_delta"] or 0)
            if qty_after <= 1e-12:
                cur["exit_ts"] = int(o["decision_ts"])
                done.append(cur)
                cur = None
    return done, cur is not None


def analyse_account(sid, acct, state, orders, opps, equity):
    eq = [r for r in equity if r["strategy"] == sid and r.get("mark_source") != "unavailable"]
    ods = [o for o in orders if o["strategy"] == sid]
    ops = [o for o in opps if o["strategy"] == sid]
    start_cash = float(acct.get("start_cash", 100000.0))
    # later-added accounts (candidate C) carry their OWN activation epoch —
    # forward_days and expected snapshot coverage both start there, not at
    # the lab's original activation
    activation_ts = int(acct.get("activation_ts") or state["activation_ts"])
    out = {"strategy_id": sid, "params": acct.get("params", {}),
           "param_hash": acct.get("param_hash"), "paused": acct.get("paused", False),
           "start_cash": start_cash}
    if not eq:
        out["status"] = "no_equity_snapshots"
        return out
    navs = [(int(r["ts"]), float(r["nav"])) for r in eq]
    as_of_ts = navs[-1][0]
    forward_days = (as_of_ts - activation_ts) / DAY
    nav_end = navs[-1][1]
    ret_pct = (nav_end / start_cash - 1) * 100
    peak, mdd = -1e18, 0.0
    for _, nav in navs:
        peak = max(peak, nav)
        mdd = min(mdd, (nav / peak - 1) * 100)
    # engine dedupes snapshots per UTC hour bucket, so expectation counts buckets
    expected_snaps = (as_of_ts // HOUR) - (activation_ts // HOUR) + 1
    coverage_pct = min(100.0, len(navs) / expected_snaps * 100) if expected_snaps > 0 else 0.0

    eps, has_open = episodes_from_orders(ods)
    wins = [e for e in eps if e["pnl"] > 0]
    win_rate = (len(wins) / len(eps) * 100) if eps else None
    best_pnl = max((e["pnl"] for e in eps), default=0.0)
    drop_best_ret_pct = ((nav_end - best_pnl) / start_cash - 1) * 100 if eps else None

    # cost fragility: actual paper cost vs the per-order fixed-cost diagnostic
    act_cost = diag_cost = notional = 0.0
    for o in ods:
        qty, mid = float(o["qty"]), float(o["mid"])
        act_cost += abs(float(o["fill_price"]) - mid) * qty + float(o["fee"])
        diag_cost += abs(float(o["diag_fill_price"]) - mid) * qty + float(o["diag_fee"])
        notional += float(o["gross_notional"])
    cost_gap_bp = ((act_cost - diag_cost) / notional * 1e4) if notional > 0 else None

    dec_counts = {}
    for o in ops:
        dec_counts[o["decision"]] = dec_counts.get(o["decision"], 0) + 1

    # candidate C: resting-limit-order fill funnel (protocol §9.3 — the whole
    # point of that account is these counts)
    fill_funnel = None
    if (acct.get("params") or {}).get("kind") == "funding_limit":
        placed = dec_counts.get("resting_placed", 0)
        filled = sum(1 for o in ods
                     if o["side"] == "BUY" and o.get("reason") == "limit_fill_entry")
        fill_funnel = {
            "placed": placed, "filled": filled,
            "expired": dec_counts.get("expired", 0),
            "cancelled": dec_counts.get("cancelled", 0),
            "fill_rate_pct": round(filled / placed * 100, 1) if placed else None,
            "median_place_to_fill_min": (sorted(
                int(o["lateness_sec"]) // 60 for o in ods
                if o["side"] == "BUY" and o.get("reason") == "limit_fill_entry")
                [filled // 2] if filled else None)}

    # date-matched control (protocol §5): BTC move × mean exposure fraction
    marks = [float(r["mark"]) for r in eq]
    mean_expo = sum(float(r["exposure_pct"]) for r in eq) / len(eq) / 100.0
    control_ret_pct = (marks[-1] / marks[0] - 1) * 100 * mean_expo if marks[0] > 0 else None

    out.update({"as_of_ts": as_of_ts, "as_of": iso(as_of_ts),
                "forward_days": round(forward_days, 2),
                "nav": round(nav_end, 2), "return_pct": round(ret_pct, 3),
                "mdd_pct": round(mdd, 3),
                "fees_cum": round(float(eq[-1]["fees_cum"]), 2),
                "orders": len(ods), "episodes_completed": len(eps),
                "position_open": has_open,
                "win_rate_pct": None if win_rate is None else round(win_rate, 1),
                "episode_pnls": [round(e["pnl"], 2) for e in eps],
                "drop_best_return_pct": None if drop_best_ret_pct is None else round(drop_best_ret_pct, 3),
                "cost_actual_usd": round(act_cost, 2), "cost_diag_usd": round(diag_cost, 2),
                "cost_gap_bp": None if cost_gap_bp is None else round(cost_gap_bp, 2),
                "traded_notional": round(notional, 2),
                "snapshots": len(navs), "snapshots_expected": expected_snaps,
                "coverage_pct": round(coverage_pct, 1),
                "opportunity_decisions": dec_counts,
                "fill_funnel": fill_funnel,
                "mean_exposure_pct": round(mean_expo * 100, 2),
                "control_return_pct": None if control_ret_pct is None else round(control_ret_pct, 3)})
    out["verdict"] = verdict(sid, out)
    return out


def verdict(sid, m):
    """Apply the frozen protocol rules. Returns dict of rule -> status text.
    Episodic rules (R3/E4) route on params.kind — B (rvol_breakout) per
    protocol v1, C (funding_limit) per the v1.1 §9 addendum with the same
    thresholds; C additionally carries the R5c fill-rate retire line."""
    v = {}
    days = m["forward_days"]
    kind = (m.get("params") or {}).get("kind")
    episodic = kind in ("rvol_breakout", "funding_limit")
    is_funding = kind == "funding_limit"
    # R1 applies anytime
    if m["mdd_pct"] <= MDD_RETIRE_PCT:
        v["R1_mdd"] = "TRIGGERED — MDD %.1f%% ≤ %.0f%%,按規則應退役(--pause)" % (
            m["mdd_pct"], MDD_RETIRE_PCT)
    else:
        v["R1_mdd"] = "ok(MDD %.1f%% > %.0f%%)" % (m["mdd_pct"], MDD_RETIRE_PCT)
    if days < VERDICT_MIN_DAYS:
        v["checkpoint"] = "N/A — 未夠30日(而家 %.1f 日);R1 以外一切審裁唔生效" % days
        return v
    v["checkpoint"] = "T+%dd" % (180 if days >= 180 else (90 if days >= 90 else 30))
    if days >= 180 and m["return_pct"] <= RET180_RETIRE_PCT:
        v["R2_return180"] = "TRIGGERED — 淨回報 %.1f%% ≤ %.0f%%" % (m["return_pct"], RET180_RETIRE_PCT)
    elif days >= 180:
        v["R2_return180"] = "ok(%.1f%%)" % m["return_pct"]
    if episodic and days >= 90:
        pnl_sum = sum(m["episode_pnls"])
        if m["episodes_completed"] >= B_RETIRE_MIN_EPISODES and pnl_sum < 0:
            v["R3_episodes"] = "TRIGGERED — %d episodes,累計已實現 $%.2f < 0" % (
                m["episodes_completed"], pnl_sum)
        else:
            v["R3_episodes"] = "ok(%d episodes,累計已實現 $%.2f)" % (
                m["episodes_completed"], pnl_sum)
    if is_funding and days >= 180:
        ff = m.get("fill_funnel") or {}
        placed, rate = ff.get("placed", 0), ff.get("fill_rate_pct")
        if placed >= FUNDING_RETIRE_MIN_PLACED and rate is not None \
                and rate <= FUNDING_RETIRE_FILLRATE_PCT:
            v["R5c_fillrate"] = ("TRIGGERED — %d 張掛單成交率 %.1f%% ≤ %.0f%%:"
                                 "執行假說被前向證據答「否」— 退役並記錄結論"
                                 % (placed, rate, FUNDING_RETIRE_FILLRATE_PCT))
        else:
            v["R5c_fillrate"] = "ok(%d 張掛單,成交率 %s)" % (
                placed, "%.1f%%" % rate if rate is not None else "N/A")
    if days < ESCALATE_MIN_DAYS:
        v["escalate"] = "冇資格 — 升級要 ≥90日(E1)"
        return v
    checks = []
    checks.append(("E2", m["return_pct"] > 0,
                   "淨回報 %.2f%%" % m["return_pct"]))
    ctrl = m["control_return_pct"]
    checks.append(("E3", ctrl is not None and m["return_pct"] > ctrl,
                   "vs control %.2f%%" % ctrl if ctrl is not None else "control N/A"))
    if episodic:
        checks.append(("E4", m["episodes_completed"] >= B_ESCALATE_MIN_EPISODES,
                       "%d/%d episodes" % (m["episodes_completed"], B_ESCALATE_MIN_EPISODES)))
    db = m["drop_best_return_pct"]
    sign_flip = (db is not None and m["return_pct"] > 0 and db <= 0)
    checks.append(("E5", not sign_flip,
                   "drop-best %.2f%%" % db if db is not None else "no completed episodes"))
    failed = [c for c in checks if not c[1]]
    if not failed:
        v["escalate"] = ("ALL CONDITIONS MET — 動作僅限:通知用戶 + 排全新 matched-control 研究 "
                         "+ fresh-context 對抗式審核。唔係加注、唔係『已驗證』。")
    else:
        v["escalate"] = "未滿足:" + "; ".join("%s(%s)" % (c[0], c[2]) for c in failed)
    return v


def backtest_reference_section(lab_dir, results):
    p = os.path.join(lab_dir, "backtest_reference.json")
    if not os.path.exists(p):
        return ("**N/A — `backtest_reference.json` 未生成**(生成需要用原研究數據重跑同長度"
                "滾動窗口,屬另一件工作,見 protocol §7)。呢度唔會用其他數字頂替。")
    with open(p, encoding="utf-8") as fh:
        ref = json.load(fh)
    lines = ["參考檔來源:%s(generated %s)" % (ref.get("source", "?"), ref.get("generated_utc", "?")), ""]
    for r in results:
        sid = r["strategy_id"]
        days = r.get("forward_days")
        if (r.get("params") or {}).get("kind") == "funding_limit":
            lines.append("- %s:**N/A — by design**(呢個候選嘅核心變數係成交/唔成交,"
                         "backtest 模擬唔到;參照 spec §0 嘅三行成本括號:taker −12.6%% / "
                         "maker假設100%%成交 +9.3%% / 零成本 +13.1%%,前向數字落喺邊度就係答案)"
                         % sid)
            continue
        strat = (ref.get("strategies") or {}).get(sid)
        if not strat or days is None:
            lines.append("- %s:參考檔冇呢個策略 — N/A" % sid)
            continue
        buckets = sorted(int(k) for k in strat.get("window_days", {}))
        usable = [b for b in buckets if days >= b]
        if not usable:
            lines.append("- %s:前向 %.1f 日未夠最短參考窗口(%s 日)— N/A"
                         % (sid, days, buckets[0] if buckets else "?"))
            continue
        b = usable[-1]
        pct = strat["window_days"][str(b)]["return_pct_percentiles"]
        fwd = r["return_pct"]
        below = sum(1 for k in ("p5", "p25", "p50", "p75", "p95") if fwd >= pct[k])
        band = ["<p5", "p5–p25", "p25–p50", "p50–p75", "p75–p95", ">p95"][below]
        lines.append("- %s:前向 %d 日窗口回報 %.2f%%,處 backtest 同長度分佈 %s "
                     "(p5=%.1f p50=%.1f p95=%.1f)— 純定位,唔係檢定"
                     % (sid, b, fwd, band, pct["p5"], pct["p50"], pct["p95"]))
    return "\n".join(lines)


def build_review(lab_dir):
    d = load_inputs(lab_dir)
    state = d["state"]
    results = [analyse_account(sid, acct, state, d["orders"], d["opps"], d["equity"])
               for sid, acct in state["accounts"].items()]
    return {"state": state, "results": results,
            "protocol_sha256": protocol_sha256(lab_dir), "lab_dir": lab_dir}


def render_markdown(review):
    r0 = review["results"]
    st = review["state"]
    days = max((r.get("forward_days") or 0) for r in r0) if r0 else 0
    L = ["# 🧪 Forward Lab 檢討報告(paper/demo,未驗證研究候選)", "",
         "**一句講晒:呢份係照 evaluation_protocol.md 預先凍結規則出嘅例行檢討,"
         + ("未夠30日,所有審裁欄位一律 N/A,下面只係描述性數字。**" if days < VERDICT_MIN_DAYS
            else "審裁欄位按凍結規則機械式計出,冇人手斟酌空間。**"), "",
         "- 啟動:%s · venue %s:%s" % (st["activation_utc"],
                                       st["venue_epoch"]["venue"], st["venue_epoch"]["product"]),
         "- 報告時鐘 = 最後一個 equity 快照(確定性,唔用牆鐘)",
         "- Protocol SHA-256:`%s`" % (review["protocol_sha256"] or "MISSING — 有問題"),
         ""]
    for r in r0:
        L.append("## %s%s" % (r["strategy_id"], "(PAUSED)" if r.get("paused") else ""))
        if r.get("status") == "no_equity_snapshots":
            L += ["", "冇 equity 快照 — 冇嘢可報。", ""]
            continue
        ff = r.get("fill_funnel")
        L += ["",
              "| 指標 | 數值 |", "|---|---|",
              "| as of | %s(前向 %.1f 日,由該帳戶自己嘅 activation 起計) |"
              % (r["as_of"], r["forward_days"]),
              "| NAV | $%s(淨回報 %.2f%%) |" % (f"{r['nav']:,.2f}", r["return_pct"]),
              "| 最大回撤(小時NAV) | %.2f%% |" % r["mdd_pct"],
              "| 已完成 episodes / 訂單 | %d / %d%s |" % (
                  r["episodes_completed"], r["orders"],
                  "(另有未平倉位)" if r["position_open"] else ""),
              "| 勝率 | %s |" % ("N/A(未有完成 episode)" if r["win_rate_pct"] is None
                                 else "%.1f%%" % r["win_rate_pct"]),
              "| 剔走最好一單後回報 | %s |" % ("N/A" if r["drop_best_return_pct"] is None
                                                else "%.2f%%" % r["drop_best_return_pct"]),
              "| 累計費用 | $%.2f |" % r["fees_cum"],
              "| 成本脆弱度(實際 vs 固定成本診斷) | %s |" % (
                  "N/A(未有成交)" if r["cost_gap_bp"] is None else
                  "$%.2f vs $%.2f(差 %.1f bp/成交額)" % (
                      r["cost_actual_usd"], r["cost_diag_usd"], r["cost_gap_bp"])),
              "| 運行覆蓋率 | %d/%d 快照(%.1f%%) |" % (
                  r["snapshots"], r["snapshots_expected"], r["coverage_pct"]),
              "| 平均曝險 | %.1f%% |" % r["mean_exposure_pct"],
              "| Date-matched 控制組回報 | %s |" % (
                  "N/A" if r["control_return_pct"] is None else "%.2f%%" % r["control_return_pct"]),
              "| 機會決策統計 | %s |" % (json.dumps(r["opportunity_decisions"], ensure_ascii=False)
                                          or "{}")]
        if ff is not None:
            L.append("| 成交漏斗(候選C存在嘅意義) | 掛 %d / 成交 %d / 過期 %d / 取消 %d"
                     " · 成交率 %s · 掛單→成交中位 %s |"
                     % (ff["placed"], ff["filled"], ff["expired"], ff["cancelled"],
                        "%.1f%%" % ff["fill_rate_pct"] if ff["fill_rate_pct"] is not None
                        else "N/A(未有掛單)",
                        "%d 分鐘" % ff["median_place_to_fill_min"]
                        if ff["median_place_to_fill_min"] is not None else "N/A"))
        L += ["", "**審裁(凍結規則)**", ""]
        for k, txt in r["verdict"].items():
            L.append("- `%s`:%s" % (k, txt))
        L.append("")
    L += ["## Backtest 參考分佈對照", "", backtest_reference_section(review["lab_dir"], r0), "",
          "> 紙上成交、冇 broker;A/B 係事後、樣本薄嘅研究線索,C 係執行假說測試"
          "(測限價單成交率,唔係測訊號)— 三個都唔係已驗證系統。"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forward-lab checkpoint review (read-only)")
    ap.add_argument("--lab-dir", default=DEFAULT_LAB_DIR)
    ap.add_argument("--out", metavar="FILE")
    args = ap.parse_args(argv)
    if not os.path.exists(os.path.join(args.lab_dir, "state.json")):
        print("Lab not activated (no state.json) — nothing to review.")
        return 1
    md = render_markdown(build_review(args.lab_dir))
    print(md)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
