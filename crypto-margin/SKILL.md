---
name: crypto-margin
description: >-
  Leveraged trading on Nobitex (معاملات تعهدی): long or short with 1x to the
  market's maximum leverage, one ticket and one yes at a time. Lists which
  markets allow long or short and their max leverage, moves collateral between
  the spot and margin wallets, opens positions with a price guard, shows every
  open position with its LIQUIDATION price and how far away it is, places
  stop-loss orders, and closes positions. Paper mode simulates all of it,
  including liquidation. Own risk rules: max leverage, max margin per position,
  max open positions, minimum distance to liquidation. Use when the user says
  "اهرم", "لوریج", "leverage", "تعهدی", "margin", "short", "شورت", "لانگ با
  اهرم", "liquidation price", "قیمت لیکوئید", "close my position", "حد ضرر
  بذار", or "funding a margin wallet".
argument-hint: "[markets | wallet | positions | transfer N --to margin | open long|short COIN --margin N --leverage L | close ID | protect ID --stop P | show | approve | place]"
---

# crypto-margin

Leverage multiplies losses as much as gains. With isolated margin the most a
position can lose is its margin (collateral), but that can happen fast:
at 2x a 45% move against a long liquidates it, at 5x about 12%.

## Before the first leveraged trade

1. The user must set `"allow_leverage": true` in `crypto/config.json`
   themselves, or tell Claude to, after Claude has said in one line: "leverage
   can lose the whole margin of a position, quickly." Then confirm the limits:
   `max_leverage` (default 2), `max_margin` per position (1,000,000 toman /
   50 USDT), `max_open_positions` (3), `min_liq_distance_pct` (15).
2. Margin trading may need to be activated in the Nobitex panel and needs a
   sufficient verification level; the error `FeatureUnavailable` or
   `TradeLimitation` means exactly that.
3. `python3 crypto-margin/margin.py markets` shows which side each market
   allows. **On Nobitex, toman markets have often allowed only shorts; longs are
   on USDT markets.** Check before proposing, and say so if the user asks for a
   toman long that is not enabled.

## Steps

1. Fresh prices: `python3 crypto-alerts/alerts.py prices BTC` (add
   `--quote USDT` for tether markets).
2. Collateral: `margin.py wallet`; if needed
   `margin.py transfer 1000000 --to margin --confirm 1000000` after the user
   agrees to move that amount (toman by default, `--quote USDT` for tether).
3. Propose: `margin.py open short BTC --margin 500000 --leverage 2`. The tool
   runs the margin risk check and prints PASS or BLOCK with reasons.
4. Show the ticket in plain words: side, leverage, size, price limit, margin,
   **estimated liquidation price and its distance in percent**. Wait for the
   user's yes for that exact ticket.
5. `margin.py approve m1 --words "<user's reply, verbatim>"`, then
   `margin.py place m1 --confirm m1`. Approvals expire after 10 minutes.
6. Right after it fills: `margin.py positions`, and offer a stop-loss
   (`margin.py protect P_ID --stop PRICE`, same ticket / yes / approve /
   place chain). A stop must sit between the price and the liquidation price.
7. Closing: `margin.py close P_ID`, then the same chain.
8. Take-profit: `margin.py oco P_ID --stop PRICE --take PRICE` (same ticket / yes /
   approve / place chain) puts a stop-loss and a take-profit on a position, and
   whichever fires cancels the other. Nobitex builds it as a LIMIT take-profit
   plus a stop-LIMIT (the stop leg is not a market order: in a fast gap past its
   limit it may not fill, so say so when showing the ticket). Nobitex rejects an
   OCO while other close orders (such as an earlier `protect` stop) already use
   the position's amount, so cancel that stop first (`cancel-order`) and place
   the OCO straight after; the position is unprotected in between.

## Watching

- `margin.py positions` shows entry, mark, P&L, liquidation price and distance;
  under 10% it prints DANGER. Offer a scheduled task every 15-30 minutes that
  runs it and reports any position under 15% from liquidation.
- Positions expire after 30 days and pay a daily extension fee (shown).

## Rules for Claude

- Never raise `max_leverage`, `max_margin` or lower `min_liq_distance_pct`
  unless the user asks for that specific change; repeat the new value back.
- Never open a position to "win back" a loss unless the user asks for that
  exact trade.
- Keys only from `NOBITEX_API_KEY` + `NOBITEX_API_SECRET` (signed API key, Ed25519)
  or `NOBITEX_TOKEN` (login token), set in the user's own terminal. Margin needs the
  same Iranian IP as all Nobitex calls.
- The liquidation price before opening is an estimate (Nobitex liquidates at
  margin ratio 1.1); after opening, the exchange's own number is shown.
- Not financial advice. Reply in the user's language.
