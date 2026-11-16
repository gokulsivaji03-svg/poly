import { createContext, useContext } from 'react'
import type { ConnStatus } from './ws'

export interface RealtimeContextValue {
  status: ConnStatus
  reconnectNow: () => void
}

export const RealtimeContext = createContext<RealtimeContextValue | null>(null)

const OFFLINE: RealtimeContextValue = {
  status: { state: 'disconnected', attempt: 0, nextRetryAt: null, everConnected: false },
  reconnectNow: () => {},
}

export function useRealtime(): RealtimeContextValue {
  return useContext(RealtimeContext) ?? OFFLINE
}

/**
 * Polling fallback: `ms` while the realtime socket is not connected, otherwise disabled.
 * Outside a RealtimeProvider (tests, login page) no polling happens.
 */
export function usePollInterval(ms = 10_000): number | false {
  const ctx = useContext(RealtimeContext)
  if (!ctx) return false
  return ctx.status.state === 'connected' ? false : ms
}
