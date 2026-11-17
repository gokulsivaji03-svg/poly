# Strategy evidence: tailing a directional sharp

What the backtests say about copying a sharp wallet's directional bets, and why hedging is off by default. Every number here comes from the two scripts in `backend/scripts/`, run on 2026-11-17 against wallet `0x0224bb9eb0a5c9fd261ac9123a72cbdd5748292a`. They are reproducible with the commands below. Results describe one wallet over one period. They are evidence, not a forecast.

## The wallet

- 24,279 activity rows across 1,554 markets, 2024-09-11 to 2026-11-17.
- Over the last 30 days it made 4,404 trades, and 96% were BUYs. It mostly holds to resolution.
- Trading clusters in 15:00 to 01:00 UTC and peaks at 22:00 to 00:00 UTC. That window is esports (LoL) and evening US politics.
- The estimated starting bankroll is $192,522. That is a lower bound reconstructed from its own cash flows.

## Speed is the edge

`simulate_copy_portfolio.py` replays the whole history as one proportionally sized copy portfolio, with a $1,000 bankroll, taker fees, and settlement at the on-chain final price. Slippage is a flat premium per share over the source's fill. It stands in for what latency costs.

| premium per share | final equity | return | max drawdown | fees | slippage paid |
|---|---|---|---|---|---|
| 0¢ | $4,445 | +345% | 42% | $1,477 | $0 |
| 0.5¢ | $3,256 | +226% | 44% | $1,144 | $849 |
| 1¢ | $2,441 | +144% | 50% | $907 | $1,373 |
| 2¢ | $1,441 | +44% | 60% | $591 | $1,882 |
| 3¢ | $920 | −8% | 66% | $406 | $2,060 |
| 5¢ | $382 | −62% | 74% | $212 | $2,053 |

Reading:
- The edge is thin per trade and large in aggregate. The first cent of premium costs about 200 points of return, the second about 100. Breakeven sits near 2.8¢. Whatever reaches the order book first, at the source's price, keeps the edge.
- Returns are concentrated. One market ("US x Iran permanent peace deal by June 15, 2026?") contributed +$2,109 of the +$3,445 at 0¢. Drawdowns of 42% and more are normal.
- At $1,000 the sizing is the binding constraint. 12,736 source BUYs were skipped as `BELOW_MIN_ORDER`, because a proportional copy of a $192k wallet is often under $1. A larger bankroll or a sizing multiplier copies more of the sharp's flow.

Limits: there is no order book, so depth, queue position and market impact are missing. CONVERSION events are ignored. Source equity is a lower bound, so allocation percentages are upper bounds.

## Hedging gives the edge away

`backtest_copy_hedge.py` copies each source BUY on the source's 60 largest markets, and separately on 60 random markets (seed 11). It then buys the complementary outcome as soon as `1 − entry − fees − hedge_ask ≥ 2¢`. Both strategies run on the same lots, so the comparison is paired. "Market" entry fills at the last minute price after the delay plus a 1¢ spread, instead of at the source's price.

| markets | entry | delay | hold | copy + hedge |
|---|---|---|---|---|
| top 60 | source price | 0 s | +3.08% | −3.37% |
| random 60 | source price | 0 s | +13.82% | −1.53% |
| top 60 | market | 0 / 30 / 60 / 120 / 300 / 900 s | +1.34 / +1.48 / +1.18 / +1.37 / +2.43 / +5.86% | −5.95 / −6.53 / −6.71 / −6.50 / −4.09 / −1.49% |
| random 60 | market | 0 / 30 / 60 / 120 / 300 / 900 s | +11.90 / +10.83 / +10.26 / +10.34 / +9.68 / +10.39% | −3.58 / −3.92 / −5.08 / −5.89 / −2.45 / −6.55% |

Return is on cost. Hedging trailed holding in all 14 configurations, by 6.5 to 16.9 points.

Why: the hedge triggers when the complement gets cheap. The complement gets cheap when the sharp's side is winning. So the scanner locks a few cents exactly on the bets that were about to pay a dollar. It also pays a second taker fee. In the random-60 run, 220 of 353 copied lots got hedged. A sure profit of a few cents per pair cannot replace the convexity of a sharp's winners.

`HedgeSettings.enabled` therefore defaults to `False`. The code stays because pair accounting (merge, settlement, `PRESERVE_HEDGE`) is correct and tested. It is useful for someone who wants to lock a gain manually. It no longer runs a book scanner on the event loop the copy path shares.

Limits: price history is in one-minute bars, so delays under 60 s cannot be told apart. That is why the delay sweep is flat while the slippage sweep is not. The slippage table is the better measure of what speed is worth.

## Reproduce

From `backend/`:

```bash
for s in 0 0.005 0.01 0.02 0.03 0.05; do uv run python scripts/simulate_copy_portfolio.py --wallet 0x0224bb9eb0a5c9fd261ac9123a72cbdd5748292a --bankroll 1000 --slippage $s; done
```

```bash
uv run python scripts/backtest_copy_hedge.py --wallet 0x0224bb9eb0a5c9fd261ac9123a72cbdd5748292a --markets 60 --rank random --seed 11 --entry market --entry-delay 60 --max-price-diff 1
```

Responses are cached in `~/.cache/polycopy-backtest`, so reruns are fast and repeatable.
