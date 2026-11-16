import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api } from './api'
import { applyRealtimeMessage } from './realtime-cache'
import { RealtimeContext, type RealtimeContextValue } from './realtime-context'
import { buildWsUrl, RealtimeSocket, type ConnStatus } from './ws'

const INITIAL: ConnStatus = { state: 'connecting', attempt: 0, nextRetryAt: null, everConnected: false }

/** Owns the single app-wide WebSocket connection and exposes its status via context. */
export function RealtimeProvider({ token, children }: { token: string; children: ReactNode }) {
  const qc = useQueryClient()
  const [status, setStatus] = useState<ConnStatus>(INITIAL)
  const sockRef = useRef<RealtimeSocket | null>(null)

  useEffect(() => {
    const sock = new RealtimeSocket({
      url: () => buildWsUrl(token),
      onMessage: (msg) => applyRealtimeMessage(qc, msg),
      onStatus: setStatus,
      // An auth-flavoured close may be a stale token: confirm via REST, whose 401 handler logs out.
      onAuthFailure: () => void api.me().catch(() => undefined),
    })
    sockRef.current = sock
    sock.start()
    const onOnline = () => sock.reconnectNow()
    window.addEventListener('online', onOnline)
    return () => {
      window.removeEventListener('online', onOnline)
      sock.stop()
      sockRef.current = null
    }
  }, [qc, token])

  const value = useMemo<RealtimeContextValue>(
    () => ({ status, reconnectNow: () => sockRef.current?.reconnectNow() }),
    [status],
  )
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>
}
