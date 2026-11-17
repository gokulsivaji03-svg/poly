import type { ColumnDef } from '@tanstack/react-table'
import { CheckCircle2, Globe2, ScrollText, XCircle } from 'lucide-react'
import { LevelBadge, Tag, WorkerStateBadge } from '@/components/common/badges'
import { TimeCell } from '@/components/common/cells'
import { timeCol } from '@/components/common/column-helpers'
import { DataTable } from '@/components/common/data-table'
import { Num, Unavailable } from '@/components/common/num'
import { KV, PageHeader, Panel, Stat } from '@/components/common/panel'
import { EmptyState, ErrorState, SkeletonLines } from '@/components/common/states'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { StageLatency } from '@/features/stage-latency'
import type { SystemEvent, SystemHealth, Venue, WorkerStatus } from '@/lib/api'
import { useSystemEvents, useSystemHealth } from '@/lib/queries'
import { useRealtime } from '@/lib/realtime-context'

function BoolTag({ value, yes, no }: { value: boolean; yes: string; no: string }) {
  return <Tag tone={value ? 'pos' : 'muted'}>{value ? yes : no}</Tag>
}

function VenueCard({ v }: { v: Venue }) {
  return (
    <Panel title={v.name || v.id} actions={<Globe2 className="size-3.5 text-faint" />}>
      <div className="px-3 py-2">
        <KV label="Live supported">
          <BoolTag value={v.live_supported} yes="yes" no="no" />
        </KV>
        <KV label="Live enabled">
          <BoolTag value={v.live_enabled} yes="enabled" no="disabled" />
        </KV>
        <KV label="Geoblock">
          {v.geoblock == null ? (
            <Unavailable reason="not checked" />
          ) : (
            <Tag tone={v.geoblock.blocked ? 'neg' : 'pos'}>{v.geoblock.blocked ? 'blocked' : 'allowed'}</Tag>
          )}
        </KV>
        {v.geoblock && (
          <>
            <KV label="Location">
              <span className="num text-[12px]">{[v.geoblock.country, v.geoblock.region].filter(Boolean).join(' / ') || '—'}</span>
            </KV>
            <KV label="Checked">
              <TimeCell iso={v.geoblock.checked_at} relative />
            </KV>
          </>
        )}
        {v.reason && <p className="mt-1.5 rounded-sm border bg-background/60 px-2 py-1.5 text-[11.5px] text-muted-foreground">{v.reason}</p>}
      </div>
    </Panel>
  )
}

type WorkerRow = WorkerStatus & { name: string }

const workerColumns: ColumnDef<WorkerRow>[] = [
  {
    id: 'name',
    header: 'Worker',
    accessorFn: (w) => w.name,
    cell: ({ row: { original: w } }) => <span className="num text-[12px]">{w.name}</span>,
  },
  {
    id: 'state',
    header: 'State',
    accessorFn: (w) => w.state,
    cell: ({ row: { original: w } }) => <WorkerStateBadge state={w.state} />,
  },
  timeCol<WorkerRow>('last_run_at', 'Last run', (w) => w.last_run_at, true),
  {
    id: 'last_error',
    header: 'Last error',
    accessorFn: (w) => w.last_error ?? undefined,
    enableSorting: false,
    cell: ({ row: { original: w } }) =>
      w.last_error ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0} className="block max-w-[420px] truncate text-[12px] text-neg outline-none">
              {w.last_error}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-md">{w.last_error}</TooltipContent>
        </Tooltip>
      ) : (
        <span className="text-faint">—</span>
      ),
  },
]

function dataText(data: unknown): string {
  if (data == null) return ''
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data)
  } catch {
    return String(data)
  }
}

const eventColumns: ColumnDef<SystemEvent>[] = [
  timeCol<SystemEvent>('created_at', 'Time', (e) => e.created_at),
  {
    id: 'level',
    header: 'Level',
    accessorFn: (e) => e.level,
    cell: ({ row: { original: e } }) => <LevelBadge level={e.level} />,
  },
  {
    id: 'kind',
    header: 'Kind',
    accessorFn: (e) => e.kind,
    cell: ({ row: { original: e } }) => <span className="num text-[11.5px]">{e.kind}</span>,
  },
  {
    id: 'message',
    header: 'Message',
    accessorFn: (e) => e.message,
    enableSorting: false,
    cell: ({ row: { original: e } }) => <span className="block max-w-[520px] truncate" title={e.message}>{e.message}</span>,
  },
  {
    id: 'data',
    header: 'Data',
    enableSorting: false,
    cell: ({ row: { original: e } }) => {
      const text = dataText(e.data)
      return text ? (
        <span className="num block max-w-[320px] truncate text-[11px] text-muted-foreground" title={text}>
          {text}
        </span>
      ) : (
        <span className="text-faint">—</span>
      )
    },
  },
]

function Metrics({ h }: { h: SystemHealth }) {
  const { status } = useRealtime()
  const m = h.metrics
  return (
    <section aria-label="Metrics" className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border bg-border sm:grid-cols-3 xl:grid-cols-6 [&>*]:bg-card">
      <Stat label="Detection lag p50" value={<Num value={m.source_detection_lag_ms_p50} kind="ms" />} />
      <Stat label="Detection lag p95" value={<Num value={m.source_detection_lag_ms_p95} kind="ms" />} />
      <Stat label="Data API lag" value={<Num value={m.data_api_lag_s} kind="s" />} />
      <Stat label="Signal latency p50" value={<Num value={m.signal_latency_ms_p50} kind="ms" />} />
      <Stat label="Exec latency p50" value={<Num value={m.exec_latency_ms_p50} kind="ms" />} />
      <Stat label="Book age p50" value={<Num value={m.book_age_ms_p50} kind="ms" />} />
      <Stat
        label="Market data WS"
        value={m.ws_connected == null ? <Unavailable /> : m.ws_connected ? <span className="text-pos">connected</span> : <span className="text-neg">down</span>}
        sub={m.ws_reconnects != null ? `${m.ws_reconnects} reconnects` : undefined}
      />
      <Stat
        label="Chain log stream"
        value={m.chain_stream_connected == null ? <Unavailable /> : m.chain_stream_connected ? <span className="text-pos">connected</span> : <span className="text-neg">down</span>}
        sub={m.chain_stream_reconnects != null ? `${m.chain_stream_reconnects} reconnects` : undefined}
      />
      <Stat
        label="Mempool stream"
        value={m.mempool_stream_connected == null ? <Unavailable /> : m.mempool_stream_connected ? <span className="text-pos">connected</span> : <span className="text-neg">down</span>}
        sub={m.mempool_stream_reconnects != null ? `${m.mempool_stream_reconnects} reconnects` : undefined}
      />
      <Stat label="Terminal stream" value={<span className={status.state === 'connected' ? 'text-pos' : 'text-warn'}>{status.state}</span>} />
      <Stat label="HTTP errors 5m" value={<Num value={m.http_errors_5m} kind="int" />} />
      <Stat label="Last chain pass" value={<TimeCell iso={m.last_poll_at} relative />} />
      <Stat label="Mode" value={<span className="num">{h.mode}</span>} />
      <Stat
        label="Ledger"
        value={h.ledger_ok ? <span className="text-pos">consistent</span> : <span className="text-neg">{h.ledger_problems.length} problems</span>}
      />
    </section>
  )
}

export function SystemPage() {
  const q = useSystemHealth()
  const events = useSystemEvents()
  const h = q.data
  const workers: WorkerRow[] = h ? Object.entries(h.workers).map(([name, w]) => ({ name, ...w })) : []

  return (
    <>
      <PageHeader title="System" description="Venues, workers, pipeline latency and ledger integrity. Refreshes every 10s." />
      {q.isPending ? (
        <Panel>
          <SkeletonLines rows={8} />
        </Panel>
      ) : !h ? (
        <Panel>
          <ErrorState error={q.error} onRetry={() => void q.refetch()} title="Failed to load system health" />
        </Panel>
      ) : (
        <>
          <Metrics h={h} />
          <StageLatency stages={h.metrics.copy_stages ?? []} />
          <div className="grid gap-3 md:grid-cols-2">
            {h.venues.map((v) => (
              <VenueCard key={v.id} v={v} />
            ))}
          </div>
          <div className="grid gap-3 xl:grid-cols-[2fr_1fr]">
            <Panel title="Workers">
              <DataTable
                data={workers}
                columns={workerColumns}
                getRowId={(w) => w.name}
                empty={<EmptyState title="No workers reported" description="Workers start once this instance holds the strategy leader lock." />}
              />
            </Panel>
            <Panel
              title="Ledger"
              actions={
                h.ledger_ok ? <CheckCircle2 className="size-3.5 text-pos" /> : <XCircle className="size-3.5 text-neg" />
              }
            >
              {h.ledger_problems.length === 0 ? (
                <div className="px-3 py-6 text-center text-[12px] text-muted-foreground">
                  {h.ledger_ok ? 'No ledger problems detected.' : 'Ledger reported not OK without details.'}
                </div>
              ) : (
                <ul className="flex flex-col gap-1 px-3 py-2">
                  {h.ledger_problems.map((p, i) => (
                    <li key={i} className="rounded-sm border border-neg/30 bg-neg/10 px-2 py-1.5 text-[12px] text-neg">
                      {p}
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
        </>
      )}
      <Panel title="System events">
        <DataTable
          data={events.data}
          columns={eventColumns}
          getRowId={(e) => String(e.id)}
          isLoading={events.isPending}
          error={events.error}
          onRetry={() => void events.refetch()}
          initialSorting={[{ id: 'created_at', desc: true }]}
          maxHeight={520}
          empty={<EmptyState icon={ScrollText} title="No system events" />}
        />
      </Panel>
    </>
  )
}
