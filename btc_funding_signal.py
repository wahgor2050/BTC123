#!/usr/bin/env python3
"""
================================================================================
  Funding z-score signal for forward-lab candidate C — PAPER ONLY
================================================================================
  Live counterpart of the frozen research signal in
  research/funding_extreme_timing_btc.py, for the forward lab's candidate
  funding_z168_limit_entry_v1 (spec: btc_demo/lab/funding_candidate_spec.md).

  The math is copied VERBATIM from the research script (settlement_zscores)
  and the bar mapping follows the same rule: a bar reads the z of the newest
  settlement at or before its OPEN timestamp; newer settlements that may
  already be knowable in live trading are deliberately NOT used — freshening
  the mapping would be a different strategy than the one researched.

  Data source is PINNED to Kraken Futures (PF_XBTUSD, hourly settlements).
  btc_basis.fetch_funding()'s "longest history wins" fallback is NOT used:
  an 8-hourly venue would silently turn the 168-settlement window from ~7
  days into ~56 days, i.e. a different strategy.

  Dependencies: Python 3.9+ standard library only (via btc_basis).
================================================================================
"""
import logging
import math

import btc_basis

logger = logging.getLogger("btc-funding-signal")

# Frozen constants — mirror research/funding_extreme_timing_btc.py and the
# pre-registered spec. Changing any of these is a new strategy version.
Z_WINDOW = 168            # settlements (~7 days at 24/day), trailing, excl. current
Z_ENTER = -2.0            # enter signal at or below
Z_EXIT = 0.0              # exit signal at or above
STALE_LIMIT_S = 6 * 3600  # signal None if newest settlement > 6h older than bar open
EXPECTED_PER_DAY = 24     # Kraken settles hourly — hard requirement
MIN_SETTLEMENTS = Z_WINDOW + 1


def settlement_zscores(rates, window=Z_WINDOW):
    """z[k] = (rates[k] - mean(rates[k-window:k])) / samplestd(rates[k-window:k]).
    Strictly causal: the window ends at k-1, never contains k.  None until
    `window` real prior observations exist, or if the window std is
    degenerate (< 1e-12).  Copied verbatim from
    research/funding_extreme_timing_btc.py."""
    n = len(rates)
    z = [None] * n
    for k in range(window, n):
        win = rates[k - window:k]
        m = sum(win) / window
        var = sum((x - m) ** 2 for x in win) / (window - 1)
        sd = math.sqrt(var)
        if sd < 1e-12:
            continue
        z[k] = (rates[k] - m) / sd
    return z


def fetch_kraken_funding():
    """Live Kraken funding settlements for the forward lab.

    Returns {"f_times": [s-epoch...], "f_rates": [...], "f_z": [...],
             "newest_settlement_ts": int} on success, or None on ANY failure
    (network error, wrong settlement cadence, too few rows). A failure is
    data weather, never a fabricated signal — the caller decides what the
    frozen spec says to do about it.
    """
    kraken_only = [s for s in btc_basis.SOURCES if s[0] == "kraken"]
    try:
        rows, per_day, source = btc_basis.fetch_funding(sources=kraken_only)
    except Exception as exc:  # noqa: BLE001 — weather, logged, never invented
        logger.warning("Funding fetch failed: %s", str(exc)[:120])
        return None
    if per_day != EXPECTED_PER_DAY:
        logger.warning("Funding source %s settles %d/day, expected %d — refusing "
                       "(a different cadence is a different strategy).",
                       source, per_day, EXPECTED_PER_DAY)
        return None
    if len(rows) < MIN_SETTLEMENTS:
        logger.warning("Funding source %s returned only %d settlements (<%d) — refusing.",
                       source, len(rows), MIN_SETTLEMENTS)
        return None
    f_times = [t // 1000 for t, _ in rows]           # ms -> s epoch
    f_rates = [r for _, r in rows]
    return {"f_times": f_times, "f_rates": f_rates,
            "f_z": settlement_zscores(f_rates),
            "newest_settlement_ts": f_times[-1]}


def z_at(bar_open_ts, f_times, f_z, stale_limit=STALE_LIMIT_S):
    """(z, settlement_ts) readable by a bar that OPENED at bar_open_ts, or
    (None, settlement_ts_or_None) when no fresh-enough settlement exists.
    Same mapping as the research: newest settlement at or BEFORE the bar
    open; stale (> stale_limit) or missing -> None."""
    import bisect
    j = bisect.bisect_right(f_times, bar_open_ts) - 1
    if j < 0:
        return None, None
    if bar_open_ts - f_times[j] > stale_limit:
        return None, f_times[j]
    return f_z[j], f_times[j]
