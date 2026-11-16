import { LineChart } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Panel } from '@/components/common/panel'
import { SegmentedControl } from '@/components/common/segmented'
import { EmptyState, ErrorState } from '@/components/common/states'
import { TimeSeriesChart, type ChartSeries } from '@/components/common/time-series-chart'
import { Skeleton } from '@/components/ui/skeleton'
import type { EquityRange } from '@/lib/api'
import { CHART_COLORS, toChartPoints } from '@/lib/chart-data'
import { useEquityHistory } from '@/lib/queries'

const RANGES: EquityRange[] = ['1d', '7d', '30d', 'all']

export function EquityChartPanel({ className }: { className?: string }) {
  const [range, setRange] = useState<EquityRange>('7d')
  const q = useEquityHistory(range)

  const series = useMemo<ChartSeries[]>(() => {
    const points = q.data?.points ?? []
    return [
      { id: 'equity', type: 'area', title: 'Equity', color: CHART_COLORS.accent, data: toChartPoints(points, (p) => p.t, (p) => p.equity) },
      {
        id: 'cash',
        type: 'line',
        title: 'Cash',
        dashed: true,
        color: CHART_COLORS.info,
        data: toChartPoints(points, (p) => p.t, (p) => p.cash),
      },
    ]
  }, [q.data])

  const hasData = (series[0]?.data.length ?? 0) > 0

  return (
    <Panel
      title="Equity curve"
      className={className}
      actions={<SegmentedControl options={RANGES} value={range} onChange={setRange} label="Equity range" />}
    >
      <div data-testid="equity-chart" className="relative h-[260px] p-2">
        {q.isPending ? (
          <Skeleton className="h-full w-full" />
        ) : q.isError && !q.data ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : !hasData ? (
          <EmptyState
            icon={LineChart}
            title="No equity history yet"
            description="Equity snapshots appear once the paper account starts trading or marking positions."
          />
        ) : (
          <TimeSeriesChart series={series} height={244} />
        )}
      </div>
    </Panel>
  )
}
