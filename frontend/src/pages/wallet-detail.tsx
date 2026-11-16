import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, BarChart3, Info, LineChart } from 'lucide-react'
import { useMemo } from 'react'
import { Link, useParams } from 'react-router'
import { AddressChip } from '@/components/common/address'
import { CoverageBadge, OutcomeBadge } from '@/components/common/badges'
import { MarketCell } from '@/components/common/cells'
import { numCol } from '@/components/common/column-helpers'
import { DataTable } from '@/components/common/data-table'
import { EquityEstimate } from '@/components/common/equity-estimate'
import { Num } from '@/components/common/num'
import { KV, Panel, Stat } from '@/components/common/panel'
import { EmptyState, ErrorState, SkeletonLines } from '@/components/common/states'
import { TimeSeriesChart, type ChartSeries } from '@/components/common/time-series-chart'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { sourceTradeColumns } from '@/features/feed-columns'
import { SizingOverrideDialog } from '@/features/sizing-override-dialog'
import { usePatchWallet } from '@/features/wallet-mutations'
import { ApiError, type SourcePosition, type SourceTrade, type WalletDetail } from '@/lib/api'
import { CHART_COLORS, toChartPoints } from '@/lib/chart-data'
import { fractionToPercent } from '@/lib/decimal'
import { fmtDateTime, fmtRelative, humanizeCode } from '@/lib/format'
import { useWallet, useWalletTradesInfinite } from '@/lib/queries'

const STATUS_COLOR: Record<string, string> = {
  EXECUTED: CHART_COLORS.pos,
  PARTIAL: CHART_COLORS.accent,
  SKIPPED: CHART_COLORS.muted,
  REJECTED: CHART_COLORS.info,
  FAILED: CHART_COLORS.neg,
}

const positionColumns: ColumnDef<SourcePosition>[] = [
  {
    id: 'title',
    header: 'Market',
    accessorFn: (p) => p.title,
    cell: ({ row: { original: p } }) => <MarketCell title={p.title} className="max-w-[320px]" />,
  },
  {
    id: 'outcome',
    header: 'Outcome',
    accessorFn: (p) => p.outcome,
    cell: ({ row: { original: p } }) => <OutcomeBadge outcome={p.outcome} />,
  },
  numCol<SourcePosition>('size', 'Shares', (p) => p.size, 'shares'),
  numCol<SourcePosition>('avg_price', 'Avg price', (p) => p.avg_price, 'cents'),
  numCol<SourcePosition>('current_price', 'Current', (p) => p.current_price, 'cents'),
  numCol<SourcePosition>('current_value', 'Value', (p) => p.current_value, 'usd'),
  numCol<SourcePosition>('realized_pnl', 'Realized', (p) => p.realized_pnl, 'usd', { signed: true, pnl: true }),
  numCol<SourcePosition>('unrealized_pnl', 'Unrealized', (p) => p.unrealized_pnl, 'usd', { signed: true, pnl: true }),
]

const tradeColumns = sourceTradeColumns.filter((c) => c.id !== 'wallet')

function Header({ w }: { w: WalletDetail }) {
  const patch = usePatchWallet()
  const isLower = w.equity_is_lower_bound || w.equity_coverage === 'POSITIONS_ONLY'
  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_auto]">
      <Panel bodyClassName="flex flex-col gap-3 p-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <Link to="/wallets" className="mb-1 inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-3" /> Wallets
          </Link>
          <h1 className="truncate text-[17px] font-semibold tracking-tight">{w.nickname ?? 'Unnamed wallet'}</h1>
          <AddressChip address={w.address} />
          <div className="mt-2 flex flex-wrap items-center gap-3 text-[12px]">
            <label className="inline-flex items-center gap-2">
              <Switch
                checked={w.follow}
                onCheckedChange={(follow) => patch.mutate({ id: w.id, body: { follow } })}
                aria-label="Copy this wallet"
                disabled={patch.isPending}
              />
              <span className={w.follow ? 'text-foreground' : 'text-muted-foreground'}>{w.follow ? 'Copying' : 'Monitoring only'}</span>
            </label>
            <span className="text-faint">|</span>
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              Sizing <SizingOverrideDialog wallet={w} />
            </span>
          </div>
        </div>
        <div className="flex flex-col gap-1 md:items-end md:text-right">
          <div className="label-caps">Estimated equity</div>
          <EquityEstimate
            equity={w.estimated_equity}
            coverage={w.equity_coverage}
            lowerBound={w.equity_is_lower_bound}
            confidence={w.equity_confidence}
            size="lg"
          />
          <div className="flex flex-wrap items-center gap-2 md:justify-end">
            <CoverageBadge coverage={w.equity_coverage} confidence={w.equity_confidence} />
            <span className="num text-[11px] text-muted-foreground" title={fmtDateTime(w.equity_as_of) ?? undefined}>
              as of {fmtRelative(w.equity_as_of) ?? '—'}
            </span>
          </div>
          {isLower && (
            <p className="mt-1 flex max-w-xs items-start gap-1.5 text-[11.5px] text-warn md:justify-end">
              <Info className="mt-0.5 size-3 shrink-0" />
              Cash unobservable — figure covers positions only and is a lower bound.
            </p>
          )}
        </div>
      </Panel>
      <Panel bodyClassName="px-3 py-2 lg:w-72">
        <KV label="Positions value">
          <Num value={w.positions_value} kind="usd" />
        </KV>
        <KV label="Cash balance">
          <Num value={w.cash_balance} kind="usd" unavailableReason="cash unobservable" />
        </KV>
        <KV label="Open positions">
          <Num value={w.open_positions} kind="int" />
        </KV>
        <KV label="Realized P&L (obs.)">
          <Num value={w.observed_realized_pnl} kind="usd" signed pnl />
        </KV>
        <KV label="Unrealized P&L (obs.)">
          <Num value={w.observed_unrealized_pnl} kind="usd" signed pnl />
        </KV>
        <KV label="Last trade">
          <span className="num">{fmtRelative(w.last_trade_at) ?? '—'}</span>
        </KV>
        {w.last_error && (
          <p className="mt-1 rounded-sm border border-neg/30 bg-neg/10 px-2 py-1 text-[11px] text-neg">{w.last_error}</p>
        )}
      </Panel>
    </div>
  )
}

function Metrics({ w }: { w: WalletDetail }) {
  const m = w.metrics
  const reasons = Object.entries(m.skip_reasons).sort((a, b) => b[1] - a[1])
  const maxCount = reasons[0]?.[1] ?? 0
  return (
    <div className="grid gap-3 lg:grid-cols-[2fr_1fr]">
      <section
        aria-label="Trader metrics"
        className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border bg-border sm:grid-cols-4 [&>*]:bg-card"
      >
        <Stat label="Trades / day" value={<Num value={m.trades_per_day} kind="num" dp={2} />} sub={`${m.trade_count} total`} />
        <Stat label="Avg trade" value={<Num value={m.avg_trade_usd} kind="usd" />} sub={`${m.buy_count} buys · ${m.sell_count} sells`} />
        <Stat label="Avg position" value={<Num value={m.avg_position_usd} kind="usd" />} />
        <Stat label="Copy rate" value={<Num value={m.copy_rate} kind="pct" dp={1} />} sub={`${m.copied_count} copied · ${m.skipped_count} skipped`} />
        <Stat label="Avg price diff" value={<Num value={m.avg_price_diff_bps} kind="bps" />} sub="fill vs source" />
        <Stat label="Median detect lag" value={<Num value={m.median_detection_lag_ms} kind="ms" />} />
        <Stat label="Trades 7d" value={<Num value={w.trades_7d} kind="int" />} />
        <Stat label="Ingestion delay" value={<Num value={w.ingestion_delay_ms} kind="ms" />} sub="median detect − block" />
      </section>
      <Panel title="Skip reasons">
        {reasons.length === 0 ? (
          <div className="px-3 py-6 text-center text-[12px] text-muted-foreground">No skipped signals.</div>
        ) : (
          <ul className="flex flex-col gap-1.5 px-3 py-2.5">
            {reasons.map(([code, count]) => (
              <li key={code} className="grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-0.5">
                <span className="truncate text-[12px]" title={code}>
                  {humanizeCode(code)}
                </span>
                <span className="num text-right text-[12px]">{count}</span>
                <span className="col-span-2 h-1 overflow-hidden rounded-full bg-muted">
                  <span className="block h-full bg-warn/70" style={{ width: `${maxCount ? (count / maxCount) * 100 : 0}%` }} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}

function Charts({ w }: { w: WalletDetail }) {
  const equitySeries = useMemo<ChartSeries[]>(
    () => [
      {
        id: 'equity',
        type: 'area',
        title: 'Equity',
        color: CHART_COLORS.accent,
        data: toChartPoints(w.equity_history, (p) => p.t, (p) => p.equity),
      },
      {
        id: 'positions',
        type: 'line',
        title: 'Positions',
        dashed: true,
        color: CHART_COLORS.info,
        data: toChartPoints(w.equity_history, (p) => p.t, (p) => p.positions_value),
      },
    ],
    [w.equity_history],
  )
  const allocSeries = useMemo<ChartSeries[]>(
    () => [
      {
        id: 'alloc',
        type: 'histogram',
        title: 'Alloc %',
        color: CHART_COLORS.accent,
        data: toChartPoints(
          w.allocation_history,
          (p) => p.t,
          (p) => fractionToPercent(p.source_alloc_pct),
          (p) => (p.signal_status ? STATUS_COLOR[p.signal_status] : undefined) ?? CHART_COLORS.muted,
        ),
      },
    ],
    [w.allocation_history],
  )
  const hasEquity = (equitySeries[0]?.data.length ?? 0) > 0
  const hasAlloc = (allocSeries[0]?.data.length ?? 0) > 0
  const lowerBoundPoints = w.equity_history.filter((p) => p.coverage === 'POSITIONS_ONLY').length

  return (
    <div className="grid gap-3 xl:grid-cols-2">
      <Panel
        title="Estimated equity history"
        actions={
          lowerBoundPoints > 0 && (
            <span className="text-[11px] text-warn">
              {lowerBoundPoints}/{w.equity_history.length} points lower bound
            </span>
          )
        }
      >
        <div className="h-[240px] p-2">
          {hasEquity ? (
            <TimeSeriesChart series={equitySeries} height={224} testId="wallet-equity-chart" />
          ) : (
            <EmptyState icon={LineChart} title="No equity snapshots yet" description="Snapshots are taken as the wallet is polled." />
          )}
        </div>
      </Panel>
      <Panel
        title="Per-trade allocation % of source equity"
        actions={
          <span className="flex flex-wrap gap-2 text-[10.5px] text-muted-foreground">
            {Object.entries(STATUS_COLOR).map(([s, c]) => (
              <span key={s} className="inline-flex items-center gap-1">
                <span className="size-2 rounded-[1px]" style={{ background: c }} />
                {s.toLowerCase()}
              </span>
            ))}
          </span>
        }
      >
        <div className="h-[240px] p-2">
          {hasAlloc ? (
            <TimeSeriesChart series={allocSeries} format="pct" height={224} testId="wallet-allocation-chart" />
          ) : (
            <EmptyState icon={BarChart3} title="No allocation history" description="Each source trade with a known equity contributes a bar." />
          )}
        </div>
      </Panel>
    </div>
  )
}

function TradeHistory({ walletId }: { walletId: string }) {
  const q = useWalletTradesInfinite(walletId)
  const trades = useMemo(() => q.data?.pages.flatMap((p) => p.trades), [q.data])
  return (
    <Panel
      title="Trade history"
      actions={trades && <span className="num text-[11px] text-muted-foreground">{trades.length} loaded</span>}
    >
      <DataTable<SourceTrade>
        data={trades}
        columns={tradeColumns}
        getRowId={(t) => String(t.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        columnToggle
        maxHeight={520}
        empty={<EmptyState title="No trades detected yet" description="Trades appear as soon as the wallet is polled." />}
      />
      {q.hasNextPage && (
        <div className="flex justify-center border-t p-2">
          <Button size="xs" variant="outline" onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>
            {q.isFetchingNextPage ? 'Loading…' : 'Load older trades'}
          </Button>
        </div>
      )}
    </Panel>
  )
}

export function WalletDetailPage() {
  const { id = '' } = useParams()
  const q = useWallet(id)

  if (q.isPending) {
    return (
      <Panel>
        <SkeletonLines rows={10} />
      </Panel>
    )
  }
  if (!q.data || (q.error instanceof ApiError && q.error.status === 404)) {
    if (q.error instanceof ApiError && q.error.status === 404) {
      return (
        <Panel>
          <EmptyState
            title="Wallet not found"
            description="It may have been removed."
            action={
              <Button asChild size="xs" variant="outline">
                <Link to="/wallets">Back to wallets</Link>
              </Button>
            }
          />
        </Panel>
      )
    }
    return (
      <Panel>
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      </Panel>
    )
  }
  const w = q.data
  return (
    <>
      <Header w={w} />
      <Metrics w={w} />
      <Charts w={w} />
      <Panel title={`Current source positions · ${w.positions.length}`}>
        <DataTable
          data={w.positions}
          columns={positionColumns}
          getRowId={(p) => p.token_id}
          initialSorting={[{ id: 'current_value', desc: true }]}
          maxHeight={420}
          empty={<EmptyState title="No open positions" description="This wallet currently holds no positions." />}
        />
      </Panel>
      <TradeHistory walletId={id} />
    </>
  )
}
