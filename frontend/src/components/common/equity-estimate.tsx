import type { EquityCoverage } from '@/lib/api'
import { fmtNum, fmtUsd } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Unavailable } from './num'

/**
 * Source-wallet equity estimate. Lower-bound estimates (cash unobservable) are always labelled:
 * "≥ $1,234 · lower bound · conf 0.62".
 */
export function EquityEstimate({
  equity,
  coverage,
  lowerBound,
  confidence,
  size = 'sm',
  className,
}: {
  equity: string | null
  coverage: EquityCoverage | string | null
  lowerBound: boolean
  confidence: string | null
  size?: 'sm' | 'lg'
  className?: string
}) {
  const value = fmtUsd(equity, { dp: 0 })
  if (value === null || coverage === 'UNAVAILABLE') {
    return <Unavailable reason="no equity snapshot" className={className} />
  }
  const isLower = lowerBound || coverage === 'POSITIONS_ONLY'
  const conf = fmtNum(confidence, 2)
  return (
    <span className={cn('inline-flex flex-wrap items-baseline gap-x-1.5', className)}>
      <span className={cn('num whitespace-nowrap', size === 'lg' ? 'text-2xl font-medium' : 'text-[12.5px]')}>
        {isLower ? `≥ ${value}` : value}
      </span>
      {isLower && <span className="text-[11px] whitespace-nowrap text-warn">· lower bound</span>}
      {conf && <span className="num text-[11px] whitespace-nowrap text-muted-foreground">· conf {conf}</span>}
    </span>
  )
}
