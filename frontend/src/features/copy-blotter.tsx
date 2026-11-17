/** The copy blotter: every copy against the source fill it followed, with what it cost in price and time. */
import type { ColumnDef } from '@tanstack/react-table'
import { OutcomeBadge, SideBadge, SignalStatusBadge } from '@/components/common/badges'
import { MarketCell, WalletCell } from '@/components/common/cells'
import { decimalSort, numCol, timeCol } from '@/components/common/column-helpers'
import { expanderColumn } from '@/components/common/data-table'
import { Num, Unavailable } from '@/components/common/num'
import type { CopySignal } from '@/lib/api'
import { signDecimal } from '@/lib/decimal'
import { fmtMs, fmtPrice, humanizeCode } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Premium in cents, signed: red when the copy paid more than the source, green when it beat it. */
export function Premium({ value, className }: { value: string | null | undefined; className?: string }) {
  const text = fmtPrice(value ?? null, { cents: true, dp: 1 })
  if (text === null) return <Unavailable reason="no fill" className={className} />
  const sign = signDecimal(String(value))
  return (
    <span
      className={cn(
        'num whitespace-nowrap',
        sign === 1 ? 'text-neg' : sign === -1 ? 'text-pos' : 'text-muted-foreground',
        className,
      )}
    >
      {sign === 1 ? '+' : ''}
      {text}
    </span>
  )
}

const market: ColumnDef<CopySignal> = {
  id: 'market',
  header: 'Market',
  accessorFn: (s) => s.title ?? undefined,
  meta: { label: 'Market' },
  cell: ({ row: { original: s } }) => (
    <span className="flex items-center gap-1.5">
      <SideBadge side={s.side} />
      <OutcomeBadge outcome={s.outcome} />
      <MarketCell title={s.title} className="max-w-[200px]" />
    </span>
  ),
}

const premium: ColumnDef<CopySignal> = {
  id: 'premium',
  header: 'Prem',
  accessorFn: (s) => s.premium ?? undefined,
  sortingFn: decimalSort,
  sortUndefined: 'last',
  meta: { align: 'right', label: 'Premium over source (¢)' },
  cell: ({ row: { original: s } }) => <Premium value={s.premium} />,
}

const path: ColumnDef<CopySignal> = {
  id: 'path',
  header: 'Path',
  accessorFn: (s) => s.stages_ms?.total,
  sortUndefined: 'last',
  meta: { align: 'right', label: 'Receipt to booked' },
  cell: ({ row: { original: s } }) => <Num value={s.stages_ms?.total} kind="ms" unavailableReason="not timed" />,
}

const outcome: ColumnDef<CopySignal> = {
  id: 'status',
  header: 'Outcome',
  accessorFn: (s) => s.status,
  meta: { label: 'Status / reason' },
  cell: ({ row: { original: s } }) => {
    const reason = s.status === 'SKIPPED' || s.status === 'REJECTED' ? s.reasons.at(-1) : undefined
    return (
      <span className="flex items-center gap-1.5">
        <SignalStatusBadge status={s.status} />
        {reason && (
          <span className="truncate text-[11px] text-muted-foreground" title={reason.message}>
            {humanizeCode(reason.code)}
          </span>
        )}
      </span>
    )
  },
}

export const blotterColumns: ColumnDef<CopySignal>[] = [
  expanderColumn<CopySignal>(),
  timeCol<CopySignal>('detected_at', 'Time', (s) => s.detected_at),
  {
    id: 'wallet',
    header: 'Wallet',
    accessorFn: (s) => s.wallet_nickname ?? s.wallet_address,
    meta: { label: 'Wallet' },
    cell: ({ row: { original: s } }) => <WalletCell id={s.wallet_id} nickname={s.wallet_nickname} address={s.wallet_address} />,
  },
  market,
  numCol<CopySignal>('source_price', 'Src', (s) => s.source_price, 'cents', { dp: 1, label: 'Source price' }),
  numCol<CopySignal>('avg_fill_price', 'Fill', (s) => s.avg_fill_price, 'cents', { dp: 1, label: 'Our average fill' }),
  premium,
  numCol<CopySignal>('filled_cost', 'Notional', (s) => s.filled_cost, 'usd', { label: 'Filled notional' }),
  numCol<CopySignal>('detection_lag_ms', 'Detect', (s) => s.detection_lag_ms, 'ms', { label: 'Block to detection' }),
  path,
  outcome,
]

const STAGE_ORDER = ['taped', 'started', 'context', 'decided', 'reserved', 'executed']

/** One copy's pipeline as a proportional strip, then its reasons. */
export function CopyTimeline({ signal }: { signal: CopySignal }) {
  const stages = signal.stages_ms
  const steps = stages ? STAGE_ORDER.filter((k) => stages[k] !== undefined).map((k) => [k, stages[k]!] as const) : []
  const total = stages?.total ?? 0
  return (
    <div className="grid gap-3 px-3 py-2.5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <div className="label-caps mb-1.5">
          Pipeline <span className="num normal-case">{fmtMs(total) ?? ''}</span>
        </div>
        {steps.length === 0 ? (
          <span className="text-[12px] text-muted-foreground">No stage timings recorded for this copy.</span>
        ) : (
          <>
            <div className="flex h-2 overflow-hidden rounded-[1px] bg-muted" aria-hidden>
              {steps.map(([k, ms], i) => (
                <div
                  key={k}
                  className={cn('h-full', i % 2 ? 'bg-primary/55' : 'bg-primary')}
                  style={{ width: `${total ? (ms / total) * 100 : 0}%` }}
                />
              ))}
            </div>
            <dl className="mt-1.5 grid grid-cols-3 gap-x-4 gap-y-0.5 sm:grid-cols-6">
              {steps.map(([k, ms]) => (
                <div key={k} className="min-w-0">
                  <dt className="truncate text-[10.5px] text-muted-foreground">{k}</dt>
                  <dd className="num text-[12px]">{fmtMs(ms)}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </div>
      <div className="min-w-0">
        <div className="label-caps mb-1.5">Reasons</div>
        {signal.reasons.length === 0 ? (
          <span className="text-[12px] text-muted-foreground">None.</span>
        ) : (
          <ul className="flex flex-col gap-1">
            {signal.reasons.map((r, i) => (
              <li key={`${r.code}-${i}`} className="text-[12px]">
                <span className="num text-[11px] text-warn">{r.code}</span>
                {r.message && r.message !== r.code && <span className="block text-muted-foreground">{r.message}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
