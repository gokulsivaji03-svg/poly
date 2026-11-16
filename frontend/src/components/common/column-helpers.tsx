import type { ColumnDef, Row } from '@tanstack/react-table'
import { compareDecimal } from '@/lib/decimal'
import { TimeCell } from './cells'
import { Num, type NumKind } from './num'

/** Exact ordering for decimal strings / ints (alphanumeric sort would put "10" before "9"). */
export function decimalSort<T>(a: Row<T>, b: Row<T>, columnId: string): number {
  return compareDecimal(String(a.getValue(columnId)), String(b.getValue(columnId))) ?? 0
}

type Accessor<T> = (row: T) => string | number | null | undefined

export interface NumColOptions {
  dp?: number
  signed?: boolean
  pnl?: boolean
  compact?: boolean
  label?: string
  className?: string
  enableHiding?: boolean
}

/** Right-aligned numeric column; nulls render as the unavailable marker and sort last. */
export function numCol<T>(id: string, header: string, get: Accessor<T>, kind: NumKind, opts: NumColOptions = {}): ColumnDef<T> {
  return {
    id,
    header,
    accessorFn: (row) => get(row) ?? undefined,
    sortingFn: decimalSort,
    sortUndefined: 'last',
    enableHiding: opts.enableHiding,
    meta: { align: 'right', label: opts.label ?? header, className: opts.className },
    cell: ({ row }) => (
      <Num value={get(row.original)} kind={kind} dp={opts.dp} signed={opts.signed} pnl={opts.pnl} compact={opts.compact} />
    ),
  }
}

export function timeCol<T>(id: string, header: string, get: (row: T) => string | null | undefined, relative = false): ColumnDef<T> {
  return {
    id,
    header,
    accessorFn: (row) => get(row) ?? undefined,
    sortUndefined: 'last',
    meta: { label: header },
    cell: ({ row }) => <TimeCell iso={get(row.original)} relative={relative} />,
  }
}
