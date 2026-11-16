import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { EquityCoverage, HedgeState, WorkerState } from '@/lib/api'
import { fmtNum, humanizeCode } from '@/lib/format'
import { cn } from '@/lib/utils'

export type Tone = 'pos' | 'neg' | 'warn' | 'info' | 'muted' | 'accent' | 'danger' | 'plain' | 'replay'

const TONE: Record<Tone, string> = {
  pos: 'border-pos/30 bg-pos/10 text-pos',
  neg: 'border-neg/30 bg-neg/10 text-neg',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  info: 'border-info/30 bg-info/10 text-info',
  muted: 'border-border bg-muted text-muted-foreground',
  accent: 'border-primary/40 bg-primary/15 text-primary',
  danger: 'border-destructive bg-destructive/25 text-[#f09591]',
  plain: 'border-border bg-transparent text-foreground/85',
  replay: 'border-replay/50 bg-replay/15 text-replay',
}

export function Tag({
  tone = 'muted',
  children,
  className,
  title,
}: {
  tone?: Tone
  children: ReactNode
  className?: string
  title?: ReactNode
}) {
  const badge = (
    <Badge
      variant="outline"
      className={cn(
        'h-[18px] rounded-sm px-1.5 py-0 text-[10px] leading-none font-medium tracking-wide uppercase',
        TONE[tone],
        className,
      )}
    >
      {children}
    </Badge>
  )
  if (!title) return badge
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-flex cursor-help outline-none">
          {badge}
        </span>
      </TooltipTrigger>
      <TooltipContent>{title}</TooltipContent>
    </Tooltip>
  )
}

export function OutcomeBadge({ outcome }: { outcome: string | null | undefined }) {
  if (!outcome) return <Tag tone="muted">?</Tag>
  const o = outcome.toUpperCase()
  const tone: Tone = o === 'YES' ? 'pos' : o === 'NO' ? 'neg' : 'plain'
  return <Tag tone={tone}>{outcome}</Tag>
}

export function SideBadge({ side }: { side: string | null | undefined }) {
  if (!side) return <Tag tone="muted">—</Tag>
  return <Tag tone={side === 'BUY' ? 'pos' : 'neg'}>{side}</Tag>
}

const HEDGE_TONE: Record<HedgeState, Tone> = {
  UNHEDGED: 'muted',
  HEDGE_OPPORTUNITY: 'accent',
  HEDGE_PENDING: 'info',
  PARTIALLY_HEDGED: 'warn',
  FULLY_HEDGED: 'pos',
  HEDGE_FAILED: 'neg',
  SETTLED: 'plain',
}

export function HedgeStateBadge({ state }: { state: string }) {
  return <Tag tone={HEDGE_TONE[state as HedgeState] ?? 'muted'}>{humanizeCode(state)}</Tag>
}

const SIGNAL_TONE: Record<string, Tone> = {
  PENDING: 'info',
  SKIPPED: 'muted',
  REJECTED: 'warn',
  EXECUTED: 'pos',
  PARTIAL: 'accent',
  FAILED: 'neg',
}

export function SignalStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Tag tone="muted">no signal</Tag>
  return <Tag tone={SIGNAL_TONE[status] ?? 'muted'}>{status}</Tag>
}

const WORKER_TONE: Record<WorkerState, Tone> = {
  running: 'pos',
  standby: 'info',
  stopped: 'muted',
  error: 'neg',
}

export function WorkerStateBadge({ state }: { state: string }) {
  return <Tag tone={WORKER_TONE[state as WorkerState] ?? 'muted'}>{state}</Tag>
}

const LEVEL_TONE: Record<string, Tone> = {
  debug: 'muted',
  info: 'info',
  warning: 'warn',
  warn: 'warn',
  error: 'neg',
  critical: 'danger',
}

export function LevelBadge({ level }: { level: string }) {
  return <Tag tone={LEVEL_TONE[level.toLowerCase()] ?? 'muted'}>{level}</Tag>
}

const COVERAGE_LABEL: Record<EquityCoverage, string> = {
  POSITIONS_AND_CASH: 'positions + cash',
  POSITIONS_ONLY: 'positions only',
  UNAVAILABLE: 'unavailable',
}

export function CoverageBadge({
  coverage,
  confidence,
}: {
  coverage: EquityCoverage | string | null | undefined
  confidence?: string | null
}) {
  const cov = (coverage ?? 'UNAVAILABLE') as EquityCoverage
  const tone: Tone = cov === 'POSITIONS_AND_CASH' ? 'pos' : cov === 'POSITIONS_ONLY' ? 'warn' : 'muted'
  const conf = fmtNum(confidence, 2)
  return (
    <Tag
      tone={tone}
      title={
        cov === 'POSITIONS_ONLY'
          ? 'Cash balance unobservable — equity is a lower bound (positions value only).'
          : cov === 'UNAVAILABLE'
            ? 'No equity estimate available.'
            : 'Equity covers positions and cash.'
      }
    >
      {COVERAGE_LABEL[cov] ?? cov}
      {conf && <span className="num ml-1 opacity-80">· {conf}</span>}
    </Tag>
  )
}
