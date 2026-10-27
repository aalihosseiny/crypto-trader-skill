---
name: crypto-ta
description: >-
  Technical analysis and a rule-based scanner for crypto. Reads candles from
  Nobitex (toman or USDT markets) or Binance (USDT pairs) and computes EMA
  20/50/200, RSI, MACD, Bollinger bands, ATR, volume surges and swing support /
  resistance, then sums them into a score from -100 to +100 with every reason
  named. The scanner checks a list of coins against the user's own rules, ranks
  them, and can write the top N to crypto/targets.json. Use when the user says
  "تحلیل تکنیکال", "analyze BTC", "RSI of ETH", "is SOL overbought",
  "support and resistance", "scan these coins", "find oversold coins",
  "which coins are in an uptrend", or "pick coins by chart".
argument-hint: "[analyze COIN | scan COIN... --rules FILE --targets N] [--tf 1d|4h|1h|15m] [--quote IRT|USDT]"
---

# crypto-ta

Describes the chart in numbers. It does not predict prices, and the score is a
transparent sum of named reasons, not a model.

## Steps

1. Pick the market: toman (`--quote IRT`, default) or tether (`--quote USDT`).
   Timeframe `--tf 1d` by default; `4h`, `1h`, `15m` for shorter views.
   Candles come from Nobitex. For USDT pairs the user can use `--source binance`
   (public data endpoint), which has longer and deeper history.
2. One coin: `python3 crypto-ta/ta.py analyze BTC [--tf 4h] [--json]`.
3. Many coins: write the user's rules to `crypto/ta_rules.json`, then
   `python3 crypto-ta/ta.py scan BTC ETH SOL ... --rules crypto/ta_rules.json`.
   Known rules: `min_score`, `max_score`, `rsi_min`, `rsi_max`, `above_ema200`,
   `above_ema50`, `macd_positive`, `min_change_7`, `max_change_7`,
   `min_change_30`, `max_atr_pct`, `min_volume_ratio`. Example in
   `ta_rules.example.json`. Results go to `crypto/signals.json`.
4. To build a portfolio from the scan: add `--targets N` (equal weights of the
   top N that pass), then hand over to crypto-budget.

## How to explain the result

- Lead with the bias (bullish / neutral / bearish) and the 2-3 reasons that
  carry the most points. Then levels: nearest support and resistance, and the
  2xATR stop hint as a place where the idea would be wrong.
- Name the timeframe. A daily uptrend can sit inside a 4h pullback.
- Small or new coins with few candles give unreliable indicators (EMA200 needs
  200 bars); say so when `bars` is low or a value is `-`.

## Notes

- Toman prices move with both the coin and the USDT/toman rate. When a toman
  chart and a USDT chart disagree, the difference is the tether rate; say so.
- Not financial advice. Indicators lag; they summarise what already happened.
- Reply in the user's language.
