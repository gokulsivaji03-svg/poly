# Operations

## Local setup

Requires Python 3.12+, uv, Node 22+, npm, and PostgreSQL 17. No private key is required.

```sh
createdb polycopy
createdb polycopy_test
cd backend
cp .env.example .env
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# separate terminal, repository root
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Development login: `admin` / `change-me`. Change the password and set a stable random JWT secret before sharing the application. The API must remain loopback-only without TLS and strong credentials. Environment settings use `POLYCOPY_`; never commit `.env`. A restart invalidates sessions unless a stable secret is configured.

PostgreSQL is required, not SQLite. Run migrations before starting. Workers use a dedicated PostgreSQL advisory-lock connection; only the elected process runs strategies. Keep one API process for local development. REST and WebSocket requests require authentication (except login, health, readiness). WebSocket tokens are in the URL: configure reverse proxies to omit query strings from access logs.

## Paper workflow

1. Configure starting bankroll in Settings and explicitly reset the paper account to apply it. Reset deletes that account's orders, fills, lots and equity history; wallet history remains.
2. Add a public wallet. Initial history is backfill for analytics, not orders to replay now. Newly detected BUY/SELL activity generates auditable copy signals.
3. Source equity is an estimate. Public position value excludes cash; without optional Polygon RPC collateral observations, it is a lower bound, not a known bankroll. Default risk caps still apply. Fixed sizing modes are available for unknown equity.
4. Inspect skipped/reduced signal reasons rather than assuming every source trade is executable.
5. Enable the independent hedge scanner. Alert-only is separate from automatic paper hedging. Matched shares and locked-profit estimates require actual ledger fills.
6. Pause stops new strategy work. Emergency stop persists until explicitly cleared. Neither action can reverse already acquired shares.

Paper fills consume executable depth with fees, optional adverse slippage and delay. They do not model queue position or your own impact on the public venue. Concurrent reservations protect local cash. Source SELLs preserve matched pairs by default. A simulated merge credits contractual pair proceeds in the paper ledger; it does not submit an on-chain transaction or imply instantaneous real capital recovery.

## Historical replay

The recorder persists relevant books and their observed market metadata. Replay uses an isolated PostgreSQL schema, a virtual clock, and only observations at or before that clock. No present-day book or market metadata fallback is permitted. Missing/stale observations reject trades. The recorder must have been running during the requested window; public trade history alone cannot reconstruct executable liquidity. Replay supports at most 31 days per run. Results persist; temporary schemas are removed after completion. Replay is not a claim of historical execution quality.

## Live execution

**Disabled by default. Do not deploy this as an unattended live trader.** International and US are separate venues; the US adapter is unsupported. International requires explicit environment opt-in, credentials and an affirmative geoblock check before each order. Never bypass geographic restrictions.

The International adapter submits SDK FAK orders only after eligibility checks. A network timeout or non-matched acceptance is uncertain, not a rejection: the persisted order and reservation are retained and further submissions halt. An operator must reconcile exchange order/trade history before restarting. Live account authorization, fee reconciliation and recovery must be verified with the actual eligible account before funding. No live orders were used to validate this project. Paper and replay are the supported validation environments.

## Verification

```sh
cd backend
uv run pytest -q
uv run ruff check . ../tests/backend
uv run basedpyright app
cd ../frontend
npm test
npm run lint
npm run build
```

Tests use the dedicated `polycopy_test` database and clear its tables. Never point the test URL at a funded or production database. Back up PostgreSQL regularly; do not delete orders or ledger entries to repair a balance. Inspect `/api/system/health`, reconciliation events and source freshness. Public API outages and unobservable balances must remain visibly unavailable rather than zero.
