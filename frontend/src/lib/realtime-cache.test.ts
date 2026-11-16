import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { CopySignal, Dashboard, SettingsEnvelope, SourceTrade, WalletSummary } from './api'
import { qk } from './queries'
import { applyRealtimeMessage, upsertNewestFirst } from './realtime-cache'

const trade = (id: number, extra: Partial<SourceTrade> = {}) => ({ id, wallet_id: 1, ...extra }) as SourceTrade

describe('upsertNewestFirst', () => {
  it('prepends new rows, replaces existing ones in place and caps length', () => {
    const list = [trade(3), trade(2), trade(1)]
    expect(upsertNewestFirst(list, trade(4), 3).map((t) => t.id)).toEqual([4, 3, 2])
    const replaced = upsertNewestFirst(list, trade(2, { price: '0.5' }), 3)
    expect(replaced.map((t) => t.id)).toEqual([3, 2, 1])
    expect(replaced[1]?.price).toBe('0.5')
  })
})

describe('applyRealtimeMessage', () => {
  it('merges source trades into every feed cache, deduped and capped by the key limit', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.sourceTrades(2), { trades: [trade(2), trade(1)] })
    qc.setQueryData(qk.sourceTrades(200), { trades: [trade(2), trade(1)] })
    applyRealtimeMessage(qc, { type: 'source_trade', data: trade(3) })
    applyRealtimeMessage(qc, { type: 'source_trade', data: trade(3) })
    expect(qc.getQueryData<{ trades: SourceTrade[] }>(qk.sourceTrades(2))?.trades.map((t) => t.id)).toEqual([3, 2])
    expect(qc.getQueryData<{ trades: SourceTrade[] }>(qk.sourceTrades(200))?.trades.map((t) => t.id)).toEqual([3, 2, 1])
  })

  it('reflects copy signal status on the originating source trade', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.sourceTrades(50), { trades: [trade(7)] })
    qc.setQueryData(qk.copySignals(50), { signals: [] })
    const signal = { id: 11, source_event_id: 7, status: 'SKIPPED', reasons: [] } as unknown as CopySignal
    applyRealtimeMessage(qc, { type: 'copy_signal', data: signal })
    expect(qc.getQueryData<{ signals: CopySignal[] }>(qk.copySignals(50))?.signals).toHaveLength(1)
    expect(qc.getQueryData<{ trades: SourceTrade[] }>(qk.sourceTrades(50))?.trades[0]).toMatchObject({
      signal_id: 11,
      signal_status: 'SKIPPED',
    })
  })

  it('patches the dashboard portfolio and ignores stale settings versions', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.dashboard, { portfolio: { equity: '1' }, strategies_paused: false } as unknown as Dashboard)
    applyRealtimeMessage(qc, { type: 'portfolio', data: { equity: '2' } })
    expect(qc.getQueryData<Dashboard>(qk.dashboard)?.portfolio.equity).toBe('2')

    const env = (version: number, paused: boolean) =>
      ({
        version,
        settings: { strategies_paused: paused, emergency_stop: false, copy: { enabled: true }, hedge: { enabled: true, mode: 'ALERT_ONLY' } },
      }) as unknown as SettingsEnvelope
    applyRealtimeMessage(qc, { type: 'settings', data: env(5, true) })
    applyRealtimeMessage(qc, { type: 'settings', data: env(4, false) })
    expect(qc.getQueryData<SettingsEnvelope>(qk.settings)?.version).toBe(5)
    expect(qc.getQueryData<Dashboard>(qk.dashboard)?.strategies_paused).toBe(true)
  })

  it('removes deleted wallets instead of merging tombstones into wallet rows', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.wallets, { wallets: [{ id: 1 }, { id: 2 }] })
    qc.setQueryData(qk.wallet('1'), { id: 1, nickname: 'Removed trader' })
    qc.setQueryData(qk.dashboard, { followed_wallets: 2 })
    applyRealtimeMessage(qc, { type: 'wallet', data: { id: 1, removed: true } })
    expect(qc.getQueryData<{ wallets: WalletSummary[] }>(qk.wallets)?.wallets).toEqual([{ id: 2 }])
    expect(qc.getQueryState(qk.wallet('1'))?.isInvalidated).toBe(true)
    expect(qc.getQueryState(qk.dashboard)?.isInvalidated).toBe(true)
  })

  it('refreshes wallet detail and trade history after signal decisions', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.wallet('1'), { id: 1, metrics: {} })
    qc.setQueryData(qk.walletTrades('1', 20), { trades: [trade(7)] })
    const signal = { id: 11, wallet_id: 1, source_event_id: 7, status: 'SKIPPED' }
    applyRealtimeMessage(qc, { type: 'copy_signal', data: signal })
    expect(qc.getQueryState(qk.wallet('1'))?.isInvalidated).toBe(true)
    expect(qc.getQueryState(qk.walletTrades('1', 20))?.isInvalidated).toBe(true)
  })

  it('invalidates detail-only fields when a wallet summary arrives', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.wallet('1'), { id: 1, nickname: 'Old', metrics: { trade_count: 1 } })
    applyRealtimeMessage(qc, { type: 'wallet', data: { id: 1, nickname: 'New' } })
    expect(qc.getQueryData(qk.wallet('1'))).toMatchObject({ nickname: 'New', metrics: { trade_count: 1 } })
    expect(qc.getQueryState(qk.wallet('1'))?.isInvalidated).toBe(true)
  })

  it('invalidates REST state on snapshot (initial sync / reconnect)', () => {
    const qc = new QueryClient()
    qc.setQueryData(qk.positions, { positions: [] })
    applyRealtimeMessage(qc, { type: 'snapshot', data: { dashboard: { mode: 'PAPER' } } })
    expect(qc.getQueryState(qk.positions)?.isInvalidated).toBe(true)
    expect(qc.getQueryData<Dashboard>(qk.dashboard)?.mode).toBe('PAPER')
    expect(qc.getQueryState(qk.dashboard)?.isInvalidated).toBe(false)
  })
})
