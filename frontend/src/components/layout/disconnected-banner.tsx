import { WifiOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useRealtime } from '@/lib/realtime-context'

function useSecondsUntil(target: number | null): number | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (target === null) return
    const t = window.setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [target])
  return target === null ? null : Math.max(0, Math.ceil((target - now) / 1000))
}

/** Shown while the realtime stream is down after a failed/lost connection. */
export function DisconnectedBanner() {
  const { status, reconnectNow } = useRealtime()
  const seconds = useSecondsUntil(status.nextRetryAt)
  const visible = status.state !== 'connected' && status.attempt > 0
  if (!visible) return null
  return (
    <div
      role="status"
      data-testid="disconnected-banner"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-warn/25 bg-warn/[0.07] px-3 py-1.5 text-[12px]"
    >
      <WifiOff className="size-3.5 text-warn" aria-hidden />
      <span className="text-foreground/90">
        {status.everConnected ? 'Realtime connection lost.' : 'Cannot reach the realtime stream.'}
      </span>
      <span className="text-muted-foreground">
        {status.state === 'connecting'
          ? `Reconnecting (attempt ${status.attempt + 1})…`
          : seconds !== null
            ? `Retrying in ${seconds}s (attempt ${status.attempt + 1}).`
            : 'Reconnecting…'}{' '}
        Data refreshes every 10s meanwhile.
      </span>
      {status.state === 'disconnected' && (
        <Button size="xs" variant="outline" className="ml-auto" onClick={reconnectNow}>
          Retry now
        </Button>
      )}
    </div>
  )
}
