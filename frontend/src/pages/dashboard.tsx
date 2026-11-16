import { ArrowRight, Radio, ShieldHalf, Wallet, Zap } from 'lucide-react'
import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router'
import { DataTable } from '@/components/common/data-table'
import { Num } from '@/components/common/num'
import { Panel, Stat } from '@/components/common/panel'
import { EmptyState, ErrorState } from '@/components/common/states'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { EquityChartPanel } from '@/features/equity-chart-panel'
import { copySignalColumnsCompact, sourceTradeColumnsCompact } from '@/features/feed-columns'
import { hedgeColumnsCompact, sortOpportunities } from '@/features/hedge-columns'
import { positionColumnsCompact } from '@/features/position-columns'
import { SystemHealthCard } from '@/features/system-health-card'
import { useCopySignals, useDashboard, useHedges, usePositions, useSourceTrades } from '@/lib/queries'

function ViewAll({ to }: { to: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
      View all <ArrowRight className="size-3" />
    </Link>
  )
}

function StatStrip() {
  const q = useDashboard()
  const d = q.data
  const p = d?.portfolio

  if (!d && q.isError) {
    return (
      <Panel>
        <ErrorState error={q.error} onRetry={() => void q.refetch()} title="Failed to load portfolio" />
      </Panel>
    )
  }

  const v = (node: ReactNode) => (d ? node : <Skeleton className="h-5 w-24" />)

  return (
    <section
      aria-label="Portfolio summary"
      className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border bg-border sm:grid-cols-4 xl:grid-cols-8 [&>*]:bg-card"
    >
      <Stat
        testId="stat-equity"
        label="Active bankroll"
        value={v(<Num value={p?.equity} kind="usd" />)}
        sub={
          p &&
          (p.unmarked_positions > 0 ? (
            <span className="text-warn">{p.unmarked_positions} unmarked — partial</span>
          ) : (
            <>
              day <Num value={p.day_pnl} kind="usd" signed pnl />
            </>
          ))
        }
      />
      <Stat
        testId="stat-cash"
        label="Available cash"
        value={v(<Num value={p?.available_cash} kind="usd" />)}
        sub={
          p && (
            <>
              reserved <Num value={p.reserved} kind="usd" />
            </>
          )
        }
      />
      <Stat
        testId="stat-exposure"
        label="Open exposure"
        value={v(<Num value={p?.positions_value} kind="usd" />)}
        sub={
          p && (
            <>
              invested <Num value={p.invested} kind="usd" />
            </>
          )
        }
      />
      <Stat
        testId="stat-realized"
        label="Realized P&L"
        value={v(<Num value={p?.realized_pnl} kind="usd" signed pnl />)}
        sub={
          p && (
            <>
              fees <Num value={p.fees_paid} kind="usd" />
            </>
          )
        }
      />
      <Stat
        testId="stat-unrealized"
        label="Unrealized P&L"
        value={v(<Num value={p?.unrealized_pnl} kind="usd" signed pnl />)}
        sub="at conservative mark"
      />
      <Stat
        testId="stat-locked"
        label="Locked profit"
        value={v(<Num value={p?.locked_profit} kind="usd" signed pnl />)}
        sub="net hedge, booked fills"
      />
      <Stat
        testId="stat-wallets"
        label="Followed wallets"
        value={v(<Num value={d?.followed_wallets} kind="int" />)}
        sub={<Link to="/wallets" className="hover:text-foreground">manage</Link>}
      />
      <Stat
        testId="stat-copied-today"
        label="Copied today"
        value={v(<Num value={d?.trades_copied_today} kind="int" />)}
        sub={d && (d.copy_enabled ? 'copying enabled' : <span className="text-warn">copying disabled</span>)}
      />
    </section>
  )
}

function SourceFeed() {
  const q = useSourceTrades(50)
  return (
    <Panel title="Live source trades" actions={<ViewAll to="/activity?tab=source" />} className="xl:col-span-2">
      <DataTable
        testId="source-feed"
        data={q.data}
        columns={sourceTradeColumnsCompact}
        getRowId={(t) => String(t.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={360}
        empty={
          <EmptyState
            icon={Radio}
            title="No source trades yet"
            description="Trades from followed wallets stream in here as they are detected."
            action={
              <Button asChild size="xs" variant="outline">
                <Link to="/wallets">
                  <Wallet /> Add a wallet
                </Link>
              </Button>
            }
          />
        }
      />
    </Panel>
  )
}

function SignalFeed() {
  const q = useCopySignals(50)
  return (
    <Panel title="Copy execution" actions={<ViewAll to="/activity?tab=signals" />} className="xl:col-span-3">
      <DataTable
        testId="signal-feed"
        data={q.data}
        columns={copySignalColumnsCompact}
        getRowId={(s) => String(s.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={360}
        empty={
          <EmptyState
            icon={Zap}
            title="No copy signals yet"
            description="Each detected source trade produces a signal showing allocation, sizing, risk reductions and fills."
          />
        }
      />
    </Panel>
  )
}

function PositionsCompact() {
  const q = usePositions()
  return (
    <Panel title="Active positions" actions={<ViewAll to="/positions" />}>
      <DataTable
        data={q.data}
        columns={positionColumnsCompact}
        getRowId={(p) => p.token_id}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={320}
        initialSorting={[{ id: 'unrealized_pnl', desc: true }]}
        empty={<EmptyState title="No open positions" description="Positions open when copied trades fill." />}
      />
    </Panel>
  )
}

function HedgesCompact() {
  const q = useHedges()
  const data = useMemo(() => (q.data ? sortOpportunities(q.data.opportunities) : undefined), [q.data])
  return (
    <Panel title="Hedge opportunities" actions={<ViewAll to="/hedges" />}>
      <DataTable
        data={data}
        columns={hedgeColumnsCompact}
        getRowId={(o) => o.id}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        maxHeight={320}
        empty={
          <EmptyState
            icon={ShieldHalf}
            title="No hedge candidates"
            description="The scanner evaluates every open position against the complementary outcome's order book."
          />
        }
      />
    </Panel>
  )
}

export function DashboardPage() {
  return (
    <>
      <StatStrip />
      <div className="grid gap-3 xl:grid-cols-3">
        <EquityChartPanel className="xl:col-span-2" />
        <SystemHealthCard />
      </div>
      <div className="grid gap-3 xl:grid-cols-5">
        <SourceFeed />
        <SignalFeed />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <PositionsCompact />
        <HedgesCompact />
      </div>
    </>
  )
}
