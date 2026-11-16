/**
 * Maps realtime WebSocket topics onto the TanStack Query cache.
 *
 * Feed topics are merged in place (dedupe by id, newest first, capped to the list's limit);
 * aggregate topics replace their query data; hint topics invalidate.
 */
import type { QueryClient, QueryKey } from '@tanstack/react-query'
import type {
  CopySignal,
  Dashboard,
  HedgeOpportunities,
  PortfolioSummary,
  SettingsEnvelope,
  SourceTrade,
  SystemEvent,
  WalletDetail,
  WalletSummary,
} from './api'
import { qk } from './queries'
import type { WsMessage } from './ws'

interface HasId {
  id: string | number
}

const DEFAULT_CAP = 200

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function hasId(v: unknown): v is HasId {
  return isRecord(v) && (typeof v.id === 'string' || typeof v.id === 'number')
}

/** Replace an existing row (same id) in place, otherwise prepend; cap the length. */
export function upsertNewestFirst<T extends HasId>(list: readonly T[], item: T, cap = DEFAULT_CAP): T[] {
  const key = String(item.id)
  const idx = list.findIndex((x) => String(x.id) === key)
  if (idx >= 0) {
    const next = list.slice()
    next[idx] = item
    return next
  }
  return [item, ...list].slice(0, cap)
}

/** Limit encoded as the 2nd element of feed keys (e.g. ['source-trades', 50]). */
function capFromKey(key: QueryKey): number {
  const n = key[1]
  return typeof n === 'number' && n > 0 ? n : DEFAULT_CAP
}

function mergeFeed<K extends string, T extends HasId>(qc: QueryClient, prefix: QueryKey, field: K, item: T): void {
  for (const [key, data] of qc.getQueriesData<Record<K, T[]>>({ queryKey: prefix })) {
    if (!data || !Array.isArray(data[field])) continue
    qc.setQueryData(key, { ...data, [field]: upsertNewestFirst(data[field], item, capFromKey(key)) })
  }
}

const SIGNAL_TERMINAL: Partial<Record<CopySignal['status'], true>> = { EXECUTED: true, PARTIAL: true, FAILED: true }

/**
 * Store a settings envelope (from WS, PUT /settings or a control action) unless the cache already
 * holds a newer version, and mirror the strategy flags into the dashboard aggregate.
 */
export function applySettingsEnvelope(qc: QueryClient, env: SettingsEnvelope): void {
  const current = qc.getQueryData<SettingsEnvelope>(qk.settings)
  if (current && current.version > env.version) return
  qc.setQueryData(qk.settings, env)
  const s = env.settings
  qc.setQueryData<Dashboard>(qk.dashboard, (old) =>
    old
      ? {
          ...old,
          strategies_paused: s.strategies_paused,
          emergency_stop: s.emergency_stop,
          copy_enabled: s.copy.enabled,
          hedge_enabled: s.hedge.enabled,
          hedge_mode: s.hedge.mode,
        }
      : old,
  )
}

export function applyRealtimeMessage(qc: QueryClient, msg: WsMessage): void {
  const data = msg.data
  switch (msg.type) {
    case 'snapshot': {
      if (isRecord(data) && isRecord(data.dashboard)) {
        // Server-pushed snapshot of the dashboard aggregate; shape per API contract.
        const dashboard = data.dashboard as unknown as Dashboard
        qc.setQueryData(qk.dashboard, dashboard)
      }
      // Initial-state sync (also runs after every reconnect): refetch everything else.
      void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== 'dashboard' && q.queryKey[0] !== 'me' })
      return
    }
    case 'source_trade': {
      if (!hasId(data)) return
      const trade = data as SourceTrade
      mergeFeed(qc, qk.sourceTradesAll, 'trades', trade)
      void qc.invalidateQueries({ queryKey: qk.walletTradesAll(String(trade.wallet_id)) })
      void qc.invalidateQueries({ queryKey: qk.wallet(String(trade.wallet_id)) })
      return
    }
    case 'copy_signal': {
      if (!hasId(data)) return
      const signal = data as CopySignal
      mergeFeed(qc, qk.copySignalsAll, 'signals', signal)
      void qc.invalidateQueries({ queryKey: qk.wallet(String(signal.wallet_id)) })
      void qc.invalidateQueries({ queryKey: qk.walletTradesAll(String(signal.wallet_id)) })
      // Reflect the signal status on its originating source trade row.
      for (const [key, feed] of qc.getQueriesData<{ trades: SourceTrade[] }>({ queryKey: qk.sourceTradesAll })) {
        if (!feed?.trades.some((t) => String(t.id) === String(signal.source_event_id))) continue
        qc.setQueryData(key, {
          ...feed,
          trades: feed.trades.map((t) =>
            String(t.id) === String(signal.source_event_id)
              ? { ...t, signal_id: Number(signal.id), signal_status: signal.status }
              : t,
          ),
        })
      }
      if (SIGNAL_TERMINAL[signal.status]) {
        void qc.invalidateQueries({ queryKey: qk.dashboard })
        void qc.invalidateQueries({ queryKey: ['orders'] })
      }
      return
    }
    case 'portfolio': {
      if (!isRecord(data)) return
      const portfolio = data as unknown as PortfolioSummary
      qc.setQueryData<Dashboard>(qk.dashboard, (old) => (old ? { ...old, portfolio } : old))
      return
    }
    case 'positions': {
      void qc.invalidateQueries({ queryKey: qk.positions })
      return
    }
    case 'hedge': {
      if (!isRecord(data) || !Array.isArray(data.opportunities)) return
      qc.setQueryData(qk.hedges, data as unknown as HedgeOpportunities)
      void qc.invalidateQueries({ queryKey: qk.hedgeEvents })
      return
    }
    case 'wallet': {
      if (!hasId(data)) return
      if (isRecord(data) && data.removed === true) {
        qc.setQueryData<{ wallets: WalletSummary[] }>(qk.wallets, (old) =>
          old ? { ...old, wallets: old.wallets.filter((w) => String(w.id) !== String(data.id)) } : old,
        )
        void qc.invalidateQueries({ queryKey: qk.wallet(String(data.id)), exact: true })
        void qc.invalidateQueries({ queryKey: qk.dashboard })
        return
      }
      const wallet = data as unknown as WalletSummary
      qc.setQueryData<{ wallets: WalletSummary[] }>(qk.wallets, (old) => {
        if (!old) return old
        const exists = old.wallets.some((w) => String(w.id) === String(wallet.id))
        return {
          wallets: exists
            ? old.wallets.map((w) => (String(w.id) === String(wallet.id) ? wallet : w))
            : [...old.wallets, wallet],
        }
      })
      qc.setQueryData<WalletDetail>(qk.wallet(String(wallet.id)), (old) => (old ? { ...old, ...wallet } : old))
      // Wallet events carry only the summary; refresh metrics, positions and histories separately.
      void qc.invalidateQueries({ queryKey: qk.wallet(String(wallet.id)) })
      return
    }
    case 'settings': {
      if (!isRecord(data) || typeof data.version !== 'number' || !isRecord(data.settings)) return
      const env = data as unknown as SettingsEnvelope
      applySettingsEnvelope(qc, env)
      return
    }
    case 'system': {
      if (!hasId(data)) return
      mergeFeed(qc, qk.systemEvents, 'events', data as SystemEvent)
      return
    }
    default:
      // heartbeat and unknown topics: nothing to merge (the socket watchdog already saw the frame).
      return
  }
}
