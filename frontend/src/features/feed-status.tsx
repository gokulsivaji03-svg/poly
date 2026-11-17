import { ArrowRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { Tag, type Tone } from '@/components/common/badges'
import { KV, Panel } from '@/components/common/panel'
import { Num } from '@/components/common/num'
import { ErrorState, SkeletonLines } from '@/components/common/states'
import { useSystemHealth } from '@/lib/queries'
import { useRealtime } from '@/lib/realtime-context'

function Up({ ok, children }: { ok: boolean | null; children: ReactNode }) {
  const tone: Tone = ok == null ? 'muted' : ok ? 'pos' : 'neg'
  return <Tag tone={tone}>{children}</Tag>
}

/** Every input the copy path depends on, in one place. Healthy workers collapse to a count. */
export function FeedStatus({ className }: { className?: string }) {
  const q = useSystemHealth()
  const { status } = useRealtime()
  const h = q.data
  const m = h?.metrics
  const workers = h ? Object.entries(h.workers) : []
  const down = workers.filter(([, w]) => w.state === 'error' || w.state === 'stopped')
  const up = m?.mempool_streams_up ?? 0
  const total = m?.mempool_streams_total ?? 0
  return (
    <Panel
      title="Feeds"
      className={className}
      actions={
        <Link to="/system" className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
          System <ArrowRight className="size-3" />
        </Link>
      }
    >
      {q.isPending ? (
        <SkeletonLines rows={6} />
      ) : !h || !m ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <div className="px-3 py-1.5">
          <KV label="Mempool">
            {total === 0 ? (
              <Tag tone="muted">off</Tag>
            ) : (
              <Up ok={up > 0}>
                {up}/{total} up
              </Up>
            )}
            {total > 0 && (
              <span className="num ml-1.5 text-[11px] text-muted-foreground">{m.mempool_stream_reconnects ?? 0} rc</span>
            )}
          </KV>
          <KV label="Chain logs">
            <Up ok={m.chain_stream_connected}>{m.chain_stream_connected ? 'up' : 'down'}</Up>
            <span className="num ml-1.5 text-[11px] text-muted-foreground">{m.chain_stream_reconnects ?? 0} rc</span>
          </KV>
          <KV label="Order books">
            <Up ok={m.ws_connected}>{m.ws_connected ? 'up' : 'down'}</Up>
            <span className="ml-1.5 text-[11px] text-muted-foreground">
              age <Num value={m.book_age_ms_p50} kind="ms" />
            </span>
          </KV>
          <KV label="Data API lag">
            <Num value={m.data_api_lag_s} kind="s" />
          </KV>
          <KV label="Detect lag p50 / p95">
            <Num value={m.source_detection_lag_ms_p50} kind="ms" />
            <span className="text-faint"> / </span>
            <Num value={m.source_detection_lag_ms_p95} kind="ms" />
          </KV>
          <KV label="HTTP errors 5m">
            <Num value={m.http_errors_5m} kind="int" className={m.http_errors_5m ? 'text-warn' : undefined} />
          </KV>
          <KV label="Ledger">
            <Up ok={h.ledger_ok}>{h.ledger_ok ? 'consistent' : `${h.ledger_problems.length} problems`}</Up>
          </KV>
          <KV label="Terminal">
            <Up ok={status.state === 'connected'}>{status.state}</Up>
          </KV>
          <KV label="Workers">
            {down.length === 0 ? (
              <span className="num text-[12px] text-muted-foreground">
                {workers.length}/{workers.length} running
              </span>
            ) : (
              <span className="flex flex-wrap justify-end gap-1">
                {down.map(([name]) => (
                  <Tag key={name} tone="neg" className="normal-case">
                    {name}
                  </Tag>
                ))}
              </span>
            )}
          </KV>
        </div>
      )}
    </Panel>
  )
}
