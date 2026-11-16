/** Conversion of API time series to lightweight-charts points (the only place floats appear). */
import { toNumber } from './decimal'

export interface ChartPoint {
  /** UTC seconds. */
  time: number
  value: number
  color?: string
}

/**
 * Map rows to ascending, unique-time points, dropping rows whose value is unavailable.
 * Rows sharing a second keep the last value (lightweight-charts requires strictly ascending time).
 */
export function toChartPoints<R>(
  rows: readonly R[],
  getTime: (r: R) => string,
  getValue: (r: R) => string | null | undefined,
  getColor?: (r: R) => string | undefined,
): ChartPoint[] {
  const byTime = new Map<number, ChartPoint>()
  for (const r of rows) {
    const ms = Date.parse(getTime(r))
    const value = toNumber(getValue(r))
    if (Number.isNaN(ms) || value === null || !Number.isFinite(value)) continue
    const time = Math.floor(ms / 1000)
    const color = getColor?.(r)
    byTime.set(time, color ? { time, value, color } : { time, value })
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

export const CHART_COLORS = {
  accent: '#d9a441',
  replay: '#a58bf0',
  pos: '#4ea97c',
  neg: '#cf625d',
  info: '#6f97c6',
  muted: '#4b535e',
  text: '#7a8491',
  grid: '#141920',
  border: '#1d232a',
} as const
