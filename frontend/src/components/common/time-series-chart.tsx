import {
  AreaSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { CHART_COLORS, type ChartPoint } from '@/lib/chart-data'
import { cn } from '@/lib/utils'

export interface ChartSeries {
  id: string
  type: 'area' | 'line' | 'histogram'
  data: ChartPoint[]
  color: string
  title?: string
  /** Dashed thin line (secondary series). */
  dashed?: boolean
}

export type ValueFormat = 'usd' | 'pct'

const usdAxis = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
const pctAxis = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

function hexA(hex: string, alpha: number): string {
  const a = Math.round(alpha * 255)
    .toString(16)
    .padStart(2, '0')
  return `${hex}${a}`
}

/**
 * lightweight-charts wrapper. Values arrive pre-converted to numbers (plotting is the only place
 * decimal strings become floats). `pct` series are plotted in percent units.
 */
export function TimeSeriesChart({
  series,
  format = 'usd',
  height = 240,
  className,
  testId,
}: {
  series: ChartSeries[]
  format?: ValueFormat
  height?: number
  className?: string
  testId?: string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: CHART_COLORS.text,
        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
        fontSize: 10,
        // Credited once on the System page, as the library's licence requires.
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: CHART_COLORS.grid },
        horzLines: { color: CHART_COLORS.grid },
      },
      rightPriceScale: { borderColor: CHART_COLORS.border, scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderColor: CHART_COLORS.border, timeVisible: true, secondsVisible: false },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: CHART_COLORS.muted, labelBackgroundColor: '#1a1f26' },
        horzLine: { color: CHART_COLORS.muted, labelBackgroundColor: '#1a1f26' },
      },
      localization: {
        priceFormatter: (v: number) => (format === 'pct' ? `${pctAxis.format(v)}%` : usdAxis.format(v)),
      },
    })
    chartRef.current = chart
    return () => {
      chart.remove()
      chartRef.current = null
    }
  }, [format])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const added = series.map((s) => {
      const data = s.data.map((p) => ({ ...p, time: p.time as UTCTimestamp }))
      const common = { title: s.title ?? '', priceLineVisible: false, lastValueVisible: true }
      if (s.type === 'area') {
        const api = chart.addSeries(AreaSeries, {
          ...common,
          lineColor: s.color,
          lineWidth: 2,
          topColor: hexA(s.color, 0.28),
          bottomColor: hexA(s.color, 0.02),
        })
        api.setData(data)
        return api
      }
      if (s.type === 'line') {
        const api = chart.addSeries(LineSeries, {
          ...common,
          color: s.color,
          lineWidth: s.dashed ? 1 : 2,
          lineStyle: s.dashed ? LineStyle.Dashed : LineStyle.Solid,
        })
        api.setData(data)
        return api
      }
      const api = chart.addSeries(HistogramSeries, { ...common, color: s.color })
      api.setData(data)
      return api
    })
    chart.timeScale().fitContent()
    return () => {
      if (chartRef.current !== chart) return
      for (const api of added) chart.removeSeries(api)
    }
  }, [series, format])

  return <div ref={containerRef} data-testid={testId} className={cn('w-full', className)} style={{ height }} />
}
