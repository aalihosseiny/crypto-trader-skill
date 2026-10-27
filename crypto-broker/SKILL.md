---
name: crypto-broker
description: >-
  Order executor for Nobitex and Wallex. Turns orders that passed crypto-risk
  into exact exchange tickets (limit orders with a price guard), shows each one
  in plain words, and places it only after the user's explicit yes for that
  exact ticket, recorded word for word. Paper trading by default; Nobitex
  testnet and live trading only when the user sets them up, with API keys in
  environment variables, never in chat. Also checks order status, cancels,
  and reads live balances into crypto/portfolio.json. Use when the user says
  "place the orders", "buy it", "بخر", "سفارش بذار", "execute", "send to
  Nobitex", "connect Wallex", "set up paper trading", "cancel my order", or
  after crypto-risk passes.
argument-hint: "[show | approve ID --words TEXT | place ID --confirm ID | status ID | cancel ID | balances | paper-init --cash N | mode]"
---

# crypto-broker

Real money can move from here, so the steps are fixed and none of them is
skipped or merged.

## The rule

**One order, one ticket, one yes.** Claude never approves on its own, never
treats "go ahead with everything" as a yes for an order it has not shown, and
never places an order the user did not answer about after seeing its ticket.

## Steps

1. `python3 crypto-broker/broker.py mode`: say which mode is active (PAPER,
   TESTNET or LIVE) and the exchange. Every message about orders starts with it.
2. `python3 crypto-broker/broker.py show`: only READY orders can go further.
   For BLOCKED / NOT CHECKED / CHANGED, go back to crypto-risk.
3. For ONE ready order, show the ticket in plain words, for example:
   "PAPER · BUY 0.000142 BTC at most 7,045,000,000 toman (about 1,000,000
   toman), LIMIT on Nobitex. Reply yes to place this order."
4. Wait for the user's reply. Only a clear yes for that order counts. Then
   record it exactly:
   `broker.py approve o1 --words "<the user's reply, verbatim>"`
5. Place it: `broker.py place o1 --confirm o1`. The tool refuses if the
   approval is older than 10 minutes, the order changed, the price went stale,
   or the order was already sent; follow its message and do not work around it.
6. Report the result, then move to the next order and repeat from step 3.

## Modes

- **paper** (default): fills are simulated at the current bid/ask; start with
  `broker.py paper-init --cash 50000000`. Nothing leaves the computer.
- **testnet** (Nobitex only): the user makes an account at testnet.nobitex.ir,
  creates an API token there, and sets `NOBITEX_TESTNET_TOKEN` in their shell.
- **live**: only when the user asks for real trading in so many words. Then:
  1. They create an API key in the exchange's panel (Nobitex: profile → API;
     Wallex: account → API), with **trading only, no withdrawal permission**,
     and IP-restricted if the exchange offers it.
  2. They set it in their own terminal, not in the chat:
     `export NOBITEX_TOKEN=...` or `export WALLEX_API_KEY=...`.
  3. They change `"mode": "live"` in `crypto/config.json` themselves, or tell
     Claude to, after Claude has repeated: "real orders with real money".
  If a user pastes a key into the chat, tell them to revoke it and make a new
  one; never write a key to a file.
- Nobitex accepts API calls only from Iranian IPs; a cloud session cannot
  reach it. Live and testnet trading run on the user's own computer.

Leverage (long / short on margin) is not this skill: use **crypto-margin**.

## After placing

- `broker.py status o1` shows the exchange's view (filled, partly filled,
  open). `broker.py cancel o1` cancels an open order.
- `broker.py balances` (live) writes the real wallet to
  `crypto/portfolio.json`; follow with `crypto-pnl` for profit and loss.
- An order marked IN FLIGHT was sent but no answer was recorded: check its
  status on the exchange before anything else; it is never re-sent.

## Notes

Limit orders can sit unfilled if the market runs away; that is the price guard
working. Not financial advice. Reply in the user's language.
