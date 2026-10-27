---
name: crypto-budget
description: >-
  Budget manager for crypto. Sizes every buy to the user's budget in toman or
  USDT before anything is bought: keeps a cash buffer, caps any one coin (the
  excess is shared out to the others), drops slices under the exchange minimum
  (Nobitex 300,000 toman or 11 USDT), rounds to whole toman, and writes the
  proposed buys to crypto/orders.json. Also owns crypto/config.json, the rules
  every other skill follows. Use when the user says "۵۰ میلیون تومان دارم",
  "I have 1000 USDT", "how much of each should I buy", "بودجه", "split my
  money", "keep 10% cash", "no coin over 25%", "set my rules", or after
  crypto-whales or crypto-ta writes targets.
argument-hint: "--budget AMOUNT [--quote IRT|USDT] [--cash-buffer 10] [--cap 25] [--write]"
---

# crypto-budget

## First run: the rules

If `crypto/config.json` does not exist, copy `crypto-budget/config.example.json`
there and walk the user through the few lines that matter, in plain words:

- `exchange`: `nobitex` or `wallex`; `quote`: `IRT` (toman) or `USDT`
- `cash_buffer_pct` (10): cash that is never spent
- `max_position_pct` (25): the most any one coin can be
- `max_order`: the biggest single order allowed
- `blocked` / `allowed`: coins never to buy / the only coins to buy
- `mode`: stays `paper` until the user explicitly asks for real trading

Show the result with `python3 crypto-budget/size.py rules`.

## Steps

1. Targets must exist in `crypto/targets.json` (from crypto-whales `copy`,
   crypto-ta `scan --targets`, or written by hand like `targets.example.json`).
2. Prices must be fresh: `python3 crypto-alerts/alerts.py prices COIN1 COIN2 ...`
   (add `--quote USDT` for tether markets).
3. Dry run: `python3 crypto-budget/size.py --budget 50000000`. Read the amounts
   back to the user in toman with separators and confirm the budget's unit when
   it is ambiguous ("50" probably means 50 million toman, not 50 toman; ask).
4. When the user is happy: run again with `--write`. The orders are PROPOSALS.
5. Hand over to **crypto-risk**. Never skip it.

## Notes

- A small budget plus many coins means slices under the minimum get dropped;
  suggest fewer coins rather than lowering the safety rules.
- With a 25% cap and fewer than 4 coins, part of the money stays idle in cash by
  design; say so.
- Not financial advice. Reply in the user's language.
