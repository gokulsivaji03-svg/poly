import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { SettingsEnvelope, StrategySettings } from '@/lib/api'
import { SettingsPage } from './settings'

const SETTINGS: StrategySettings = {
  sizing: {
    mode: 'PROPORTIONAL',
    multiplier: '1.000000',
    fixed_amount_usd: '25.00',
    fixed_pct_of_user: '0.010000',
    source_multiple: '0.1',
    unknown_equity_policy: 'SKIP',
    lower_bound_equity_policy: 'USE_CAPPED',
    min_source_confidence: '0.5',
  },
  risk: {
    max_pct_per_trade: '0.020000',
    max_position_usd: '200',
    max_market_exposure_usd: '400',
    max_total_exposure_pct: '0.8',
    min_liquidity_usd: '100',
    max_price_diff_pct: '0.05',
    max_slippage_pct: '0.03',
    max_detection_delay_s: 60,
    min_cash_reserve_usd: '50',
    daily_loss_limit_usd: '100',
    max_book_age_s: 10,
    min_order_usd: '1',
    max_entry_price: '0.9',
    max_buys_per_market: 10,
  },
  copy: { enabled: true, sell_policy: 'PROPORTIONAL', hedged_sell_policy: 'PRESERVE_HEDGE', copy_sells: true },
  hedge: {
    enabled: true,
    mode: 'ALERT_ONLY',
    min_net_gap_per_share: '0.01',
    min_abs_profit_usd: '1',
    min_return_pct: '0.005',
    min_liquidity_usd: '50',
    max_hedge_price: '0.95',
    min_hedge_shares: '5',
    max_hedge_shares: '100000',
    full_hedge_only: false,
    other_cost_per_share: '0',
    allow_neg_risk: false,
    scan_interval_s: 5,
  },
  paper: {
    starting_bankroll_usd: '1000',
    execution_delay_ms: 250,
    buy_fee_mode: 'shares',
    share_step: '0.01',
    extra_slippage_bps: 0,
  },
  strategies_paused: false,
  emergency_stop: false,
}

interface Call {
  method: string
  url: string
  body: unknown
}

let calls: Call[]
let putResponse: (body: unknown) => Response

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  calls = []
  putResponse = () => json({ version: 8, settings: SETTINGS } satisfies SettingsEnvelope)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ method, url, body })
      if (url === '/api/settings' && method === 'GET') return json({ version: 7, settings: SETTINGS } satisfies SettingsEnvelope)
      if (url === '/api/settings' && method === 'PUT') return putResponse(body)
      if (url === '/api/wallets') return json({ wallets: [] })
      return json({ detail: 'not found' }, 404)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

const puts = () => calls.filter((c) => c.method === 'PUT')

describe('SettingsPage form', () => {
  it('edits percent fields as % and sends exact fraction strings', async () => {
    const user = userEvent.setup()
    renderPage()

    const maxPct = await screen.findByLabelText(/Max % per trade/)
    await waitFor(() => expect(maxPct).toHaveValue('2'))
    const save = screen.getByTestId('settings-save')
    expect(save).toBeDisabled()

    // 14.3 / 100 === 0.14300000000000002 in floating point; the payload must be exactly "0.143".
    await user.clear(maxPct)
    await user.type(maxPct, '14.3')
    expect(screen.getByText('1 unsaved change')).toBeInTheDocument()
    expect(save).toBeEnabled()
    await user.click(save)

    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(puts()[0]!.body).toEqual({
      settings: { risk: { max_pct_per_trade: '0.143' } },
      expected_version: 7,
    })
  })

  it('sends only changed fields, normalizes plain decimals and ignores no-op edits', async () => {
    const user = userEvent.setup()
    renderPage()

    const multiplier = await screen.findByTestId('settings-multiplier')
    await waitFor(() => expect(multiplier).toHaveValue('1'))
    await user.clear(multiplier)
    await user.type(multiplier, '1.50')

    // Re-typing an equal value is not a change.
    const slippage = screen.getByLabelText(/Max slippage vs best ask/)
    await user.clear(slippage)
    await user.type(slippage, '3.0')

    expect(screen.getByText('1 unsaved change')).toBeInTheDocument()
    await user.click(screen.getByTestId('settings-save'))
    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(puts()[0]!.body).toEqual({ settings: { sizing: { multiplier: '1.5' } }, expected_version: 7 })
  })

  it('blocks invalid input client-side', async () => {
    const user = userEvent.setup()
    renderPage()
    const maxPct = await screen.findByLabelText(/Max % per trade/)
    await waitFor(() => expect(maxPct).toHaveValue('2'))
    await user.clear(maxPct)
    await user.type(maxPct, '150')
    expect(screen.getByText('Must be ≤ 100')).toBeInTheDocument()
    expect(screen.getByTestId('settings-save')).toBeDisabled()
  })

  it('maps 422 validation errors onto fields', async () => {
    putResponse = () =>
      json({ detail: [{ loc: ['body', 'settings', 'risk', 'max_pct_per_trade'], msg: 'exceeds hard cap', type: 'value_error' }] }, 422)
    const user = userEvent.setup()
    renderPage()
    const maxPct = await screen.findByLabelText(/Max % per trade/)
    await waitFor(() => expect(maxPct).toHaveValue('2'))
    await user.clear(maxPct)
    await user.type(maxPct, '50')
    await user.click(screen.getByTestId('settings-save'))
    expect(await screen.findByText('exceeds hard cap')).toBeInTheDocument()
  })

  it('prompts to reload on a 409 version conflict', async () => {
    putResponse = () => json({ detail: 'version conflict' }, 409)
    const user = userEvent.setup()
    renderPage()
    const maxPct = await screen.findByLabelText(/Max % per trade/)
    await waitFor(() => expect(maxPct).toHaveValue('2'))
    await user.clear(maxPct)
    await user.type(maxPct, '3')
    await user.click(screen.getByTestId('settings-save'))
    expect(await screen.findByText(/Settings were changed elsewhere/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reload latest/ })).toBeInTheDocument()
  })
})
