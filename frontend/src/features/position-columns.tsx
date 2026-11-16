import type { ColumnDef } from '@tanstack/react-table'
import { HedgeStateBadge, OutcomeBadge, Tag } from '@/components/common/badges'
import { MarketCell, MonoId } from '@/components/common/cells'
import { numCol, timeCol } from '@/components/common/column-helpers'
import { DataTable, expanderColumn } from '@/components/common/data-table'
import { Unavailable } from '@/components/common/num'
import type { Lot, Position } from '@/lib/api'
import { toNumber } from '@/lib/decimal'
import { fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { isMergeable, MergePairsButton } from './merge-pairs-button'

/** Hedged share fraction as a thin bar (width is visual only; the label is exact). */
export function HedgedBar({ fraction }: { fraction: string | null }) {
  const label = fmtPct(fraction, { dp: 0 })
  if (label === null) return <Unavailable />
  const pct = Math.min(100, Math.max(0, (toNumber(fraction) ?? 0) * 100))
  return (
    <span className="inline-flex items-center justify-end gap-2">
      <span className="relative h-1.5 w-14 overflow-hidden rounded-full bg-muted" aria-hidden>
        <span
          className={cn('absolute inset-y-0 left-0 rounded-full', pct >= 99.5 ? 'bg-pos' : 'bg-primary/80')}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="num w-9 text-right">{label}</span>
    </span>
  )
}

const market: ColumnDef<Position> = {
  id: 'question',
  header: 'Market',
  accessorFn: (p) => p.question,
  meta: { label: 'Market' },
  cell: ({ row: { original: p } }) => <MarketCell title={p.question} className="max-w-[300px]" />,
}

const outcome: ColumnDef<Position> = {
  id: 'outcome',
  header: 'Outcome',
  accessorFn: (p) => p.outcome,
  meta: { label: 'Outcome' },
  cell: ({ row: { original: p } }) => <OutcomeBadge outcome={p.outcome} />,
}

const hedgeState: ColumnDef<Position> = {
  id: 'hedge_state',
  header: 'Hedge',
  accessorFn: (p) => p.hedge_state,
  meta: { label: 'Hedge state' },
  cell: ({ row: { original: p } }) => <HedgeStateBadge state={p.hedge_state} />,
}

export const positionColumnsCompact: ColumnDef<Position>[] = [
  market,
  outcome,
  numCol<Position>('shares', 'Shares', (p) => p.shares, 'shares'),
  numCol<Position>('avg_entry', 'Entry', (p) => p.avg_entry, 'cents', { dp: 1 }),
  numCol<Position>('mark', 'Mark', (p) => p.mark, 'cents', { dp: 1 }),
  numCol<Position>('unrealized_pnl', 'uPnL', (p) => p.unrealized_pnl, 'usd', { signed: true, pnl: true }),
  hedgeState,
]

export const positionColumns: ColumnDef<Position>[] = [
  expanderColumn<Position>(),
  market,
  outcome,
  {
    id: 'origins',
    header: 'Origin',
    accessorFn: (p) => p.origins.join(','),
    meta: { label: 'Origin' },
    cell: ({ row: { original: p } }) => (
      <span className="flex gap-1">
        {p.origins.map((o) => (
          <Tag key={o} tone={o === 'HEDGE' ? 'accent' : 'plain'}>
            {o}
          </Tag>
        ))}
      </span>
    ),
  },
  numCol<Position>('shares', 'Shares', (p) => p.shares, 'shares'),
  numCol<Position>('avg_entry', 'Avg entry', (p) => p.avg_entry, 'cents', { dp: 2 }),
  numCol<Position>('executable_bid', 'Exec bid', (p) => p.executable_bid, 'cents', { dp: 2, label: 'Executable bid' }),
  numCol<Position>('mark', 'Mark', (p) => p.mark, 'cents', { dp: 2, label: 'Conservative mark' }),
  numCol<Position>('cost_basis', 'Cost basis', (p) => p.cost_basis, 'usd'),
  numCol<Position>('market_value', 'Value', (p) => p.market_value, 'usd', { label: 'Market value' }),
  numCol<Position>('unrealized_pnl', 'Unrealized', (p) => p.unrealized_pnl, 'usd', { signed: true, pnl: true, label: 'Unrealized P&L' }),
  {
    id: 'hedged_pct',
    header: 'Hedged',
    accessorFn: (p) => p.hedged_pct ?? undefined,
    sortUndefined: 'last',
    meta: { align: 'right', label: 'Hedged share %' },
    cell: ({ row: { original: p } }) => <HedgedBar fraction={p.hedged_pct} />,
  },
  numCol<Position>('matched_shares', 'Matched', (p) => p.matched_shares, 'shares', { label: 'Matched shares' }),
  numCol<Position>('unmatched_shares', 'Unmatched', (p) => p.unmatched_shares, 'shares', { label: 'Unmatched shares' }),
  numCol<Position>('locked_profit', 'Locked', (p) => p.locked_profit, 'usd', { signed: true, pnl: true, label: 'Net locked profit' }),
  hedgeState,
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    enableHiding: false,
    meta: { align: 'right' },
    // Merge is offered on the primary (copied) leg; its opportunity id is "<condition_id>:<token_id>".
    cell: ({ row: { original: p } }) =>
      isMergeable(p.hedge_state) && p.origins.includes('COPY') ? (
        <MergePairsButton hedgeId={`${p.condition_id}:${p.token_id}`} question={p.question} matchedShares={p.matched_shares} />
      ) : null,
  },
]

const lotColumns: ColumnDef<Lot>[] = [
  {
    id: 'id',
    header: 'Lot',
    accessorFn: (l) => String(l.id),
    meta: { label: 'Lot id' },
    cell: ({ row: { original: l } }) => <MonoId value={l.id} />,
  },
  {
    id: 'origin',
    header: 'Origin',
    accessorFn: (l) => l.origin,
    cell: ({ row: { original: l } }) => <Tag tone={l.origin === 'HEDGE' ? 'accent' : 'plain'}>{l.origin}</Tag>,
  },
  {
    id: 'source_wallet',
    header: 'Source wallet',
    accessorFn: (l) => l.source_wallet ?? undefined,
    cell: ({ row: { original: l } }) => <MonoId value={l.source_wallet} head={6} />,
  },
  numCol<Lot>('entry_price', 'Entry', (l) => l.entry_price, 'cents', { dp: 2 }),
  numCol<Lot>('shares_acquired', 'Acquired', (l) => l.shares_acquired, 'shares'),
  numCol<Lot>('shares_remaining', 'Remaining', (l) => l.shares_remaining, 'shares'),
  numCol<Lot>('matched_shares', 'Matched', (l) => l.matched_shares, 'shares'),
  numCol<Lot>('cost_remaining', 'Cost rem.', (l) => l.cost_remaining, 'usd'),
  numCol<Lot>('fee_usdc', 'Fee', (l) => l.fee_usdc, 'usd', { dp: 4 }),
  timeCol<Lot>('opened_at', 'Opened', (l) => l.opened_at),
]

export function LotsTable({ lots }: { lots: Lot[] }) {
  return (
    <div className="border-l-2 border-primary/40 py-1 pl-6">
      <div className="label-caps px-3 py-1.5">Lots · {lots.length}</div>
      <DataTable
        data={lots}
        columns={lotColumns}
        getRowId={(l) => String(l.id)}
        initialSorting={[{ id: 'opened_at', desc: false }]}
        empty={<div className="px-3 py-3 text-[12px] text-muted-foreground">No open lots.</div>}
      />
    </div>
  )
}
