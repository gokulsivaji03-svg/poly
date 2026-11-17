import { ArrowRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { DataTable } from '@/components/common/data-table'
import { Num } from '@/components/common/num'
import { Panel, Stat } from '@/components/common/panel'
import { EmptyState, ErrorState, SkeletonLines } from '@/components/common/states'
import { Skeleton } from '@/components/ui/skeleton'
import { blotterColumns, CopyTimeline, Premium } from '@/features/copy-blotter'
import { EquityChartPanel } from '@/features/equity-chart-panel'
import { FeedStatus } from '@/features/feed-status'
import { sourceTradeColumnsCompact } from '@/features/feed-columns'
import { positionColumnsCompact } from '@/features/position-columns'
import { StageLatency } from '@/features/stage-latency'
import { humanizeCode } from '@/lib/format'
import { useCopyQuality, useCopySignals, useDashboard, usePositions, useSourceTrades, useSystemHealth } from '@/lib/queries'

const WINDOW_H = 24

function ViewAll({ to, label = 'All' }: { to: string; label?: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
      {label} <ArrowRight className="size-3" />
    </Link>
  )
}

/** Account on the left, execution quality on the right: what the edge is and what it costs. */
function Strip() {
  const dash = useDashboard()
  const quality = useCopyQuality(WINDOW_H)
  const p = dash.data?.portfolio
  const c = quality.data

  if (!dash.data && dash.isError) {
    return (
      <Panel>
        <ErrorState error={dash.error} onRetry={() => void dash.refetch()} title="Failed to load portfolio" />
      </Panel>
    )
  }

  const v = (ready: unknown, node: ReactNode) => (ready ? node : <Skeleton className="h-5 w-20" />)

  return (
    <section
      aria-label="Account and execution quality"
      className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border bg-border sm:grid-cols-4 xl:grid-cols-8 [&>*]:bg-card"
    >
      <Stat
        testId="stat-equity"
        label="Equity"
        value={v(p, <Num value={p?.equity} kind="usd" />)}
        sub={
          p &&
          (p.unmarked_positions > 0 ? (
            <span className="text-warn">{p.unmarked_positions} unmarked</span>
          ) : (
            <>
              day <Num value={p.day_pnl} kind="usd" signed pnl />
            </>
          ))
        }
      />
      <Stat
        testId="stat-exposure"
        label="Exposure"
        value={v(p, <Num value={p?.positions_value} kind="usd" />)}
        sub={
          p && (
            <>
              cash <Num value={p.available_cash} kind="usd" />
            </>
          )
        }
      />
      <Stat
        testId="stat-pnl"
        label="Realized P&L"
        value={v(p, <Num value={p?.realized_pnl} kind="usd" signed pnl />)}
        sub={
          p && (
            <>
              unreal <Num value={p.unrealized_pnl} kind="usd" signed pnl />
            </>
          )
        }
      />
      <Stat
        testId="stat-premium"
        label={`Premium p50 · ${WINDOW_H}h`}
        value={v(c, <Premium value={c?.premium_p50} />)}
        sub={
          c && (
            <>
              p90 <Premium value={c.premium_p90} />
            </>
          )
        }
      />
      <Stat
        testId="stat-at-source"
        label="At source price"
        value={v(c, <Num value={c?.at_source_share} kind="pct" dp={0} unavailableReason="no fills yet" />)}
        sub={c && `of ${c.filled} fills`}
      />
      <Stat
        testId="stat-detect-to-fill"
        label="Detect → fill p50"
        value={v(c, <Num value={c?.detect_to_fill_ms_p50} kind="ms" unavailableReason="no fills yet" />)}
        sub={
          c && (
            <>
              p99 <Num value={c.detect_to_fill_ms_p99} kind="ms" />
            </>
          )
        }
      />
      <Stat
        testId="stat-copies"
        label={`Copies · ${WINDOW_H}h`}
        value={v(c, <Num value={c?.filled} kind="int" />)}
        sub={
          c && (
            <>
              {c.skipped} skipped
              {c.failed > 0 && <span className="text-neg"> · {c.failed} failed</span>}
            </>
          )
        }
      />
      <Stat
        testId="stat-state"
        label="Engine"
        value={v(
          dash.data,
          dash.data?.emergency_stop ? (
            <span className="text-neg">KILLED</span>
          ) : dash.data?.strategies_paused ? (
            <span className="text-warn">PAUSED</span>
          ) : dash.data?.copy_enabled ? (
            <span className="text-pos">ARMED</span>
          ) : (
            <span className="text-muted-foreground">COPY OFF</span>
          ),
        )}
        sub={dash.data && `${dash.data.followed_wallets} wallet${dash.data.followed_wallets === 1 ? '' : 's'}`}
      />
    </section>
  )
}

function Blotter() {
  const q = useCopySignals(100)
  return (
    <Panel title="Copy blotter" actions={<ViewAll to="/activity?tab=signals" />}>
      <DataTable
        testId="signal-feed"
        data={q.data}
        columns={blotterColumns}
        getRowId={(s) => String(s.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={520}
        renderExpanded={(s) => <CopyTimeline signal={s} />}
        empty={<EmptyState title="No copies yet." description="Each source trade lands here with its fill against the source price." />}
      />
    </Panel>
  )
}

function Skips() {
  const q = useCopyQuality(WINDOW_H)
  const reasons = q.data?.skip_reasons ?? []
  const widest = Math.max(...reasons.map((r) => r.n), 1)
  return (
    <Panel title={`Skips · ${WINDOW_H}h`}>
      {q.isPending ? (
        <SkeletonLines rows={3} />
      ) : reasons.length === 0 ? (
        <EmptyState title="None." />
      ) : (
        <ul className="flex flex-col gap-1.5 px-3 py-2">
          {reasons.map((r) => (
            <li key={r.code} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5">
              <span className="truncate text-[12px]" title={r.code}>
                {humanizeCode(r.code)}
              </span>
              <span className="num text-[12px] text-muted-foreground">{r.n}</span>
              <span className="col-span-2 h-1 rounded-full bg-muted">
                <span className="block h-1 rounded-full bg-warn/70" style={{ width: `${(r.n / widest) * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function Latency() {
  const q = useSystemHealth()
  if (!q.data) return <Panel title="Copy latency by stage">{q.isError ? <ErrorState error={q.error} /> : <SkeletonLines rows={6} />}</Panel>
  return <StageLatency stages={q.data.metrics.copy_stages} />
}

function SourceTape() {
  const q = useSourceTrades(50)
  return (
    <Panel title="Source tape" actions={<ViewAll to="/activity?tab=source" />}>
      <DataTable
        testId="source-feed"
        data={q.data}
        columns={sourceTradeColumnsCompact}
        getRowId={(t) => String(t.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={300}
        empty={
          <EmptyState
            title="No source trades yet."
            action={
              <Link to="/wallets" className="text-[11.5px] text-primary hover:underline">
                Follow a wallet
              </Link>
            }
          />
        }
      />
    </Panel>
  )
}

function Positions() {
  const q = usePositions()
  return (
    <Panel title="Positions" actions={<ViewAll to="/positions" />}>
      <DataTable
        data={q.data}
        columns={positionColumnsCompact}
        getRowId={(p) => p.token_id}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={300}
        initialSorting={[{ id: 'unrealized_pnl', desc: true }]}
        empty={<EmptyState title="Flat." />}
      />
    </Panel>
  )
}

export function DashboardPage() {
  return (
    <>
      <Strip />
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Blotter />
        <div className="flex min-w-0 flex-col gap-3">
          <Latency />
          <FeedStatus />
          <Skips />
        </div>
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
        <SourceTape />
        <Positions />
        <EquityChartPanel />
      </div>
    </>
  )
}
