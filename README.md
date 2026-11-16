# polycopy

Paper-first Polymarket copy-trading and complementary-outcome hedging terminal.
Real public wallet activity, executable order-book simulation, one auditable portfolio ledger.

## What it does

- Monitors public International wallets through Data API v2; persists activity and restart checkpoints.
- Sizes copies from estimated pre-trade source equity, with explicit unknown/lower-bound coverage.
- Walks real order-book depth with Decimal accounting, fees, slippage, delay and atomic cash reservations.
- Scans exact binary complements for fee-adjusted profitable pairs; reconciles acquired shares before reporting locked profit.
- Preserves matched hedges when a source sells. Supports simulated merge and observed resolution accounting.
- Streams portfolio, trades, opportunities and health to an authenticated React terminal.
- Replays recorded books and metadata in an isolated PostgreSQL schema without future-price fallback.

Live execution is disabled by default. International eligibility is checked; US is a distinct,
unsupported integration. This is not investment advice or a guarantee of profit. Matched pairs
retain execution, platform, settlement and operational risk.

## Run

Requires PostgreSQL 17, Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+ and npm.

```sh
createdb polycopy
createdb polycopy_test
cd backend
cp .env.example .env
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# another terminal:
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Local development login: `admin` / `change-me`.
Change credentials before exposing the server. No private key is needed.

## Structure

```text
backend/
  app/api/             authenticated REST + WebSocket transport
  app/domain/          deterministic sizing, risk, fills, hedge math
  app/integrations/    Data API v2, Gamma, CLOB SDK/WS, eligibility
  app/services/        workers, ledger, copy, hedge, replay
  app/db/              SQLAlchemy models
  alembic/             PostgreSQL migrations
frontend/
  src/pages/           dashboard, wallets, positions, hedges, activity, replay, settings
  src/lib/             typed API, real-time cache, decimal formatting
tests/
  backend/             unit, integration, accounting property tests
  e2e/                 Playwright real-backend desktop/mobile flows
docs/
```

## Verify

```sh
cd backend
uv run pytest -q
uv run ruff check . ../tests/backend
uv run basedpyright app
cd ../frontend
npm test && npm run lint && npm run build
# with backend and Vite running:
cd ../tests/e2e
npm ci
npx playwright install chromium
npm test
```

Tests clear the dedicated `polycopy_test` database. Browser tests change and restore sizing,
pause/resume strategies, and capture screenshots; run them against a development paper account.

## Design and operational boundaries

[Architecture](docs/ARCHITECTURE.md) · [API contract](docs/API.md) ·
[Current Polymarket API research](docs/polymarket-api-research.md) ·
[Operations and safety](docs/OPERATIONS.md)

Public positions do not reveal a wallet's full cash bankroll. Paper trading cannot reproduce
queue priority or market impact. Historical replay requires contemporaneously recorded data;
trade history alone is insufficient. Live execution requires eligible account-specific validation
and operator reconciliation of uncertain submissions; no live-money orders were used in testing.
