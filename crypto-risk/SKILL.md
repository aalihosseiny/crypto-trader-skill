---
name: crypto-risk
description: >-
  Risk manager for crypto orders. Checks every proposed order in
  crypto/orders.json against the user's limits before it can reach Nobitex or
  Wallex, and blocks anything over them: blocked or not-allowed coins,
  leverage / margin / futures (off by default), minimum and maximum order size,
  stale prices, wide bid/ask spreads, fat-finger limit prices, selling more
  than held, a coin over the position cap, breaking the cash buffer, duplicates
  and a daily order limit. Prints PASS or BLOCK with the reason and stamps each
  order so crypto-broker only accepts what passed, unchanged. Use when the user
  says "check my orders", "is this safe", "ریسک", "does this break my rules",
  "can I place this", and always before crypto-broker.
argument-hint: "[check | limits] [--orders FILE]"
---

# crypto-risk

The gate between a proposal and the exchange. It runs before every placement,
including when the user is in a hurry.

## Steps

1. Refresh prices first if they are older than a few minutes:
   `python3 crypto-alerts/alerts.py prices COINS...` (orders on stale prices are
   blocked anyway).
2. `python3 crypto-risk/check.py`. Exit code 1 means at least one BLOCK.
3. Report every order with its verdict. For each BLOCK, say the reason in plain
   words and the smallest fix (a smaller amount, fresh prices, remove a
   duplicate). Do not change the user's limits to make an order pass unless the
   user asks for that limit to change, and then repeat the new limit back.
4. If anything changes in an order after the check, run the check again; the
   broker compares a hash and refuses changed orders.
5. Only PASS orders go to **crypto-broker**.

## Notes

- `python3 crypto-risk/check.py limits` prints the limits in force.
- This skill checks spot orders. Leveraged positions go through
  **crypto-margin**, which has its own checks (max leverage, max margin, open
  positions, distance to liquidation) and needs `allow_leverage: true`.
- Reply in the user's language.
