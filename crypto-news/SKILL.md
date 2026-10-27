---
name: crypto-news
description: >-
  The crypto news desk. New headlines for the coins the user holds (CoinDesk,
  Cointelegraph, Decrypt, The Block and the Persian ArzDigital), tagged by what
  usually moves price (hack, regulation, ETF, listing, unlock, macro,
  liquidation, whale, upgrade); market mood from the Fear & Greed index, BTC
  dominance and perpetual funding rates; and a calendar of upcoming token
  unlocks. Shows only what is new since the last check. Use when the user says
  "اخبار کریپتو", "any news on my coins", "what's happening with Solana",
  "fear and greed", "ترس و طمع", "funding rate", "token unlock", "آنلاک توکن",
  "market sentiment", or "morning briefing".
argument-hint: "[briefing | headlines | sentiment | unlocks] [--coins BTC ETH] [--hours 24]"
---

# crypto-news

## Steps

1. Coins: from `--coins`, otherwise from `crypto/portfolio.json` holdings.
2. Full picture: `python3 crypto-news/news.py briefing --coins BTC ETH SOL`.
   Or one part at a time:
   - `headlines --coins ... --hours 24` (add `--all` for every story, `--peek`
     to look without marking items as seen)
   - `sentiment --coins ...`
   - `unlocks --days 30`
3. Token unlocks have no free reliable API. When the user holds or plans to buy
   a coin with vesting (most new L1/L2s and launchpad tokens), search the web for
   "<coin> token unlock schedule", and record each unlock with its source:
   `news.py unlocks add ARB 2026-11-16 --pct 2.1 --usd 95000000 --source URL`.
   Never add an unlock without a source link; tell the user when the date is an
   estimate.

## How to report

- Lead with what touches the user's holdings (lines marked `*`), then tags
  `hack` and `regulation` before the rest. One line each: what happened, which
  coin, why it may matter, the link.
- Mood: Fear & Greed value and its direction over the week; funding above
  +0.05% per 8h means longs are crowded, below -0.02% means shorts are crowded.
  These are context, not signals.
- Unlocks of 1% of supply or more within 14 days are worth a mention for a coin
  the user holds: new supply often meets selling.
- Do not invent news. If a feed failed, say which one.

## Schedule

Offer a scheduled task each morning that runs `news.py briefing` and sends only
the items marked `*`, the mood line and the unlocks inside 7 days.

## Notes

Headlines come from third-party RSS feeds; check anything important at the
source before acting on it. Not financial advice. Reply in the user's language.
