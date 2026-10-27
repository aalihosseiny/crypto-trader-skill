---
name: crypto-alerts
description: >-
  Prices and price alerts for crypto on the Iranian exchanges Nobitex and
  Wallex. Fetches live last / bid / ask / 24h change for toman (IRT) and tether
  (USDT) markets, the USDT rate in toman, and writes crypto/prices.json that
  every other crypto skill reads. Alerts ring once: above a target, below a
  level, a stop, or a 24h move of N percent. Use when the user says "price of
  bitcoin", "قیمت بیت کوین", "قیمت تتر", "alert me when BTC hits", "هشدار قیمت",
  "set a stop", "watch my coins", "get prices", or before sizing, risk,
  rebalancing or P&L.
argument-hint: "[prices COIN... | add COIN above|below|stop|move LEVEL | check]"
---

# crypto-alerts

The price feed for the whole pack. Crypto trades 24/7, so there is no market
clock: what matters is how fresh the price is, and crypto-risk refuses orders on
prices older than `max_price_age_min` (15 minutes by default).

## Units (read this once)

- Toman markets (`BTCIRT`) are stored in **toman** for both exchanges.
  Nobitex's API answers in **rial**; the tool divides by 10 when reading.
  Wallex's `TMN` markets are already toman.
- Tether markets (`BTCUSDT`) are in USDT.
- Exchange minimums are recorded per market (`min_notional`): Nobitex is
  300,000 toman (3,000,000 rial) and 11 USDT; Wallex publishes its own per market.

## Steps

1. Read `crypto/config.json` if it exists for the exchange (default `nobitex`).
   The user can override with `--exchange wallex`.
2. Prices: `python3 crypto-alerts/alerts.py prices BTC ETH SOL` (toman) or add
   `--quote USDT`. `--all` fetches every market of that quote, which crypto-whales
   needs to know what is tradable. USDT/IRT is added automatically to toman runs.
3. Alerts: `add COIN KIND LEVEL [--quote USDT] [--note TEXT]` with KIND one of
   `above`, `below`, `stop`, `move` (move = absolute 24h change in percent).
   Always repeat the level back to the user in toman with thousands separators,
   and ask if they meant toman when a number looks like rial (10x too big) or
   dollars (far too small for a toman market).
4. `check --refresh` fetches only the markets that have armed alerts, rings each
   alert once, and saves the state. `reset ID` re-arms one.
5. For a recurring watch, offer a scheduled task that runs
   `alerts.py check --refresh` every 15-60 minutes and reports only RING lines.

## Output

Show prices in a small table: market, last, bid, ask, 24h %. Toman with
thousands separators, never in rial. Say which exchange and when.

## Notes

- Nobitex requires requests from an Iranian IP. If a call fails from abroad or
  from a cloud machine, say so plainly and suggest running on the user's own
  computer in Iran, or switching to `--exchange wallex`.
- Prices are public market data, not advice. A stop alert only rings; it does not
  sell. Placing a real stop order is crypto-broker's job, after crypto-risk.
- Reply in the user's language (Persian if they write Persian).
