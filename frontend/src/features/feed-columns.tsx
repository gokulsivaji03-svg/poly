/** Column sets for source trades, copy signals and orders (full + compact variants). */
import type { ColumnDef } from '@tanstack/react-table'
import { ExternalLink } from 'lucide-react'
import { OutcomeBadge, SideBadge, SignalStatusBadge, Tag } from '@/components/common/badges'
import { MarketCell, MonoId, ReasonsCell, WalletCell } from '@/components/common/cells'
import { numCol, timeCol } from '@/components/common/column-helpers'
import { Num, Unavailable } from '@/components/common/num'
import type { CopySignal, Order, SourceTrade } from '@/lib/api'
import { fmtNum, fmtShares, truncateAddress } from '@/lib/format'

// ---------------------------------------------------------------------------------------------
// Source trades

const tradeWallet: ColumnDef<SourceTrade> = {
  id: 'wallet',
  header: 'Wallet',
  accessorFn: (t) => t.wallet_nickname ?? t.wallet_address,
  meta: { label: 'Wallet' },
  cell: ({ row: { original: t } }) => <WalletCell id={t.wallet_id} nickname={t.wallet_nickname} address={t.wallet_address} />,
}

const tradeSide: ColumnDef<SourceTrade> = {
  id: 'side',
  header: 'Side',
  accessorFn: (t) => t.side ?? t.type,
  meta: { label: 'Side' },
  cell: ({ row: { original: t } }) =>
    t.type === 'TRADE' ? <SideBadge side={t.side} /> : <Tag tone="info">{t.type}</Tag>,
}

const tradeOutcome: ColumnDef<SourceTrade> = {
  id: 'outcome',
  header: 'Outcome',
  accessorFn: (t) => t.outcome ?? undefined,
  meta: { label: 'Outcome' },
  cell: ({ row: { original: t } }) => <OutcomeBadge outcome={t.outcome} />,
}

const tradeMarket: ColumnDef<SourceTrade> = {
  id: 'market',
  header: 'Market',
  accessorFn: (t) => t.title ?? undefined,
  meta: { label: 'Market' },
  cell: ({ row: { original: t } }) => <MarketCell title={t.title} />,
}

const tradeSignal: ColumnDef<SourceTrade> = {
  id: 'signal',
  header: 'Signal',
  accessorFn: (t) => t.signal_status ?? undefined,
  meta: { label: 'Signal status' },
  cell: ({ row: { original: t } }) =>
    t.is_backfill ? <Tag tone="muted" title="Historical backfill — never copied">backfill</Tag> : <SignalStatusBadge status={t.signal_status} />,
}

export const sourceTradeColumnsCompact: ColumnDef<SourceTrade>[] = [
  timeCol<SourceTrade>('detected_at', 'Time', (t) => t.detected_at),
  tradeWallet,
  tradeSide,
  tradeOutcome,
  tradeMarket,
  numCol<SourceTrade>('price', 'Price', (t) => t.price, 'cents', { dp: 1 }),
  numCol<SourceTrade>('usdc_size', 'Notional', (t) => t.usdc_size, 'usd'),
  numCol<SourceTrade>('detection_lag_ms', 'Lag', (t) => t.detection_lag_ms, 'ms'),
  tradeSignal,
]

export const sourceTradeColumns: ColumnDef<SourceTrade>[] = [
  timeCol<SourceTrade>('detected_at', 'Detected', (t) => t.detected_at),
  timeCol<SourceTrade>('block_ts', 'Block time', (t) => t.block_ts),
  tradeWallet,
  tradeSide,
  tradeOutcome,
  tradeMarket,
  numCol<SourceTrade>('price', 'Price', (t) => t.price, 'cents', { dp: 2 }),
  numCol<SourceTrade>('size', 'Shares', (t) => t.size, 'shares'),
  numCol<SourceTrade>('usdc_size', 'Notional', (t) => t.usdc_size, 'usd'),
  numCol<SourceTrade>('detection_lag_ms', 'Detection lag', (t) => t.detection_lag_ms, 'ms'),
  tradeSignal,
  {
    id: 'tx_hash',
    header: 'Tx',
    accessorFn: (t) => t.tx_hash ?? undefined,
    enableSorting: false,
    meta: { label: 'Transaction' },
    cell: ({ row: { original: t } }) =>
      t.tx_hash ? (
        <a
          href={`https://polygonscan.com/tx/${t.tx_hash}`}
          target="_blank"
          rel="noreferrer noopener"
          className="num inline-flex items-center gap-1 text-[11.5px] text-muted-foreground hover:text-primary"
        >
          {truncateAddress(t.tx_hash, 6, 4)}
          <ExternalLink className="size-3" />
        </a>
      ) : (
        <Unavailable />
      ),
  },
]

// ---------------------------------------------------------------------------------------------
// Copy signals

const signalWallet: ColumnDef<CopySignal> = {
  id: 'wallet',
  header: 'Wallet',
  accessorFn: (s) => s.wallet_nickname ?? s.wallet_address,
  meta: { label: 'Wallet' },
  cell: ({ row: { original: s } }) => <WalletCell id={s.wallet_id} nickname={s.wallet_nickname} address={s.wallet_address} />,
}

const signalStatus: ColumnDef<CopySignal> = {
  id: 'status',
  header: 'Status',
  accessorFn: (s) => s.status,
  meta: { label: 'Status' },
  cell: ({ row: { original: s } }) => <SignalStatusBadge status={s.status} />,
}

const signalMarket: ColumnDef<CopySignal> = {
  id: 'market',
  header: 'Market',
  accessorFn: (s) => s.title ?? undefined,
  meta: { label: 'Market' },
  cell: ({ row: { original: s } }) => (
    <span className="flex items-center gap-1.5">
      <SideBadge side={s.side} />
      <OutcomeBadge outcome={s.outcome} />
      <MarketCell title={s.title} className="max-w-[220px]" />
    </span>
  ),
}

const signalFill: ColumnDef<CopySignal> = {
  id: 'fill',
  header: 'Fill',
  accessorFn: (s) => s.filled_shares ?? undefined,
  meta: { align: 'right', label: 'Fill (shares @ avg)' },
  cell: ({ row: { original: s } }) =>
    s.filled_shares === null ? (
      <Unavailable reason="no fill" />
    ) : (
      <span className="num whitespace-nowrap">
        {fmtShares(s.filled_shares)}
        <span className="text-muted-foreground"> @ </span>
        <Num value={s.avg_fill_price} kind="cents" dp={2} />
      </span>
    ),
}

const signalReasons: ColumnDef<CopySignal> = {
  id: 'reasons',
  header: 'Reasons',
  accessorFn: (s) => s.reasons.map((r) => r.code).join(','),
  enableSorting: false,
  meta: { label: 'Reasons' },
  cell: ({ row: { original: s } }) => <ReasonsCell reasons={s.reasons} />,
}

export const copySignalColumnsCompact: ColumnDef<CopySignal>[] = [
  timeCol<CopySignal>('detected_at', 'Time', (s) => s.detected_at),
  signalWallet,
  signalStatus,
  signalMarket,
  numCol<CopySignal>('source_alloc_pct', 'Src alloc', (s) => s.source_alloc_pct, 'pct', { dp: 4, label: 'Source allocation %' }),
  numCol<CopySignal>('proposed_pct', 'Copy %', (s) => s.proposed_pct, 'pct', { dp: 4, label: 'Proposed copy %' }),
  numCol<CopySignal>('target_budget', 'Target', (s) => s.target_budget, 'usd'),
  numCol<CopySignal>('approved_budget', 'Approved', (s) => s.approved_budget, 'usd'),
  signalFill,
  signalReasons,
]

export const copySignalColumns: ColumnDef<CopySignal>[] = [
  timeCol<CopySignal>('detected_at', 'Detected', (s) => s.detected_at),
  signalWallet,
  signalStatus,
  signalMarket,
  numCol<CopySignal>('source_price', 'Src price', (s) => s.source_price, 'cents', { label: 'Source price' }),
  numCol<CopySignal>('source_shares', 'Src shares', (s) => s.source_shares, 'shares', { label: 'Source shares' }),
  numCol<CopySignal>('source_cost', 'Src cost', (s) => s.source_cost, 'usd', { label: 'Source cost' }),
  {
    id: 'source_equity',
    header: 'Src equity',
    accessorFn: (s) => s.source_equity ?? undefined,
    sortUndefined: 'last',
    meta: { align: 'right', label: 'Source equity' },
    cell: ({ row: { original: s } }) => (
      <span className="inline-flex items-baseline gap-1">
        {s.source_equity_coverage === 'POSITIONS_ONLY' && s.source_equity !== null && <span className="text-warn">≥</span>}
        <Num value={s.source_equity} kind="usd" dp={0} />
        {s.source_confidence !== null && (
          <span className="num text-[10.5px] text-muted-foreground">conf {fmtNum(s.source_confidence, 2)}</span>
        )}
      </span>
    ),
  },
  numCol<CopySignal>('source_alloc_pct', 'Src alloc', (s) => s.source_alloc_pct, 'pct', { dp: 4, label: 'Source allocation %' }),
  numCol<CopySignal>('user_equity', 'Your equity', (s) => s.user_equity, 'usd', { dp: 0 }),
  numCol<CopySignal>('proposed_pct', 'Copy %', (s) => s.proposed_pct, 'pct', { dp: 4, label: 'Proposed copy %' }),
  numCol<CopySignal>('target_budget', 'Target', (s) => s.target_budget, 'usd'),
  numCol<CopySignal>('approved_budget', 'Approved', (s) => s.approved_budget, 'usd'),
  numCol<CopySignal>('limit_price', 'Limit', (s) => s.limit_price, 'cents', { label: 'Limit price' }),
  numCol<CopySignal>('filled_shares', 'Filled', (s) => s.filled_shares, 'shares', { label: 'Filled shares' }),
  numCol<CopySignal>('filled_cost', 'Fill cost', (s) => s.filled_cost, 'usd'),
  numCol<CopySignal>('avg_fill_price', 'Avg fill', (s) => s.avg_fill_price, 'cents', { label: 'Average fill price' }),
  numCol<CopySignal>('price_diff_bps', 'Δ vs src', (s) => s.price_diff_bps, 'bps', { label: 'Price diff vs source (bps)' }),
  signalReasons,
  numCol<CopySignal>('detection_lag_ms', 'Detect lag', (s) => s.detection_lag_ms, 'ms', { label: 'Detection latency' }),
  numCol<CopySignal>('signal_latency_ms', 'Signal lat', (s) => s.signal_latency_ms, 'ms', { label: 'Signal latency' }),
  numCol<CopySignal>('exec_latency_ms', 'Exec lat', (s) => s.exec_latency_ms, 'ms', { label: 'Execution latency' }),
  timeCol<CopySignal>('decided_at', 'Decided', (s) => s.decided_at),
  timeCol<CopySignal>('executed_at', 'Executed', (s) => s.executed_at),
  {
    id: 'order_id',
    header: 'Order',
    accessorFn: (s) => (s.order_id == null ? undefined : String(s.order_id)),
    enableSorting: false,
    meta: { label: 'Order id' },
    cell: ({ row: { original: s } }) => <MonoId value={s.order_id} />,
  },
]

// ---------------------------------------------------------------------------------------------
// Orders

export function orderColumns(questionByCondition: Map<string, string>): ColumnDef<Order>[] {
  return [
    timeCol<Order>('created_at', 'Created', (o) => o.created_at),
    {
      id: 'kind',
      header: 'Kind',
      accessorFn: (o) => o.kind,
      meta: { label: 'Kind' },
      cell: ({ row: { original: o } }) => <Tag tone={o.kind === 'HEDGE' ? 'accent' : 'plain'}>{o.kind}</Tag>,
    },
    {
      id: 'side',
      header: 'Side',
      accessorFn: (o) => o.side,
      meta: { label: 'Side' },
      cell: ({ row: { original: o } }) => <SideBadge side={o.side} />,
    },
    {
      id: 'status',
      header: 'Status',
      accessorFn: (o) => o.status,
      meta: { label: 'Status' },
      cell: ({ row: { original: o } }) => <SignalStatusBadge status={o.status} />,
    },
    {
      id: 'market',
      header: 'Market',
      accessorFn: (o) => questionByCondition.get(o.condition_id) ?? o.condition_id,
      meta: { label: 'Market' },
      cell: ({ row: { original: o } }) => {
        const q = questionByCondition.get(o.condition_id)
        return q ? <MarketCell title={q} /> : <MonoId value={o.condition_id} />
      },
    },
    {
      id: 'token_id',
      header: 'Token',
      accessorFn: (o) => o.token_id,
      enableSorting: false,
      meta: { label: 'Token id' },
      cell: ({ row: { original: o } }) => <MonoId value={o.token_id} head={6} />,
    },
    numCol<Order>('requested_budget', 'Req budget', (o) => o.requested_budget, 'usd', { label: 'Requested budget' }),
    numCol<Order>('requested_shares', 'Req shares', (o) => o.requested_shares, 'shares', { label: 'Requested shares' }),
    numCol<Order>('limit_price', 'Limit', (o) => o.limit_price, 'cents', { label: 'Limit price' }),
    numCol<Order>('gross_shares', 'Gross sh', (o) => o.fill?.gross_shares, 'shares', { label: 'Fill gross shares' }),
    numCol<Order>('net_shares', 'Net sh', (o) => o.fill?.net_shares, 'shares', { label: 'Fill net shares' }),
    numCol<Order>('avg_price', 'Avg px', (o) => o.fill?.avg_price, 'cents', { label: 'Fill avg price' }),
    numCol<Order>('worst_price', 'Worst px', (o) => o.fill?.worst_price, 'cents', { label: 'Fill worst price' }),
    numCol<Order>('notional', 'Notional', (o) => o.fill?.notional, 'usd', { label: 'Fill notional' }),
    numCol<Order>('fee_usdc', 'Fee', (o) => o.fill?.fee_usdc, 'usd', { dp: 4, label: 'Fee (USDC)' }),
    numCol<Order>('cash_delta', 'Cash Δ', (o) => o.fill?.cash_delta, 'usd', { signed: true, pnl: true, label: 'Cash delta' }),
    numCol<Order>('exec_latency_ms', 'Exec lat', (o) => o.exec_latency_ms, 'ms', { label: 'Execution latency' }),
    numCol<Order>('book_age_ms', 'Book age', (o) => o.book_age_ms, 'ms', { label: 'Book age' }),
    {
      id: 'reason',
      header: 'Reason',
      accessorFn: (o) => o.reason ?? undefined,
      enableSorting: false,
      meta: { label: 'Reason' },
      cell: ({ row: { original: o } }) => (o.reason ? <ReasonsCell reasons={[o.reason]} /> : <span className="text-faint">—</span>),
    },
    timeCol<Order>('completed_at', 'Completed', (o) => o.completed_at),
  ]
}
