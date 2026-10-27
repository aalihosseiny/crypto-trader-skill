---
name: crypto-pnl
description: >-
  Profit and loss for a crypto portfolio. Keeps cost-basis lots for every coin
  from the fills crypto-broker records (paper or live) and trades the user
  enters by hand, then reports unrealized and realized P&L per coin in toman
  or USDT, open lots, what a sale would realize (FIFO or highest-cost-first),
  and which coins sit on a loss. Rebuilds crypto/portfolio.json holdings from
  the trade history. Use when the user says "سود و زیان", "how much am I up",
  "what's my P&L", "cost basis", "میانگین خرید", "average buy price", "what
  would selling cost me", "which coins are losing", or records a trade made
  outside the pack.
argument-hint: "[report | add buy|sell COIN QTY PRICE | lots | whatif sell COIN QTY | losers | sync | cash N]"
---

# crypto-pnl

## Steps

1. Trades made outside the pack (on the exchange app, another exchange, a
   friend): record each one with its real price and fee:
   `python3 crypto-pnl/pnl.py add buy BTC 0.01 6800000000 --fee 0.25% --at 2026-09-14`
   Toman by default; add `--quote USDT` for tether trades. Ask for the date and
   fee if the user does not give them; never invent a price.
2. Refresh prices: `python3 crypto-alerts/alerts.py prices COINS...`.
3. `python3 crypto-pnl/pnl.py report` (add `--method hifo` to compare).
   Lead with total unrealized and realized per quote currency, then the coins
   that carry most of it.
4. Before a sale: `pnl.py whatif sell ETH 0.5` shows proceeds, basis and P&L.
5. `pnl.py sync` rebuilds holdings in `crypto/portfolio.json` from the trades;
   `pnl.py cash N` sets the cash line.

## Notes

- Toman and USDT positions are never added together. If the user wants one
  number, convert with the USDT/IRT rate from crypto-alerts and say the rate used.
- A toman P&L mixes the coin's move with the tether rate's move; when the user
  asks why they are up in toman but down in dollars, that is the reason.
- The tool applies no tax rules of any country: this is bookkeeping, not tax
  advice. For tax questions, point the user to a tax professional. Reply in the
  user's language.
