import { Layers } from 'lucide-react'
import { useMemo, useState } from 'react'
import { DataTable } from '@/components/common/data-table'
import { PageHeader, Panel } from '@/components/common/panel'
import { SegmentedControl } from '@/components/common/segmented'
import { EmptyState } from '@/components/common/states'
import { LotsTable, positionColumns } from '@/features/position-columns'
import type { Position } from '@/lib/api'
import { usePositions } from '@/lib/queries'

type Filter = 'all' | 'unhedged' | 'hedged'
const FILTERS: Filter[] = ['all', 'unhedged', 'hedged']

const HEDGED_STATES: Partial<Record<Position['hedge_state'], true>> = { PARTIALLY_HEDGED: true, FULLY_HEDGED: true }

export function PositionsPage() {
  const q = usePositions()
  const [filter, setFilter] = useState<Filter>('all')
  const data = useMemo(() => {
    if (!q.data) return undefined
    if (filter === 'all') return q.data
    return q.data.filter((p) => (filter === 'hedged') === Boolean(HEDGED_STATES[p.hedge_state]))
  }, [q.data, filter])

  return (
    <>
      <PageHeader
        title="Positions"
        description="Open paper positions at conservative marks. Expand a row for its lots."
      />
      <Panel>
        <DataTable
          testId="positions-table"
          data={data}
          columns={positionColumns}
          getRowId={(p) => p.token_id}
          isLoading={q.isPending}
          error={q.error}
          onRetry={() => void q.refetch()}
          columnToggle
          initialHidden={['matched_shares', 'unmatched_shares', 'origins']}
          toolbar={
            <>
              <SegmentedControl options={FILTERS} value={filter} onChange={setFilter} label="Filter positions" />
              {q.data && <span className="num text-[11px] text-muted-foreground">{data?.length ?? 0} positions</span>}
            </>
          }
          renderExpanded={(p) => <LotsTable lots={p.lots} />}
          canExpand={(p) => p.lots.length > 0}
          empty={
            <EmptyState
              icon={Layers}
              title={filter === 'all' ? 'No open positions' : `No ${filter} positions`}
              description="Positions open when copied trades or paper hedges fill."
            />
          }
        />
      </Panel>
    </>
  )
}
