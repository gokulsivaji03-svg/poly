# HTTP & WebSocket API

Base path `/api`. All endpoints except `/api/health`, `/api/ready` and `/api/auth/login` require
`Authorization: Bearer <jwt>`. The WebSocket takes the token as `?token=`.

Conventions:

* **Money, shares, prices and percentages are decimal strings** (`"12.345600"`). Percentages are
  fractions (`"0.02"` = 2%). Never parse them as floats for arithmetic.
* `null` means **unavailable / unknown**, never zero.
* Timestamps are ISO-8601 UTC strings.
* Errors: `{"detail": "..."}` with 4xx/5xx status. Validation errors are 422.

## Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/auth/login` | `{username, password}` | `{access_token, token_type: "bearer", expires_at}` |
| GET | `/auth/me` | – | `{username}` |

## Health

| GET `/health` | `{status: "ok"}` (liveness) |
|---|---|
| GET `/ready` | `{ready, database, leader, workers: {<name>: WorkerStatus}}` |

`WorkerStatus = {state: "running"|"stopped"|"error"|"standby", last_run_at, last_error}`

## Dashboard & portfolio

`GET /dashboard`

```jsonc
{
  "portfolio": PortfolioSummary,
  "followed_wallets": 3,
  "trades_copied_today": 7,
  "mode": "PAPER",               // PAPER | REPLAY | LIVE
  "strategies_paused": false,
  "emergency_stop": false,
  "copy_enabled": true,
  "hedge_enabled": true,
  "hedge_mode": "ALERT_ONLY"     // ALERT_ONLY | AUTO_PAPER
}
```

`PortfolioSummary`:
`{account_id, as_of, starting_bankroll, equity, cash, reserved, available_cash, invested,
positions_value, realized_pnl, unrealized_pnl, locked_profit, fees_paid, unmarked_positions,
day_pnl}`

`GET /portfolio/equity-history?range=1d|7d|30d|all` →
`{points: [{t, equity, cash, positions_value, realized_pnl, unrealized_pnl, locked_profit}]}`
(ascending by `t`).

`GET /positions` → `{positions: Position[]}`

```jsonc
Position = {
  "condition_id", "token_id", "opposite_token_id",   // opposite may be null if unknown
  "question", "outcome",
  "shares", "matched_shares", "unmatched_shares", "hedged_pct",
  "avg_entry", "cost_basis",
  "mark",            // conservative mark (mid, else best bid) or null
  "executable_bid",  // best bid or null
  "market_value", "unrealized_pnl",
  "locked_profit",   // locked profit attributable to this condition (same for both legs)
  "hedge_state",     // UNHEDGED | HEDGE_OPPORTUNITY | HEDGE_PENDING | PARTIALLY_HEDGED | FULLY_HEDGED | HEDGE_FAILED | SETTLED
  "origins": ["COPY"|"HEDGE"],
  "lots": Lot[]
}
Lot = {id, origin, source_wallet, entry_price, shares_acquired, shares_remaining,
       matched_shares, cost_remaining, fee_usdc, opened_at}
```

`GET /orders?limit=100` → `{orders: [{id, kind, side, status, token_id, condition_id,
requested_budget, requested_shares, limit_price, reason, created_at, completed_at,
exec_latency_ms, book_age_ms, fill: {gross_shares, net_shares, notional, fee_usdc, cash_delta,
avg_price, worst_price} | null}]}`

## Wallets

`GET /wallets` → `{wallets: WalletSummary[]}`

```jsonc
WalletSummary = {
  "id", "address", "nickname", "follow", "added_at",
  "sizing_override": {...partial SizingSettings} | null,
  "estimated_equity": "1234.5" | null,
  "equity_coverage": "POSITIONS_AND_CASH" | "POSITIONS_ONLY" | "UNAVAILABLE",
  "equity_is_lower_bound": true,
  "equity_confidence": "0.62",
  "equity_as_of": ts | null,
  "positions_value": str | null, "cash_balance": str | null,
  "open_positions": 12 | null,
  "observed_realized_pnl": str | null, "observed_unrealized_pnl": str | null,
  "last_trade_at": ts | null,       // last detected TRADE (block time)
  "last_success_at": ts | null,     // last successful poll
  "last_error": str | null,
  "freshness_s": 3 | null,          // now - last_success_at
  "ingestion_delay_ms": 4100 | null,// median detected_at - block_ts over recent trades
  "trades_7d": 18
}
```

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/wallets` | `{address, nickname?, follow?=true}` | `201 WalletSummary`; `409` duplicate; `422` invalid address |
| PATCH | `/wallets/{id}` | `{nickname?, follow?, sizing_override?: object|null}` | `WalletSummary` |
| DELETE | `/wallets/{id}` | – | `204` (monitoring stops; history kept) |
| GET | `/wallets/{id}` | – | `WalletDetail` |
| GET | `/wallets/{id}/trades?limit=100&before_id=` | – | `{trades: SourceTrade[]}` |

```jsonc
WalletDetail = WalletSummary & {
  "metrics": {
    "trade_count", "buy_count", "sell_count", "trades_per_day": str | null,
    "avg_trade_usd": str | null, "avg_position_usd": str | null,
    "copied_count", "skipped_count", "copy_rate": str | null,
    "avg_price_diff_bps": str | null, "median_detection_lag_ms": int | null,
    "skip_reasons": {"STALE_SIGNAL": 3, ...}
  },
  "equity_history": [{t, equity, positions_value, cash_balance, coverage, confidence}],
  "allocation_history": [{t, source_alloc_pct, source_cost, source_equity, signal_status}],
  "positions": [{token_id, condition_id, title, outcome, size, avg_price, current_price,
                 current_value, realized_pnl, unrealized_pnl}]
}
```

## Feeds

`GET /source-trades?limit=50&wallet_id=` → `{trades: SourceTrade[]}` (newest first)

```jsonc
SourceTrade = {
  "id", "wallet_id", "wallet_address", "wallet_nickname",
  "type": "TRADE"|"SPLIT"|"MERGE"|"REDEEM"|"CONVERSION"|"REWARD"|...,
  "side": "BUY"|"SELL"|null, "condition_id", "token_id", "outcome", "title", "slug",
  "size", "usdc_size", "price", "tx_hash", "block_ts", "detected_at",
  "detection_lag_ms", "is_backfill",
  "signal_id": int | null, "signal_status": str | null
}
```

`GET /copy-signals?limit=50` → `{signals: CopySignal[]}` (newest first)

```jsonc
CopySignal = {
  "id", "source_event_id", "wallet_id", "wallet_nickname", "wallet_address",
  "status": "PENDING"|"SKIPPED"|"REJECTED"|"EXECUTED"|"PARTIAL"|"FAILED",
  "side", "title", "outcome", "token_id", "condition_id",
  "source_price", "source_shares", "source_cost",
  "source_equity": str|null, "source_equity_coverage": str|null, "source_confidence": str|null,
  "source_alloc_pct": str|null,      // source_cost / source pre-trade equity
  "user_equity": str|null, "proposed_pct": str|null,
  "target_budget": str|null, "approved_budget": str|null, "limit_price": str|null,
  "filled_shares": str|null, "filled_cost": str|null, "avg_fill_price": str|null,
  "price_diff_bps": str|null,        // (avg_fill - source_price)/source_price * 1e4
  "reasons": [{"code", "message"}],  // every reduction / skip, in order applied
  "detected_at", "decided_at", "executed_at",
  "detection_lag_ms", "signal_latency_ms", "exec_latency_ms", "order_id"
}
```

## Hedging

`GET /hedges/opportunities` →

```jsonc
{
  "scanner": {"enabled", "mode", "last_scan_at", "scan_duration_ms", "positions_scanned"},
  "opportunities": [HedgeOpportunity]   // one per open primary position; includes non-viable with reasons
}
HedgeOpportunity = {
  "id",                       // stable "<condition_id>:<primary_token_id>"
  "condition_id", "question",
  "primary_token_id", "primary_outcome", "hedge_token_id", "hedge_outcome",
  "state", "viable", "reasons": ["NO_PROFITABLE_DEPTH", ...],
  "existing_shares", "unhedged_shares", "existing_entry_price",
  "hedge_best_ask": str|null, "hedge_avg_price": str|null, "hedge_worst_price": str|null,
  "combined_cost_per_share": str|null,
  "matched_shares", "net_profit_per_share", "net_profit", "gross_profit",
  "primary_fees", "hedge_fees", "capital_required", "expected_return",
  "depth_usd", "quoted_at", "book_age_ms"
}
```

| Method | Path | Response |
|---|---|---|
| POST | `/hedges/{id}/execute` | `{status, order_id, matched_shares, reason, state}` — manual paper hedge (re-quotes first; rejected if no longer viable) |
| POST | `/hedges/{id}/merge` | `{proceeds}` — simulate a CTF merge of matched pairs (paper only; not instantaneous on-chain) |
| GET | `/hedges/events?limit=100` | `{events: [{id, condition_id, primary_token_id, kind, from_state, to_state, detail, created_at}]}` |

## Settings & control

`GET /settings` → `{version, settings: StrategySettings}`
`PUT /settings` body `{settings: <partial StrategySettings>, expected_version?}` →
`{version, settings}`; `409` on version conflict; `422` on invalid values.

`StrategySettings` (all decimals as strings):

```jsonc
{
  "sizing": {"mode": "PROPORTIONAL"|"FIXED_AMOUNT"|"FIXED_PCT_OF_USER"|"SOURCE_MULTIPLE",
             "multiplier", "fixed_amount_usd", "fixed_pct_of_user", "source_multiple",
             "unknown_equity_policy": "SKIP"|"FALLBACK_FIXED",
             "lower_bound_equity_policy": "USE_CAPPED"|"SKIP", "min_source_confidence"},
  "risk": {"max_pct_per_trade", "max_position_usd", "max_market_exposure_usd",
           "max_total_exposure_pct", "min_liquidity_usd", "max_price_diff_pct",
           "max_slippage_pct", "max_detection_delay_s", "min_cash_reserve_usd",
           "daily_loss_limit_usd", "max_book_age_s", "min_order_usd",
           "max_entry_price", "max_buys_per_market"},
  "copy": {"enabled", "sell_policy": "PROPORTIONAL"|"FULL_EXIT"|"IGNORE",
           "hedged_sell_policy": "PRESERVE_HEDGE"|"UNWIND_IF_BETTER", "copy_sells"},
  "hedge": {"enabled", "mode": "ALERT_ONLY"|"AUTO_PAPER", "min_net_gap_per_share",
            "min_abs_profit_usd", "min_return_pct", "min_liquidity_usd", "max_hedge_price",
            "min_hedge_shares", "max_hedge_shares", "full_hedge_only", "other_cost_per_share",
            "allow_neg_risk", "scan_interval_s"},
  "paper": {"starting_bankroll_usd", "execution_delay_ms", "buy_fee_mode": "shares"|"cash",
            "share_step", "extra_slippage_bps"},
  "strategies_paused": false,
  "emergency_stop": false
}
```

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/control/pause` | – | `strategies_paused=true` |
| POST | `/control/resume` | – | `strategies_paused=false` (does not clear emergency stop) |
| POST | `/control/emergency-stop` | – | `emergency_stop=true`, releases open reservations |
| POST | `/control/clear-emergency` | – | `emergency_stop=false` |
| POST | `/control/reset-paper` | `{starting_bankroll}` | wipes paper history, returns `PortfolioSummary` |

All return `{version, settings}` except `reset-paper`.

## System

`GET /system/health` →

```jsonc
{
  "mode": "PAPER",
  "venues": [{"id": "polymarket_international", "name", "live_supported": bool,
              "live_enabled": bool, "reason": str|null,
              "geoblock": {"blocked", "country", "region", "checked_at"} | null},
             {"id": "polymarket_us", ...}],
  "workers": {<name>: WorkerStatus},
  "metrics": {"source_detection_lag_ms_p50", "source_detection_lag_ms_p95",
              "data_api_lag_s", "signal_latency_ms_p50", "exec_latency_ms_p50",
              "book_age_ms_p50", "ws_connected": bool, "ws_reconnects": int,
              "http_errors_5m": int, "last_poll_at"},   // ints/strings or null
  "ledger_ok": bool, "ledger_problems": [str]
}
```

`GET /system/events?limit=100` → `{events: [{id, level, kind, message, data, created_at}]}`

## WebSocket `/api/ws?token=<jwt>`

On connect the server sends `{"type": "snapshot", "data": {"dashboard": Dashboard}}`.
Subsequent messages: `{"type": <topic>, "data": <payload>, "ts": iso}`.

| topic | payload |
|---|---|
| `source_trade` | `SourceTrade` |
| `copy_signal` | `CopySignal` (sent on create and on every status change) |
| `portfolio` | `PortfolioSummary` |
| `positions` | `{}` (hint: refetch `/positions`) |
| `hedge` | `{opportunities: HedgeOpportunity[], scanner}` |
| `wallet` | `WalletSummary` |
| `settings` | `{version, settings}` |
| `system` | system event row |
| `heartbeat` | `{}` every 15s |

Client may send `{"type":"ping"}`; server replies `{"type":"pong"}`. Clients should reconnect
with backoff and refetch REST state after reconnect (the snapshot covers the dashboard).

## Historical replay

`POST /replay` → 202 `{id, status:"RUNNING"}`. Body:
`{start:iso, end:iso, wallet_ids:int[], starting_bankroll:decimal-string,
detection_delay_s?:int, settings?:partial-StrategySettings}`.
Timezone-aware window must be positive and at most 31 days; 1–50 wallets;
delay 0–3600 seconds. 409 if a run is already active in this process.

`GET /replay` → `{runs:ReplayRun[]}`; `GET /replay/{id}` → `ReplayRun`.
A run has `id,status,params,created_at,finished_at,error,summary`.
Completed detail includes `equity_curve:[{t,equity}]` and
`signals:[{t,wallet,side,title,outcome,status,source_price,avg_fill_price,filled_cost,reasons}]`.
Summary fields: `final_equity,starting_bankroll,return_pct,realized_pnl,unrealized_pnl,
locked_profit,signals` (status counts), `events_replayed,snapshots_used,ledger_ok`.
All money values are strings; status is RUNNING, COMPLETED or FAILED.
Replay uses recorded books **and recorded market metadata**, never current market fallbacks.

### Settings integer exceptions

`max_detection_delay_s`, `max_book_age_s`, `max_buys_per_market`, `scan_interval_s`,
`execution_delay_ms` and `extra_slippage_bps` are JSON integers, not decimal strings.
`hedge.max_hedge_shares` is a positive decimal, not nullable.
