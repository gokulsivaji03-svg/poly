import { useMemo } from 'react'
import { useSearchParams } from 'react-router'
import { DataTable } from '@/components/common/data-table'
import { PageHeader, Panel } from '@/components/common/panel'
import { EmptyState } from '@/components/common/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { copySignalColumns, orderColumns, sourceTradeColumns } from '@/features/feed-columns'
import { useCopySignals, useOrders, usePositions, useSourceTrades } from '@/lib/queries'

const TABS = ['source', 'signals', 'orders'] as const
type Tab = (typeof TABS)[number]

const FEED_LIMIT = 200

function SourceTradesTab() {
  const q = useSourceTrades(FEED_LIMIT)
  return (
    <DataTable
      testId="activity-source-table"
      data={q.data}
      columns={sourceTradeColumns}
      getRowId={(t) => String(t.id)}
      isLoading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      columnToggle
      toolbar={<span className="text-[11px] text-muted-foreground">Latest {FEED_LIMIT} detected source events</span>}
      empty={<EmptyState title="No source trades yet" description="Trades from monitored wallets appear here as they are detected." />}
    />
  )
}

function SignalsTab() {
  const q = useCopySignals(FEED_LIMIT)
  return (
    <DataTable
      testId="activity-signals-table"
      data={q.data}
      columns={copySignalColumns}
      getRowId={(s) => String(s.id)}
      isLoading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      columnToggle
      initialHidden={['limit_price', 'filled_cost', 'decided_at', 'order_id']}
      toolbar={<span className="text-[11px] text-muted-foreground">Sizing, risk reductions and fills per source trade</span>}
      empty={<EmptyState title="No copy signals yet" description="Signals are generated for every detected trade of a copied wallet." />}
    />
  )
}

function OrdersTab() {
  const q = useOrders(100)
  const positions = usePositions()
  const columns = useMemo(() => {
    const map = new Map<string, string>()
    for (const p of positions.data ?? []) map.set(p.condition_id, p.question)
    return orderColumns(map)
  }, [positions.data])
  return (
    <DataTable
      testId="activity-orders-table"
      data={q.data}
      columns={columns}
      getRowId={(o) => String(o.id)}
      isLoading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      columnToggle
      initialHidden={['token_id', 'gross_shares', 'worst_price']}
      empty={<EmptyState title="No orders yet" description="Paper orders from copy signals and hedges are listed here with fill details." />}
    />
  )
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: Tab = TABS.includes(raw as Tab) ? (raw as Tab) : 'source'

  return (
    <>
      <PageHeader title="Activity" description="Full audit trail: detected source events, copy decisions and paper orders." />
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })} className="gap-3">
        <TabsList className="h-8 rounded-sm">
          <TabsTrigger value="source" className="rounded-[2px] px-3 text-[12px]" data-testid="tab-source">
            Source trades
          </TabsTrigger>
          <TabsTrigger value="signals" className="rounded-[2px] px-3 text-[12px]" data-testid="tab-signals">
            Copy signals
          </TabsTrigger>
          <TabsTrigger value="orders" className="rounded-[2px] px-3 text-[12px]" data-testid="tab-orders">
            Orders
          </TabsTrigger>
        </TabsList>
        <Panel>
          <TabsContent value="source">
            <SourceTradesTab />
          </TabsContent>
          <TabsContent value="signals">
            <SignalsTab />
          </TabsContent>
          <TabsContent value="orders">
            <OrdersTab />
          </TabsContent>
        </Panel>
      </Tabs>
    </>
  )
}
