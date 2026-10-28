---
name: crypto-daily
description: >-
  The once-a-day leveraged trading routine on Nobitex for a user who is not a
  trader. Reports yesterday's closed positions, makes sure every open position
  has an OCO (take-profit + stop-loss), scans every margin market on the daily
  and 4h charts, checks news, funding, Fear & Greed and token unlocks, and
  proposes at most a few trades, each with entry, stop-loss, take-profit,
  reward-to-risk after fees, estimated days and liquidation price, ranked
  fastest and richest first. Shows ONLY a table; after the user's yes it opens
  the approved rows and puts an OCO on each as soon as it fills. Use when the
  user says "/crypto-daily", "روال روزانه", "معامله امروز", "امروز چی
  بخرم", "daily trades", "find me trades", "بهترین معامله‌ها رو پیدا کن", or
  wants trades found and opened without picking coins themselves.
argument-hint: "[--margin 300000] [--leverage 2] [--max-new 2]"
---

# crypto-daily

One run a day; between runs every position is protected by an OCO, so the user
does not need to watch the screen. The plan describes the charts and sizes the
risk; it does not know the future. Say that once, plainly, in the first run of
a conversation, not every day.

Uses: crypto-margin (markets, wallet, positions, open, oco, approve, place,
cancel-order), crypto-alerts (prices), crypto-ta (through daily.py),
crypto-news (headlines, sentiment, unlocks), crypto-backtest (the odds).

## Backtest first

If `crypto/backtest_stats.json` does not exist or is older than 30 days, say
so at the top and offer to run crypto-backtest (fetch + walkforward) before
trading. If the last backtest says the rules have no proven edge (the range
of the average R reaches below zero), repeat that in one line above the table
every day, so the user decides with it in view.

## Defaults (the user can change them in words)

margin 300,000 toman per trade · leverage 2 · at most 2 new trades a day ·
at most `max_open_positions` open (config, default 3) · reward-to-risk after
fees at least 1.8 · liquidation at least 10% of the price beyond the stop.
Never go above `max_leverage` / `max_margin` in crypto/config.json; if the user
asks for more, they change the config first (see crypto-margin).

## Step 1: yesterday and today's book

1. `python3 crypto-margin/margin.py positions` and
   `python3 crypto-margin/margin.py wallet`.
2. Positions that closed since the last run (status Closed / Stopped /
   Liquidated, or gone from the open list): report each in one line, its P&L in
   toman, and whether the target or the stop was hit.
3. Any open position WITHOUT an OCO: propose one first (stop and take from the
   last plan in `crypto/daily_plan.json` if it is there, else 1.5 daily ATR
   stop and 2R target), in the same table as today's trades.
4. If open positions already equal `max_open_positions`, skip to the report:
   "no room for new trades today".

## Step 2: the plan

1. Fresh data: `python3 crypto-margin/margin.py markets`, then prices for the
   markets it lists (`python3 crypto-alerts/alerts.py prices --all` and
   `--all --quote USDT`).
2. `python3 crypto-daily/daily.py plan --margin 300000 --leverage 2 --max-new 2 --exclude <coins already open>`
   (`--quote IRT` if the user only has toman in the margin wallet; longs are
   usually on USDT markets). `--margin` is toman; tether markets use
   `--margin-usdt` (default: the same money at the USDT rate). Nobitex needs at
   least 11 USDT of position, so small toman budgets cannot trade USDT markets:
   say so when those rows are skipped. Settings come from
   `crypto/strategy.json` if a walk-forward result was adopted.
3. Filter the picks with the news desk:
   `python3 crypto-news/news.py headlines --coins <picked coins> --hours 24 --peek`,
   `news.py sentiment --coins <picked>`, `news.py unlocks --days 7`.
   Drop a pick with a hack, delisting or regulation headline about that coin,
   or an unlock of 1% of supply or more inside 7 days. Note funding: do not open
   a long when funding is above +0.05% (longs crowded) or a short below -0.02%.
   If the user holds only toman in the margin wallet and a pick is a USDT
   market, say it needs tether moved first, or drop it.
4. Margin wallet must cover every row (margin + fees); if not, list fewer rows
   and say why.

## Step 3: the table, and nothing else

Write the table in the user's language, one row per trade, in rank order:

| # | coin | long/short | entry | stop-loss | take-profit | R:R | ~days | liquidation | max loss (toman) | profit at target (toman) | chance (backtest) | why (one line) |

"chance (backtest)" = how often similar past trades closed in profit, with
its 90% range and the number of trades, next to the break-even rate for that
R:R, e.g. "44% (37-51%, n=140), needs 36%". Write "too few" when daily.py says
so, and "no backtest" when there is none. Never present it as a promise.
Drop or flag a row whose setup has 30+ past trades and an average result
below zero.

Under it, one line: the total margin used and the total worst-case loss. If
nothing passed: "no trade today" and the main reason (e.g. no clear trend,
fees too high for the targets, news). Then stop and wait. Place nothing.

## Step 4: after the user's yes

- "yes" / "بله" approves every row; "بله ۱" or "۱ و ۳" approves those rows
  only; anything else is a no.
- The yes covers BOTH the opening order and the OCO of each approved row, with
  the exact prices in the table. If prices moved so the entry is more than 1%
  away, rebuild that row and ask again instead.
- For each approved row, in rank order:
  1. refresh its price, `margin.py open SIDE COIN --margin M --leverage L [--quote USDT]`
  2. `margin.py approve mN --words "<the user's reply, verbatim>"`, then
     `margin.py place mN --confirm mN`
  3. check `margin.py positions` every minute or so until the position appears
     (filled); if it has not filled within 15 minutes, cancel the order
     (`margin.py cancel-order ID --confirm ID`) and report it
  4. at once: `margin.py oco P_ID --stop STOP --take TAKE`, approve with the
     same words, place. If Nobitex refuses (`ExceedLiability`), an earlier close
     order holds the amount: cancel it and place the OCO immediately.
  5. if the OCO is refused for another reason, place a plain stop-loss
     (`margin.py protect`) so the position is never left unprotected, and tell
     the user.
- Finish with a short summary: what is open, its stop, its target, its
  liquidation price, and the total at risk.

## Rules

- Never more trades, more leverage or more margin than the table showed.
- Never move a stop further away, and never open a trade to win back a loss,
  unless the user asks for that exact change.
- A blocked risk check stays blocked; explain it, do not work around it.
- Offer once (not daily) a scheduled task that runs `margin.py positions` every
  few hours and messages the user only if a position lost its OCO or is within
  15% of liquidation.
- Not financial advice. Reply in the user's language.
