import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { signDecimal } from '@/lib/decimal'
import {
  fmtBps,
  fmtInt,
  fmtMs,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtSeconds,
  fmtShares,
  fmtUsd,
  UNAVAILABLE,
} from '@/lib/format'
import { cn } from '@/lib/utils'

/** Em dash marking an unavailable (unknown — not zero) value. */
export function Unavailable({ reason, className }: { reason?: string; className?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          data-unavailable=""
          aria-label="unavailable"
          className={cn('num cursor-help text-faint outline-none', className)}
        >
          {UNAVAILABLE}
        </span>
      </TooltipTrigger>
      <TooltipContent>{reason ? `unavailable — ${reason}` : 'unavailable'}</TooltipContent>
    </Tooltip>
  )
}

export type NumKind = 'usd' | 'shares' | 'price' | 'cents' | 'pct' | 'bps' | 'int' | 'ms' | 's' | 'num'

export interface NumProps {
  value: string | number | null | undefined
  kind?: NumKind
  dp?: number
  signed?: boolean
  compact?: boolean
  /** Colour by sign (P&L / direction). */
  pnl?: boolean
  className?: string
  /** Shown in the unavailable tooltip. */
  unavailableReason?: string
}

function formatNum({ value, kind = 'num', dp, signed, compact }: NumProps): string | null {
  const s = value == null ? null : String(value)
  switch (kind) {
    case 'usd':
      return fmtUsd(s, { dp, signed, compact })
    case 'shares':
      return fmtShares(s, dp)
    case 'price':
      return fmtPrice(s, { dp })
    case 'cents':
      return fmtPrice(s, { cents: true, dp })
    case 'pct':
      return fmtPct(s, { dp, signed })
    case 'bps':
      return fmtBps(s, { dp, signed })
    case 'int':
      return fmtInt(s)
    case 'ms':
      return fmtMs(s)
    case 's':
      return fmtSeconds(s)
    case 'num':
      return fmtNum(s, dp ?? 2)
  }
}

/** Monospace tabular number; renders the unavailable marker for null. */
export function Num(props: NumProps) {
  const text = formatNum(props)
  if (text === null) return <Unavailable reason={props.unavailableReason} className={props.className} />
  let tone = ''
  if (props.pnl) {
    const sign = signDecimal(String(props.value))
    tone = sign === 1 ? 'text-pos' : sign === -1 ? 'text-neg' : 'text-muted-foreground'
  }
  return <span className={cn('num whitespace-nowrap', tone, props.className)}>{text}</span>
}
