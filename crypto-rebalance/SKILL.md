---
name: crypto-rebalance
description: >-
  Rebalancer for a crypto portfolio. Values every holding at current Nobitex /
  Wallex prices, works out each coin's drift from its target weight in
  percentage points, and proposes trades only for coins outside a band
  (default 5 points): sells first, buys scaled to keep the cash buffer, the
  per-coin cap respected, tiny trades skipped, coins no longer in the targets
  sold unless kept. Writes proposals to crypto/orders.json. Use when the user
  says "rebalance", "ری‌بالانس", "my portfolio drifted", "back to the
  targets", "I added money", "the whale changed its holdings", "am I
  overweight BTC", or on a monthly schedule.
argument-hint: "[--band 5] [--add AMOUNT] [--keep-others] [--write]"
---

# crypto-rebalance

## Steps

1. Holdings must be current: live users run `broker.py balances`; paper users
   already have `crypto/portfolio.json`; manual users run `crypto-pnl sync` and
   `pnl.py cash N`.
2. Fresh prices for every held and target coin:
   `python3 crypto-alerts/alerts.py prices COINS...`.
3. Targets: usually unchanged; after a new whale copy or TA scan,
   `crypto/targets.json` is already updated.
4. Dry run: `python3 crypto-rebalance/rebalance.py --band 5`. Show the drift
   table: coin, now %, target %, drift, the trade.
5. With `--write` the trades become proposals, then **crypto-risk**, then
   **crypto-broker** one ticket at a time.

## Choosing the band

Crypto is volatile: a tight band (2-3 points) trades often and pays fees and
spread every time; 5 points is a sensible default, 10 for small portfolios
where fees bite. New money (`--add`) is a cheap moment to rebalance because it
mostly needs buys.

## Notes

`--keep-others` leaves coins that are not in the targets alone (for coins the
user holds on purpose). Not financial advice. Reply in the user's language.
