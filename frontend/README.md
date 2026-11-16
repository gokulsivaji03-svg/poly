# polycopy terminal

React/TypeScript terminal for the authenticated backend API. Dashboard, source wallets and histories,
positions/lots, paper hedges and simulated merges, activity, historical replay, strategy settings and
system health use real REST/WebSocket responses. There is no production mock-data mode.

## Development

Run the backend at `http://127.0.0.1:8000`, with the database, admin login and workers configured.
From `frontend/`:

```sh
npm ci
npm run dev
```

Open `http://localhost:5173` and sign in with the backend admin credentials. Vite proxies `/api`
(including the authenticated WebSocket) to the backend. Failed or unavailable backend data is shown
as an error or `—`, never fabricated balances or fills.

## Production

`npm run build` writes `dist/`. Serve it behind HTTPS with `/api` forwarded to the backend,
WebSocket upgrades enabled for `/api/ws`, and non-API browser routes rewritten to `index.html`.
`npm run preview` serves the build on port 4173 with the same local backend proxy; it is not a
production server. The browser keeps its JWT in localStorage; never enter wallet private keys.

## Data and safety

- The mode badge reports the backend mode. Paper hedge/merge actions are simulations; the frontend
  does not configure or enable live trading.
- Financial calculations, formatting and percent conversions use decimal strings. Only chart
  coordinates and visual bars use approximate floating-point values. Integer timing/slippage
  settings remain JSON integers.
- Realtime updates refresh caches; reconnects resync REST state, with 10-second polling while the
  stream is offline. Equity history refreshes every 30 seconds; orders and system health every 10 seconds.
- Replay uses `/api/replay` from the replay-enabled backend. Choose 1–50 wallets, a window of at most
  31 days, a positive bankroll and a whole-second detection delay of 0–3600 seconds. Runs are polled
  every 2 seconds while running; final results stay separate from the active account. Results appear
  when the backend finishes, and failed ledger verification is prominently flagged.
- Replay requires previously recorded source events, books and equity snapshots. Missing history is
  not synthesized. The isolated replay unpauses strategies, clears its emergency stop and zeroes
  execution delay; it does not change active-account controls.
- Settings save only edited fields with an expected version. Conflicts preserve edits for explicit
  reload/rebase. Wallet sizing overrides support every global sizing field; blank fields inherit.
- Resetting the paper account and executing/merging hedges require confirmation. Reset requires a
  positive bankroll and wipes paper trading history, not source wallet history.

## Checks

```sh
npm test
npm run lint
npm run build
```

Tests cover decimal arithmetic, WebSocket reconnects, realtime cache updates and form/API contracts.
