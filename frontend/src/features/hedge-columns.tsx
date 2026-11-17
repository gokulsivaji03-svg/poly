import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Zap } from 'lucide-react'
import { toast } from 'sonner'
import { HedgeStateBadge, OutcomeBadge, Tag } from '@/components/common/badges'
import { MarketCell, ReasonsCell, TimeCell } from '@/components/common/cells'
import { numCol } from '@/components/common/column-helpers'
import { ConfirmDialog } from '@/components/common/confirm-dialog'
import { KV } from '@/components/common/panel'
import { Num } from '@/components/common/num'
import { Button } from '@/components/ui/button'
import { api, type HedgeOpportunity } from '@/lib/api'
import { addDecimal, compareDecimal, negateDecimal } from '@/lib/decimal'
import { fmtMs, fmtShares, humanizeCode } from '@/lib/format'
import { qk } from '@/lib/queries'
import { isMergeable, MergePairsButton } from './merge-pairs-button'

/** Viable first, then by total net profit (desc); nulls last. */
export function sortOpportunities(list: readonly HedgeOpportunity[]): HedgeOpportunity[] {
  return [...list].sort((a, b) => {
    if (a.viable !== b.viable) return a.viable ? -1 : 1
    if (a.net_profit === null) return b.net_profit === null ? 0 : 1
    if (b.net_profit === null) return -1
    return compareDecimal(b.net_profit, a.net_profit) ?? 0
  })
}

function ExecuteHedgeButton({ opp }: { opp: HedgeOpportunity }) {
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => api.executeHedge(opp.id),
    onSuccess: (res) => {
      if (res.status === 'REJECTED' || res.status === 'FAILED') {
        toast.error(`Hedge ${res.status.toLowerCase()}`, { description: res.reason ?? undefined })
      } else {
        const parts = [
          res.matched_shares ? `${fmtShares(res.matched_shares)} shares matched` : null,
          res.state ? `now ${humanizeCode(res.state).toLowerCase()}` : null,
        ].filter(Boolean)
        toast.success(`Paper hedge ${res.status.toLowerCase()}`, { description: parts.join(' · ') || undefined })
      }
      void qc.invalidateQueries({ queryKey: qk.hedges })
      void qc.invalidateQueries({ queryKey: qk.positions })
      void qc.invalidateQueries({ queryKey: qk.hedgeEvents })
      void qc.invalidateQueries({ queryKey: qk.dashboard })
      void qc.invalidateQueries({ queryKey: ['orders'] })
    },
  })
  return (
    <ConfirmDialog
      trigger={
        <Button size="xs" variant="outline" className="border-primary/40 text-primary hover:bg-primary/10" data-testid="hedge-execute">
          <Zap /> Execute paper hedge
        </Button>
      }
      title="Execute paper hedge?"
      description={
        <>
          Buy <strong className="text-foreground">{opp.hedge_outcome ?? 'opposite'}</strong> against your{' '}
          <strong className="text-foreground">{opp.primary_outcome}</strong> position in “{opp.question}”. The order is
          re-priced against the live book at execution; fills may differ from this quote.
        </>
      }
      confirmLabel="Execute hedge"
      onConfirm={() => mutation.mutateAsync()}
    >
      <div className="rounded-sm border bg-background/60 px-3 py-1.5">
        <KV label="Matched shares">
          <Num value={opp.matched_shares} kind="shares" />
        </KV>
        <KV label="Avg hedge price">
          <Num value={opp.hedge_avg_price} kind="cents" />
        </KV>
        <KV label="Combined cost / share">
          <Num value={opp.combined_cost_per_share} kind="cents" />
        </KV>
        <KV label="Capital required">
          <Num value={opp.capital_required} kind="usd" />
        </KV>
        <KV label="Locked profit (net)">
          <Num value={opp.net_profit} kind="usd" signed pnl />
        </KV>
        <KV label="Book age">
          <span className="num">{fmtMs(opp.book_age_ms) ?? '—'}</span>
        </KV>
      </div>
    </ConfirmDialog>
  )
}

const marketCol: ColumnDef<HedgeOpportunity> = {
  id: 'question',
  header: 'Market',
  accessorFn: (o) => o.question,
  meta: { label: 'Market' },
  cell: ({ row: { original: o } }) => <MarketCell title={o.question} className="max-w-[260px]" />,
}

const primaryCol: ColumnDef<HedgeOpportunity> = {
  id: 'primary_outcome',
  header: 'Held',
  accessorFn: (o) => o.primary_outcome,
  meta: { label: 'Existing position outcome' },
  cell: ({ row: { original: o } }) => <OutcomeBadge outcome={o.primary_outcome} />,
}

const viableCol: ColumnDef<HedgeOpportunity> = {
  id: 'viable',
  header: 'Viable',
  accessorFn: (o) => (o.viable ? 1 : 0),
  meta: { label: 'Viable / reasons' },
  cell: ({ row: { original: o } }) => (o.viable ? <Tag tone="pos">viable</Tag> : <ReasonsCell reasons={o.reasons} />),
}

export const hedgeColumns: ColumnDef<HedgeOpportunity>[] = [
  marketCol,
  {
    id: 'state',
    header: 'State',
    accessorFn: (o) => o.state,
    meta: { label: 'Hedge state' },
    cell: ({ row: { original: o } }) => <HedgeStateBadge state={o.state} />,
  },
  viableCol,
  primaryCol,
  numCol<HedgeOpportunity>('existing_shares', 'Held sh', (o) => o.existing_shares, 'shares', { label: 'Existing shares' }),
  numCol<HedgeOpportunity>('unhedged_shares', 'Unhedged', (o) => o.unhedged_shares, 'shares', { label: 'Unhedged shares' }),
  numCol<HedgeOpportunity>('existing_entry_price', 'Entry', (o) => o.existing_entry_price, 'cents', { label: 'Existing entry price' }),
  {
    id: 'hedge_outcome',
    header: 'Hedge leg',
    accessorFn: (o) => o.hedge_outcome ?? undefined,
    meta: { label: 'Hedge outcome' },
    cell: ({ row: { original: o } }) => <OutcomeBadge outcome={o.hedge_outcome} />,
  },
  numCol<HedgeOpportunity>('hedge_best_ask', 'Best ask', (o) => o.hedge_best_ask, 'cents', { label: 'Opposite best ask' }),
  numCol<HedgeOpportunity>('hedge_avg_price', 'Avg', (o) => o.hedge_avg_price, 'cents', { label: 'Opposite avg price' }),
  numCol<HedgeOpportunity>('hedge_worst_price', 'Worst', (o) => o.hedge_worst_price, 'cents', { label: 'Opposite worst price' }),
  numCol<HedgeOpportunity>('combined_cost_per_share', 'Comb. cost', (o) => o.combined_cost_per_share, 'cents', {
    label: 'Combined cost / share',
  }),
  numCol<HedgeOpportunity>('matched_shares', 'Matched', (o) => o.matched_shares, 'shares', { label: 'Matched shares' }),
  numCol<HedgeOpportunity>('net_profit_per_share', 'Net/sh', (o) => o.net_profit_per_share, 'cents', {
    signed: true,
    pnl: true,
    label: 'Net profit / share',
  }),
  numCol<HedgeOpportunity>('net_profit', 'Locked profit', (o) => o.net_profit, 'usd', {
    signed: true,
    pnl: true,
    label: 'Total locked profit (net)',
  }),
  numCol<HedgeOpportunity>('gross_profit', 'Gross', (o) => o.gross_profit, 'usd', { signed: true, label: 'Gross profit' }),
  numCol<HedgeOpportunity>('primary_fees', 'Fees (held)', (o) => o.primary_fees, 'usd', { dp: 4, label: 'Primary fees' }),
  numCol<HedgeOpportunity>('hedge_fees', 'Fees (hedge)', (o) => o.hedge_fees, 'usd', { dp: 4, label: 'Hedge fees' }),
  numCol<HedgeOpportunity>('capital_required', 'Capital', (o) => o.capital_required, 'usd', { label: 'Capital required' }),
  numCol<HedgeOpportunity>('expected_return', 'Return', (o) => o.expected_return, 'pct', { label: 'Expected return' }),
  numCol<HedgeOpportunity>('depth_usd', 'Depth', (o) => o.depth_usd, 'usd', { dp: 0, label: 'Profitable depth (USD)' }),
  {
    id: 'quoted_at',
    header: 'Updated',
    accessorFn: (o) => o.quoted_at ?? undefined,
    sortUndefined: 'last',
    meta: { label: 'Last updated / book age' },
    cell: ({ row: { original: o } }) => (
      <span className="inline-flex items-baseline gap-1.5">
        <TimeCell iso={o.quoted_at} relative />
        {o.book_age_ms !== null && <span className="num text-[10.5px] text-faint">book {fmtMs(o.book_age_ms)}</span>}
      </span>
    ),
  },
  {
    id: 'action',
    header: '',
    enableSorting: false,
    enableHiding: false,
    meta: { align: 'right' },
    cell: ({ row: { original: o } }) => (
      <span className="inline-flex items-center justify-end gap-1.5">
        {o.viable && <ExecuteHedgeButton opp={o} />}
        {isMergeable(o.state) && (
          <MergePairsButton
            hedgeId={o.id}
            question={o.question}
            matchedShares={addDecimal(o.existing_shares, negateDecimal(o.unhedged_shares))}
          />
        )}
      </span>
    ),
  },
]

export const HEDGE_HIDDEN_BY_DEFAULT = ['gross_profit', 'primary_fees', 'hedge_fees', 'hedge_worst_price']
