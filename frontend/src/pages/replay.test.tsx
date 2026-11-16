import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { ReplayDetail, ReplayParams, ReplaySignal } from '@/lib/api'
import { ReplayPage } from './replay'

const run: ReplayDetail = {
  id: 9,
  status: 'COMPLETED',
  params: {
    start: '2026-11-01T00:00:00Z',
    end: '2026-11-02T00:00:00Z',
    wallet_ids: [1],
    starting_bankroll: '1234.56',
    detection_delay_s: 2,
  },
  created_at: '2026-11-03T00:00:00Z',
  finished_at: '2026-11-03T00:00:01Z',
  error: null,
  summary: {
    starting_bankroll: '1234.56', final_equity: '1200', return_pct: '-0.028',
    realized_pnl: '-34.56', unrealized_pnl: '0', locked_profit: '0',
    signals: { SKIPPED: 2 }, events_replayed: 2, snapshots_used: 0, ledger_ok: false,
  },
  equity_curve: [],
  signals: Array.from({ length: 2 }, (): ReplaySignal => ({
    t: '2026-11-01T01:00:00Z', wallet: 'Trader', side: 'BUY', title: 'Identical market',
    outcome: 'Yes', status: 'SKIPPED', source_price: '0.5', avg_fill_price: null,
    filled_cost: null, reasons: ['NO_BOOK'],
  })),
}

let posts: ReplayParams[]
let settingsAvailable: boolean
let runs: ReplayDetail[]
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })

beforeEach(() => {
  posts = []
  settingsAvailable = true
  runs = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/settings') {
      return settingsAvailable
        ? json({ version: 1, settings: { paper: { starting_bankroll_usd: '1234.5600' } } })
        : json({ detail: 'Settings unavailable' }, 503)
    }
    if (url === '/api/wallets') return json({ wallets: [{ id: 1, nickname: 'Trader', address: `0x${'a'.repeat(40)}` }] })
    if (url === '/api/replay' && init?.method === 'POST') {
      posts.push(JSON.parse(String(init.body)))
      return json({ id: 9, status: 'RUNNING' }, 202)
    }
    if (url === '/api/replay') return json({ runs })
    if (url === '/api/replay/9') return json(run)
    return json({ detail: 'Not found' }, 404)
  }))
})

afterEach(() => vi.unstubAllGlobals())

function renderPage(path = '/replay') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[path]}><ReplayPage /></MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

async function selectWallet() {
  await userEvent.click(await screen.findByRole('checkbox'))
  await waitFor(() => expect(screen.getByTestId('replay-bankroll')).toHaveValue('1234.56'))
}

describe('ReplayPage', () => {
  it('submits exact bankroll strings and integer delay without strategy overrides', async () => {
    const user = userEvent.setup()
    renderPage()
    await selectWallet()
    await user.clear(screen.getByTestId('replay-bankroll'))
    await user.type(screen.getByTestId('replay-bankroll'), '123.450000000000000001')
    await user.click(screen.getByTestId('replay-submit'))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({ wallet_ids: [1], starting_bankroll: '123.450000000000000001', detection_delay_s: 2 })
    expect(posts[0]).not.toHaveProperty('settings')
    expect(posts[0]!.start).toMatch(/Z$/)
    expect(posts[0]!.end).toMatch(/Z$/)
    expect(await screen.findByTestId('replay-detail')).toBeInTheDocument()
  })

  it('rejects zero bankroll and non-integer or out-of-range delay', async () => {
    const user = userEvent.setup()
    renderPage()
    await selectWallet()
    const bankroll = screen.getByTestId('replay-bankroll')
    await user.clear(bankroll)
    await user.type(bankroll, '0.000')
    await user.click(screen.getByTestId('replay-submit'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Bankroll must be a positive decimal.')
    await user.clear(bankroll)
    await user.type(bankroll, '100')
    const delay = screen.getByTestId('replay-delay')
    for (const value of ['0.5', '3601']) {
      await user.clear(delay)
      await user.type(delay, value)
      await user.click(screen.getByTestId('replay-submit'))
      expect(screen.getByRole('alert')).toHaveTextContent('Delay must be a whole number from 0 to 3600 seconds.')
    }
    expect(posts).toHaveLength(0)
  })

  it('rejects a replay window longer than 31 days', async () => {
    renderPage()
    await selectWallet()
    fireEvent.change(screen.getByTestId('replay-start'), { target: { value: '2026-01-01T00:00' } })
    fireEvent.change(screen.getByTestId('replay-end'), { target: { value: '2026-03-01T00:00' } })
    await userEvent.click(screen.getByTestId('replay-submit'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Replay windows are limited to 31 days.')
    expect(posts).toHaveLength(0)
  })

  it('does not fabricate a default bankroll when settings fail', async () => {
    settingsAvailable = false
    renderPage()
    expect(await screen.findByText('Failed to load default bankroll')).toBeInTheDocument()
    expect(screen.getByTestId('replay-bankroll')).toHaveValue('')
  })

  it('flags failed ledger verification and retains otherwise identical signals', async () => {
    renderPage('/replay?run=9')
    expect(await screen.findByText(/Replay ledger verification failed/)).toBeInTheDocument()
    const table = screen.getByTestId('replay-signals-table')
    expect(within(table).getAllByText('Identical market')).toHaveLength(2)
    await userEvent.click(screen.getByRole('button', { name: 'Close run' }))
    expect(screen.queryByTestId('replay-detail')).not.toBeInTheDocument()
  })

  it('disables new runs while another run is running', async () => {
    runs = [{ ...run, status: 'RUNNING', summary: null, finished_at: null }]
    renderPage()
    await waitFor(() => expect(screen.getByTestId('replay-submit')).toBeDisabled())
    expect(screen.getByTestId('replay-submit')).toHaveTextContent('Replay running')
  })
})
