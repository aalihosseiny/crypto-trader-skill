---
name: crypto-whales
description: >-
  The whale tracker and copier. Reads any Ethereum wallet (ETH plus every
  ERC-20, priced in USD, via Ethplorer) or Bitcoin address (via mempool.space),
  watches a list of big wallets and reports what they bought, added, trimmed
  and sold since the last check, and turns one wallet or a blend of several
  into copy weights in crypto/targets.json, keeping only coins the user's
  exchange lists. Use when the user says "کپی نهنگ", "copy this wallet",
  "what does this whale hold", "track this address", "whale alert",
  "what did the smart money buy", "follow Vitalik's wallet", or pastes a 0x...
  or bc1... address.
argument-hint: "[eth|btc ADDRESS | watch add ADDRESS --label X | check | copy ADDRESS... --top N]"
---

# crypto-whales

The crypto version of copying a fund's 13F. On-chain balances are public and
current, so copies are fresher than a 13F. Wallets, though, have no legal
labels, and that is the main risk.

## Steps

1. **Find the wallet.** If the user names a person or fund rather than giving an
   address, search the web for its public, labelled address (Etherscan labels,
   Arkham, the fund's own disclosures, ENS names). Show the address and the
   source of the label, and get the user's OK before using it. Never guess an
   address.
2. **Look:** `python3 crypto-whales/whales.py eth 0x...` or `btc bc1...`.
3. **Check the label.** If the holdings look like an exchange (hundreds of
   tokens, huge stablecoin balances, constant in/out flow), a bridge or a
   custodian, tell the user that copying it copies customers' deposits, not one
   investor's decisions.
4. **Watch:** `watch add ADDRESS --label "..."`. Then
   `check --min-usd 100000` diffs every watched wallet against its last
   snapshot (NEW, added, trimmed, SOLD OUT, with USD size).
5. **Copy:** first make sure `crypto/prices.json` lists the exchange's markets
   (`python3 crypto-alerts/alerts.py prices --all`), then
   `python3 crypto-whales/whales.py copy ADDRESS [ADDRESS2 ...] --top 5`.
   Stablecoins are removed by default (`--keep-stables` keeps them); wrapped
   and staked forms count as the coin (WETH, stETH -> ETH; WBTC -> BTC).
   Coins the exchange does not list are skipped and named.
6. Hand over to **crypto-budget** to size the copy to the user's money.

## How to report

A short table: coin, target %, share of the wallet, change versus the last copy.
Then what was skipped and why. For `check`: lead with NEW and SOLD OUT, then the
biggest adds and trims in USD, marking coins the user holds.

## Notes

- Ethplorer's free key is rate-limited; set `ETHPLORER_KEY` for heavy use.
  Bitcoin addresses show one address, not the owner's whole stack.
- Tokens without a market price are listed at no value and never copied.
- A whale can be wrong, can be hedged elsewhere (derivatives, other chains,
  exchanges), and moves before you see it. Copying a wallet does not copy its
  results. Not financial advice. Reply in the user's language.
