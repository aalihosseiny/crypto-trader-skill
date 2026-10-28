---
name: crypto-backtest
description: >-
  Backtests the crypto-daily margin rules on Nobitex history without fooling
  the user: no look-ahead (decisions see only closed candles, entries at the
  next 4h open, stop counted first when a bar touches both levels, gaps fill at
  the open, liquidation, 30-day expiry, all fees), a walk-forward test over a
  deliberately small parameter grid, confidence ranges on every number, and a
  rule that only adopts new settings when they beat the defaults out of sample
  and their grid neighbours agree. Saves the historical odds that crypto-daily
  prints next to every proposed trade. Use when the user says "بک تست",
  "backtest", "آیا این استراتژی جواب می‌ده", "does this strategy work",
  "win rate", "درصد شانس", "چند درصد احتمال سود", "optimize the settings",
  "overfitting", or before trusting crypto-daily with real money.
argument-hint: "[fetch --days 1095 | run | walkforward | adopt | report | odds SIDE MARKET SCORE]"
---

# crypto-backtest

## What it answers

"If I had followed the crypto-daily table every day in the past, what would
have happened?" Trades closed in profit X% of the time (with a range), the
average trade made Y R (1R = the planned loss of a trade), the worst run of
losses was Z R. Those odds then appear in the daily table.

## Automatic use

crypto-daily runs `backtest.py ensure --auto-adopt` as its first step every
day: nothing happens while the backtest is under 30 days old; otherwise it
fetches, runs the walk-forward, adopts or withdraws settings only by the
recommendation, and prints a VERDICT line. The steps below are for running it
by hand or explaining it in depth.

## Steps

1. Markets: `python3 crypto-margin/margin.py markets` (needs the Iranian IP,
   like every Nobitex call).
2. History: `python3 crypto-backtest/backtest.py fetch --days 1095` (three
   years of daily and 4h candles, cached in `crypto/history/`; later runs only
   add new candles). Add `--quote IRT` if the user trades toman markets only.
3. The rules as they are: `backtest.py run`. Writes the trade list
   (`crypto/backtest_trades.csv`) and, because the defaults were set before any
   test, saves honest odds for crypto-daily.
4. Would other settings be better? `backtest.py walkforward`. Train 365 days,
   test the next 90, slide, repeat; only test windows count. It prints both the
   walk-forward result and the fixed defaults on the same unseen windows, the
   whole grid as a robustness map, and a RECOMMENDATION.
5. Only if the recommendation says adopt, and the user agrees:
   `backtest.py adopt`. crypto-daily then uses those settings
   (`crypto/strategy.json`). Never use `adopt --force` unless the user insists
   after hearing why it was not recommended.
6. Re-run monthly (fetch + walkforward): markets change.

## How to explain the result (in the user's language, plain words)

- Lead with the out-of-sample numbers, never the in-sample grid.
- Win rate is not enough: compare it with the break-even rate for the target
  (a 1.8R target breaks even at 36% after fees). The average R and its range
  decide: if the range of the average R includes zero or below, say "no proven
  edge", even if the average is positive.
- Under 30 trades: "too few to judge"; give no percentage as if it were solid.
- Show the worst drawdown in R and in toman (R x the planned loss per trade),
  and the longest losing streak: that is what the user must be able to sit
  through.
- If the strategy has no edge, say so plainly and suggest not trading it with
  real money (or smaller), rather than searching for settings until something
  looks good. That search is the overfitting this skill exists to prevent.
- Offer a chart of the equity curve from the CSV (cumulative R by exit date)
  if the user wants to see it.

## Limits to state once

- Past results do not promise future ones; crypto regimes change.
- 4h bars hide what happened inside them; the stop-first rule makes results
  slightly pessimistic, which is the safe side.
- The backtest cannot see news, so the news filter of crypto-daily is not in
  it; live results may differ either way.
- Nobitex history for small coins is short; those markets add few trades.
- Not financial advice.
