# Polymarket API research (verified 2026-11-15)

Findings from live probes and official docs. Everything the integration layer relies on is
listed here with its source so it can be re-verified when Polymarket changes things.

## Polymarket International (on-chain CLOB, Polygon)

| Need | Source | Notes |
|---|---|---|
| Wallet activity | `GET https://data-api.polymarket.com/v2/activity?user=` | **v1 is retired** (`/trades` returns `"Data API v1 has been retired"`). Envelope `{data, pagination}`; cursor-only keyset pagination ordered by `(block_timestamp, sequence_id)`; `sort_direction=ASC` supported; `start`/`end` epoch-second window (omitted `start` floors to 3 years back). Types: `TRADE, SPLIT, MERGE, REDEEM, CONVERSION, REWARD, YIELD, MAKER_REBATE, …`. Rows carry `transaction_hash`, `token_id`, `condition_id`, `side`, `size` (shares), `usdc_size`, `price`, `outcome_index` (`999` = unlabelable), `is_combo`. **No per-row event id is exposed.** |
| Wallet positions | `GET /v2/positions?user=` | `current_size`, `avg_price`, `current_price`, `current_value`, `opposite_token_id`, `negative_risk`, `status` (`OPEN` or `REDEEMABLE`; worthless resolved losers linger as `REDEEMABLE` while redeemed winners disappear, so summing per-position P&L is biased toward losses). |
| Wallet P&L | `GET /v2/user-pnl?user=` | `{data: {points: [...]}}`; latest point's `realized_pnl` / `unrealized_pnl` are lifetime account totals (`realized_pnl` matches `/v2/leaderboard` `pnl`). Empty `points` = no history. Source of observed wallet P&L. |
| Portfolio value | `GET /v2/value?user=` | Positions marked to market. Does **not** include free collateral. |
| Freshness | `GET /v2/status` | `serving.lag_seconds`, ingestion block lag. Used for health/staleness. |
| Token mapping | `GET /v2/tokens?condition=` (SDK `get_token_references`) | Token → condition, outcome index, `opposite_asset_id`, `neg_risk`, `resolved`, `final_price`. Authoritative complement mapping. |
| Market metadata | `GET https://gamma-api.polymarket.com/markets?condition_ids=` | `clobTokenIds`, `outcomes`, `negRisk`, `orderPriceMinTickSize`, `orderMinSize`, `acceptingOrders`, `closed`, `feesEnabled`, `feeSchedule {rate, exponent, takerOnly}`. |
| Order books | `polymarket-client` `AsyncPublicClient.get_order_books` (CLOB `/books`) | Decimal levels, `tick_size`, `min_order_size`, `neg_risk`, `hash`, exchange timestamp. |
| Real-time books | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | `book` snapshots + `price_change` deltas; must resubscribe after reconnect. |
| Geoblock | `GET https://polymarket.com/api/geoblock` | Returned `{"blocked":true,"country":"US","region":"CA"}` from the dev machine. Live International execution is therefore disabled here. |
| Live orders | `polymarket-client` `AsyncSecureClient` (`create_market_order`, `post_order`, `cancel_order`) | Requires private key + derived L2 API creds. Never enabled by default. |

### Fees

Docs (`/trading/fees`): `fee = C × feeRate × p × (1 − p)`, takers only, rate varies by
category (crypto 0.07, sports 0.05, politics 0.04, geopolitics 0). Per-market parameters
come from Gamma `feeSchedule`. The engine models the fee either as reduced delivered shares
(conservative default for hedge matching) or as added cash, configurable.

### Settlement

* A standard binary condition's two tokens always pay $1 combined (winner $1, or $0.50 each
  on a 50/50 resolution), which is what makes an equal-share complementary hedge lock value.
* Resolution goes through UMA: ~2h challenge period, 24–48h if disputed. Merge (full set →
  collateral) is an on-chain CTF operation. Neither is instantaneous; capital recovery is
  modelled as a separate, observed/simulated event.
* Collateral is **pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, 6 decimals).

### Identity and deduplication

The v2 feed has no event id, and one transaction can contain several fills for the same
wallet/token. Event identity is therefore a SHA-256 of
`(wallet, tx_hash, type, token_id, side, size, usdc_size, price, timestamp)` plus an
**occurrence ordinal** for byte-identical rows within the same transaction. Polling
re-reads an overlap window, so repeated rows hash to the same key and are ignored by a
unique constraint.

### Source bankroll observability

`/v2/value` exposes marked positions only. Free collateral needs an on-chain `balanceOf`
for pUSD/USDC.e on the proxy wallet (optional `POLYGON_RPC_URL`). Without it the estimate is
explicitly a **lower bound** and labelled as such.

## Polymarket US (CFTC-regulated DCM)

Separate venue, separate API, separate accounts:

* Public market data: `https://gateway.polymarket.us`; authenticated trading:
  `https://api.polymarket.us` (API key per KYC'd account), WS at `/v1/ws/markets` and
  `/v1/ws/private`.
* Market structure differs (single instrument per outcome, long/short with margin).
* Accounts are not public on-chain wallets, so wallet copy-trading of International
  traders cannot be reproduced there one-to-one.

This project models it as its own venue with its own capability record; it is not wired for
execution and reports itself as unsupported rather than pretending to work.
