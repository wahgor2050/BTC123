# BTC123 — BTC paper-trading forward lab

Paper trading only. No broker connection, no real orders, no API keys required to run.

This repo hosts a BTC paper-trading demo, migrated from a private research repo to keep
GitHub Actions minutes and git history separate from unrelated private work.

- `btc_autotrade.py` — the baseline bot: daily dual-SMA(3,30) trend + 45% volatility-target sizing.
- `btc_demo/` — paper account state, trade ledger, and the live dashboard pages.
- `.github/workflows/btc_autotrade.yml` — daily schedule that runs the baseline bot.
- `research/` — reference scripts for signals under evaluation (not yet live).

Nothing here is validated trading alpha. See individual script docstrings and
`research/` outputs for honest backtest caveats before treating any number as a claim.
