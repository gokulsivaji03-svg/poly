/** Query keys and TanStack Query hooks for every REST resource. */
import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { api, type EquityRange } from './api'
import { usePollInterval } from './realtime-context'

export const qk = {
  me: ['me'] as const,
  dashboard: ['dashboard'] as const,
  equityHistory: (range: EquityRange) => ['equity-history', range] as const,
  positions: ['positions'] as const,
  orders: (limit: number) => ['orders', limit] as const,
  wallets: ['wallets'] as const,
  wallet: (id: string) => ['wallet', id] as const,
  walletTradesAll: (id: string) => ['wallet-trades', id] as const,
  walletTrades: (id: string, limit: number) => ['wallet-trades', id, 'list', limit] as const,
  walletTradesInfinite: (id: string) => ['wallet-trades', id, 'infinite'] as const,
  sourceTradesAll: ['source-trades'] as const,
  sourceTrades: (limit: number) => ['source-trades', limit] as const,
  copySignalsAll: ['copy-signals'] as const,
  copySignals: (limit: number) => ['copy-signals', limit] as const,
  hedges: ['hedges'] as const,
  hedgeEvents: ['hedge-events'] as const,
  settings: ['settings'] as const,
  systemHealth: ['system-health'] as const,
  systemEvents: ['system-events'] as const,
  replayRuns: ['replay'] as const,
  replayRun: (id: string) => ['replay', id] as const,
}

export function useMe() {
  return useQuery({ queryKey: qk.me, queryFn: api.me, staleTime: Infinity })
}

export function useDashboard() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.dashboard, queryFn: api.dashboard, refetchInterval })
}

export function useEquityHistory(range: EquityRange) {
  return useQuery({
    queryKey: qk.equityHistory(range),
    queryFn: () => api.equityHistory(range),
    // Equity history is not included in portfolio WS messages.
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })
}

export function usePositions() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.positions, queryFn: api.positions, refetchInterval, select: (d) => d.positions })
}

export function useOrders(limit = 100) {
  // Orders have no dedicated WS topic (including automatic hedge orders).
  return useQuery({ queryKey: qk.orders(limit), queryFn: () => api.orders(limit), refetchInterval: 10_000, select: (d) => d.orders })
}

export function useWallets() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.wallets, queryFn: api.wallets, refetchInterval, select: (d) => d.wallets })
}

export function useWallet(id: string) {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.wallet(id), queryFn: () => api.wallet(id), refetchInterval })
}

export function useWalletTrades(id: string, limit = 20) {
  const refetchInterval = usePollInterval()
  return useQuery({
    queryKey: qk.walletTrades(id, limit),
    queryFn: () => api.walletTrades(id, limit),
    select: (d) => d.trades,
    refetchInterval,
  })
}

const WALLET_TRADES_PAGE = 100

export function useWalletTradesInfinite(id: string) {
  const refetchInterval = usePollInterval()
  return useInfiniteQuery({
    queryKey: qk.walletTradesInfinite(id),
    queryFn: ({ pageParam }) => api.walletTrades(id, WALLET_TRADES_PAGE, pageParam),
    initialPageParam: undefined as string | number | undefined,
    refetchInterval,
    getNextPageParam: (last) =>
      last.trades.length < WALLET_TRADES_PAGE ? undefined : last.trades[last.trades.length - 1]?.id,
  })
}

export function useSourceTrades(limit = 50) {
  const refetchInterval = usePollInterval()
  return useQuery({
    queryKey: qk.sourceTrades(limit),
    queryFn: () => api.sourceTrades(limit),
    refetchInterval,
    select: (d) => d.trades,
  })
}

export function useCopySignals(limit = 50) {
  const refetchInterval = usePollInterval()
  return useQuery({
    queryKey: qk.copySignals(limit),
    queryFn: () => api.copySignals(limit),
    refetchInterval,
    select: (d) => d.signals,
  })
}

export function useHedges() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.hedges, queryFn: api.hedgeOpportunities, refetchInterval })
}

export function useHedgeEvents() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.hedgeEvents, queryFn: () => api.hedgeEvents(100), refetchInterval, select: (d) => d.events })
}

export function useSettings() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.settings, queryFn: api.settings, refetchInterval })
}

export function useSystemHealth() {
  // Health metrics are not pushed over WS; poll regardless of connection state.
  return useQuery({ queryKey: qk.systemHealth, queryFn: api.systemHealth, refetchInterval: 10_000 })
}

export function useSystemEvents() {
  const refetchInterval = usePollInterval()
  return useQuery({ queryKey: qk.systemEvents, queryFn: () => api.systemEvents(100), refetchInterval, select: (d) => d.events })
}

const REPLAY_POLL_MS = 2_000

/** Replay runs are not pushed over WS: poll every 2s while any run is RUNNING. */
export function useReplayRuns() {
  return useQuery({
    queryKey: qk.replayRuns,
    queryFn: api.replayRuns,
    select: (d) => d.runs,
    refetchInterval: (q) => (q.state.data?.runs.some((r) => r.status === 'RUNNING') ? REPLAY_POLL_MS : false),
  })
}

export function useReplayRun(id: string | null) {
  return useQuery({
    queryKey: qk.replayRun(id ?? ''),
    queryFn: () => api.replayRun(id!),
    enabled: id !== null,
    refetchInterval: (q) => (q.state.data?.status === 'RUNNING' ? REPLAY_POLL_MS : false),
  })
}
