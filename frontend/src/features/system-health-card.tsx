import { ArrowRight } from 'lucide-react'
import { Link } from 'react-router'
import { Tag, WorkerStateBadge } from '@/components/common/badges'
import { KV, Panel } from '@/components/common/panel'
import { Num, Unavailable } from '@/components/common/num'
import { ErrorState, SkeletonLines } from '@/components/common/states'
import { useSystemHealth } from '@/lib/queries'
import { useRealtime } from '@/lib/realtime-context'

export function SystemHealthCard({ className }: { className?: string }) {
  const q = useSystemHealth()
  const { status } = useRealtime()
  const h = q.data
  return (
    <Panel
      title="System health"
      className={className}
      actions={
        <Link to="/system" className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
          Details <ArrowRight className="size-3" />
        </Link>
      }
    >
      {q.isPending ? (
        <SkeletonLines rows={8} />
      ) : !h ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <div className="grid gap-x-6 px-3 py-2 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
          <div>
            <div className="label-caps pt-1 pb-0.5">Latency</div>
            <KV label="Detection lag p50 / p95">
              <Num value={h.metrics.source_detection_lag_ms_p50} kind="ms" />
              <span className="text-faint"> / </span>
              <Num value={h.metrics.source_detection_lag_ms_p95} kind="ms" />
            </KV>
            <KV label="Data API lag">
              <Num value={h.metrics.data_api_lag_s} kind="s" />
            </KV>
            <KV label="Signal latency p50">
              <Num value={h.metrics.signal_latency_ms_p50} kind="ms" />
            </KV>
            <KV label="Exec latency p50">
              <Num value={h.metrics.exec_latency_ms_p50} kind="ms" />
            </KV>
            <KV label="Book age p50">
              <Num value={h.metrics.book_age_ms_p50} kind="ms" />
            </KV>
          </div>
          <div>
            <div className="label-caps pt-1 pb-0.5">Status</div>
            <KV label="Market data WS">
              {h.metrics.ws_connected == null ? (
                <Unavailable />
              ) : (
                <Tag tone={h.metrics.ws_connected ? 'pos' : 'neg'}>{h.metrics.ws_connected ? 'connected' : 'down'}</Tag>
              )}
            </KV>
            <KV label="Terminal stream">
              <Tag tone={status.state === 'connected' ? 'pos' : status.state === 'connecting' ? 'warn' : 'neg'}>{status.state}</Tag>
            </KV>
            <KV label="Ledger">
              <Tag tone={h.ledger_ok ? 'pos' : 'neg'}>{h.ledger_ok ? 'ok' : `${h.ledger_problems.length} problems`}</Tag>
            </KV>
            <KV label="HTTP errors (5m)">
              <Num value={h.metrics.http_errors_5m} kind="int" />
            </KV>
            {Object.entries(h.workers).map(([name, w]) => (
              <KV key={name} label={<span className="num text-[11.5px]">{name}</span>}>
                <WorkerStateBadge state={w.state} />
              </KV>
            ))}
          </div>
        </div>
      )}
    </Panel>
  )
}
