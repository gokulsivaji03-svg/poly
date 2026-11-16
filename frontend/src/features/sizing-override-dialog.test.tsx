import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WalletSummary } from '@/lib/api'
import { SizingOverrideDialog } from './sizing-override-dialog'

const wallet = {
  id: 1, nickname: 'Trader', address: `0x${'a'.repeat(40)}`,
  sizing_override: {
    mode: 'PROPORTIONAL', multiplier: '1', unknown_equity_policy: 'SKIP',
    lower_bound_equity_policy: 'USE_CAPPED', min_source_confidence: '0.5',
  },
} as WalletSummary

afterEach(() => vi.unstubAllGlobals())

describe('wallet sizing overrides', () => {
  it('preserves policy overrides while editing exact percentage values', async () => {
    const bodies: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)))
      return new Response(JSON.stringify(wallet), { status: 200 })
    }))
    const user = userEvent.setup()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><SizingOverrideDialog wallet={wallet} /></QueryClientProvider>)
    await user.click(screen.getByRole('button', { name: 'Edit sizing override for Trader' }))
    expect(screen.getByLabelText('Unknown source equity')).toHaveValue('SKIP')
    expect(screen.getByLabelText('Lower-bound source equity')).toHaveValue('USE_CAPPED')
    expect(screen.getByLabelText('Min equity confidence')).toHaveValue('0.5')
    await user.type(screen.getByLabelText('% of your bankroll'), '14.3')
    await user.click(screen.getByRole('button', { name: 'Save override' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ sizing_override: {
      mode: 'PROPORTIONAL', multiplier: '1', fixed_pct_of_user: '0.143',
      unknown_equity_policy: 'SKIP', lower_bound_equity_policy: 'USE_CAPPED', min_source_confidence: '0.5',
    } })
  })

  it('blocks percentages and multipliers that the backend rejects', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const user = userEvent.setup()
    const qc = new QueryClient()
    render(<QueryClientProvider client={qc}><SizingOverrideDialog wallet={wallet} /></QueryClientProvider>)
    await user.click(screen.getByRole('button', { name: 'Edit sizing override for Trader' }))
    await user.type(screen.getByLabelText('% of your bankroll'), '101')
    const multiplier = screen.getByLabelText('Multiplier')
    await user.clear(multiplier)
    await user.type(multiplier, '0')
    await user.click(screen.getByRole('button', { name: 'Save override' }))
    expect(await screen.findByText('Must be ≤ 100')).toBeInTheDocument()
    expect(screen.getByText('Must be > 0')).toBeInTheDocument()
    expect(fetcher).not.toHaveBeenCalled()
  })
})
