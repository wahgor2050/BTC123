# BTC123 — BTC paper-trading forward lab

Paper trading only. No broker connection, no real orders, no API keys required to run.

This repo hosts a BTC paper-trading demo, migrated from a private research repo to keep
GitHub Actions minutes and git history separate from unrelated private work.

- `btc_autotrade.py` — the baseline bot: daily dual-SMA(3,30) trend + 45% volatility-target
  sizing. Runs once a day (00:15 UTC). Untouched by the forward lab.
- `btc_forward_lab.py` — the forward lab: three frozen, **unverified** research candidates
  run forward as independent $100,000 paper accounts (A/B activated 2026-09-20 12:23 UTC,
  C added 2026-09-20 16:41 UTC with its own epoch; venue Bitstamp BTC/USD spot):
  - `sma_1d_30d_v25_v1` — hourly SMA(24)>SMA(720) trend, 25% annualised vol target,
    10%-of-NAV resize band.
  - `rvol2_breakout24h_v25_v1` — 24h-high breakout above SMA(720), greedy parent schedule
    with signals ≥25h apart (gate-rejected parents still consume the cooldown),
    RVOL(28 same-UTC-hour days) ≥ 2 gate, fixed 24h hold.
  - `funding_z168_limit_entry_v1` — **execution-hypothesis test** (spec frozen before any
    forward data in `btc_demo/lab/funding_candidate_spec.md`): Kraken perp funding z-score
    (168-settlement trailing window) enter ≤ −2 / exit ≥ 0, entry via a resting LIMIT buy at
    best bid with a 4h deadline (fill = candle-low touch, 2bp maker fee; unfilled attempts
    logged, never fabricated), exit via the same taker model as A/B. The signal is real at
    zero cost but loses at taker cost; this account measures the actual fill rate — nothing
    else. Uses two data sources: Bitstamp spot (fills) + Kraken Futures (signal), via
    `btc_funding_signal.py`.
- `btc_demo/` — baseline paper account state, trade ledger, and dashboard pages.
- `btc_demo/lab/` — forward-lab state (`state.json`), append-only monthly archives
  (`orders_*.csv`, `opportunities_*.csv`, `equity_*.csv`), dashboard payload
  (`summary.json`) and the lab page (`index.html`).
- `btc_demo/lab/evaluation_protocol.md` — **pre-registered evaluation rules**, frozen on
  activation day before any forward data existed: exact retire/escalate/continue conditions
  for the 30/90/180-day checkpoints. Its SHA-256 is stamped into `summary.json` hourly so
  any edit is visible. "Escalate" only ever means *flag for a fresh matched-control study
  plus adversarial audit* — never allocation, deployment, or a "validated" claim.
- `btc_lab_review.py` — deterministic checkpoint-review exporter: reads only the
  append-only archives, prints the pre-registered report (verdicts are hard-gated to
  "N/A" before 30 forward days). Run `python btc_lab_review.py` any time.
- `.github/workflows/btc_autotrade.yml` — daily baseline schedule (00:15 UTC); also runs
  `btc_forward_lab.py --health-check`, a read-only watchdog on an independent schedule that
  sends a 🩺 Telegram alert if the hourly lab has silently stopped updating, has an
  unresolvable due exit, or shows ledger reconciliation failures. (Residual risk: if GitHub
  disables all schedules after 60 days of repo inactivity, the watchdog dies with them —
  GitHub's workflow-failure emails are the last backstop.)
- `.github/workflows/btc_forward_lab.yml` — hourly lab schedule (minute 7).
- `tests/` — offline behavior tests; both workflows run them before touching anything.
- `research/` — reference scripts for signals under evaluation (not live).

Live pages (GitHub Pages): <https://wahgor2050.github.io/BTC123/btc_demo/> (baseline) and
<https://wahgor2050.github.io/BTC123/btc_demo/lab/> (forward lab).

## Pausing a lab candidate (without touching its history)

Actions → "BTC Forward Lab (Paper)" → Run workflow → put the strategy id
(`sma_1d_30d_v25_v1` or `rvol2_breakout24h_v25_v1`) in the `pause` input — or locally:
`python btc_forward_lab.py --pause <strategy_id>` and commit `btc_demo/lab/state.json`.
Pausing blocks **new entries only**: due exits, hourly snapshots and all archives continue,
and nothing is ever deleted. `resume` re-enables entries. Each candidate pauses
independently; the baseline bot is unaffected.

Parameters are frozen at activation. Any retune would be a new strategy id/version with a
fresh forward record — the original record is never overwritten.

Nothing here is validated trading alpha. Both lab candidates are post-hoc, thin-sample
research leads that failed (or never faced) their own pre-registered qualification bars;
the lab exists to collect prospective evidence and execution data. See script docstrings
for the full caveats before treating any number as a claim.
