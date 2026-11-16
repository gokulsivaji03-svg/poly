import {
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getSortedRowModel,
  useReactTable,
  type Column,
  type ColumnDef,
  type ExpandedState,
  type RowData,
  type SortingState,
  type VisibilityState,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ChevronRight, Columns3 } from 'lucide-react'
import { Fragment, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { EmptyState, ErrorState } from './states'

declare module '@tanstack/react-table' {
  interface ColumnMeta<TData extends RowData, TValue> {
    align?: 'left' | 'right' | 'center'
    /** Applied to both header and cells. */
    className?: string
    /** Label in the column-visibility menu (defaults to the column id). */
    label?: string
  }
}

export interface DataTableProps<T> {
  data: T[] | undefined
  // TanStack column value types are heterogeneous per column, hence `any`.
  columns: ColumnDef<T, any>[]
  getRowId?: (row: T) => string
  testId?: string
  isLoading?: boolean
  error?: unknown
  onRetry?: () => void
  empty?: ReactNode
  initialSorting?: SortingState
  initialHidden?: string[]
  /** Show the column-visibility menu. */
  columnToggle?: boolean
  toolbar?: ReactNode
  renderExpanded?: (row: T) => ReactNode
  canExpand?: (row: T) => boolean
  /** Cap height and scroll with a sticky header. */
  maxHeight?: number | string
  rowClassName?: (row: T) => string | undefined
  className?: string
  skeletonRows?: number
}

function alignClass(align: 'left' | 'right' | 'center' | undefined): string {
  return align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : 'text-left'
}

function columnLabel<T>(col: Column<T, unknown>): string {
  const meta = col.columnDef.meta
  if (meta?.label) return meta.label
  const h = col.columnDef.header
  return typeof h === 'string' ? h : col.id
}

/** Dense, sortable trading table. Scrolls horizontally on narrow screens. */
export function DataTable<T>({
  data,
  columns,
  getRowId,
  testId,
  isLoading,
  error,
  onRetry,
  empty,
  initialSorting = [],
  initialHidden = [],
  columnToggle = false,
  toolbar,
  renderExpanded,
  canExpand,
  maxHeight,
  rowClassName,
  className,
  skeletonRows = 6,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting)
  const [expanded, setExpanded] = useState<ExpandedState>({})
  const [visibility, setVisibility] = useState<VisibilityState>(() =>
    Object.fromEntries(initialHidden.map((id) => [id, false])),
  )

  const table = useReactTable({
    data: data ?? [],
    columns,
    state: { sorting, expanded, columnVisibility: visibility },
    onSortingChange: setSorting,
    onExpandedChange: setExpanded,
    onColumnVisibilityChange: setVisibility,
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
    getRowCanExpand: renderExpanded ? (row) => (canExpand ? canExpand(row.original) : true) : undefined,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    autoResetExpanded: false,
  })

  const visibleCount = table.getVisibleLeafColumns().length
  const rows = table.getRowModel().rows
  const showSkeleton = isLoading && !data
  const showError = !showSkeleton && error != null && !data
  const hideable = table.getAllLeafColumns().filter((c) => c.getCanHide())

  return (
    <div className={cn('flex min-w-0 flex-col', className)} data-testid={testId}>
      {(toolbar || columnToggle) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-1.5">
          <div className="flex min-w-0 flex-wrap items-center gap-2">{toolbar}</div>
          {columnToggle && hideable.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="xs" className="text-muted-foreground">
                  <Columns3 /> Columns
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
                <DropdownMenuLabel className="label-caps">Visible columns</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {hideable.map((col) => (
                  <DropdownMenuCheckboxItem
                    key={col.id}
                    checked={col.getIsVisible()}
                    onCheckedChange={(v) => col.toggleVisibility(Boolean(v))}
                    onSelect={(e) => e.preventDefault()}
                    className="text-[12px]"
                  >
                    {columnLabel(col)}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}
      <div className="relative min-w-0 overflow-auto" style={maxHeight ? { maxHeight } : undefined}>
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="sticky top-0 z-10 bg-card">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b">
                {hg.headers.map((header) => {
                  const meta = header.column.columnDef.meta
                  const sortable = header.column.getCanSort()
                  const sorted = header.column.getIsSorted()
                  return (
                    <th
                      key={header.id}
                      scope="col"
                      aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
                      className={cn(
                        'h-8 px-2.5 align-middle font-medium whitespace-nowrap first:pl-3 last:pr-3',
                        alignClass(meta?.align),
                        meta?.className,
                      )}
                    >
                      {header.isPlaceholder ? null : sortable ? (
                        <button
                          type="button"
                          onClick={header.column.getToggleSortingHandler()}
                          className={cn(
                            'label-caps inline-flex items-center gap-1 hover:text-foreground focus-visible:text-foreground focus-visible:outline-none',
                            meta?.align === 'right' && 'flex-row-reverse',
                            sorted && 'text-foreground',
                          )}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {sorted === 'asc' ? (
                            <ArrowUp className="size-3" />
                          ) : sorted === 'desc' ? (
                            <ArrowDown className="size-3" />
                          ) : (
                            <span className="size-3" />
                          )}
                        </button>
                      ) : (
                        <span className="label-caps">
                          {flexRender(header.column.columnDef.header, header.getContext())}
                        </span>
                      )}
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {showSkeleton &&
              Array.from({ length: skeletonRows }, (_, i) => (
                <tr key={`sk-${i}`} className="border-b border-border/60">
                  {table.getVisibleLeafColumns().map((col) => (
                    <td key={col.id} className="h-8 px-2.5 first:pl-3 last:pr-3">
                      <Skeleton className="h-3.5 w-full min-w-8" />
                    </td>
                  ))}
                </tr>
              ))}
            {showError && (
              <tr>
                <td colSpan={visibleCount}>
                  <ErrorState error={error} onRetry={onRetry} />
                </td>
              </tr>
            )}
            {!showSkeleton && !showError && rows.length === 0 && (
              <tr>
                <td colSpan={visibleCount}>{empty ?? <EmptyState title="Nothing here yet" />}</td>
              </tr>
            )}
            {rows.map((row) => (
              <Fragment key={row.id}>
                <tr
                  data-row-id={row.id}
                  data-state={row.getIsExpanded() ? 'expanded' : undefined}
                  className={cn(
                    'border-b border-border/60 transition-colors hover:bg-row-hover data-[state=expanded]:bg-row-hover',
                    rowClassName?.(row.original),
                  )}
                >
                  {row.getVisibleCells().map((cell) => {
                    const meta = cell.column.columnDef.meta
                    return (
                      <td
                        key={cell.id}
                        className={cn(
                          'h-8 px-2.5 py-1 align-middle whitespace-nowrap first:pl-3 last:pr-3',
                          alignClass(meta?.align),
                          meta?.className,
                        )}
                      >
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    )
                  })}
                </tr>
                {row.getIsExpanded() && renderExpanded && (
                  <tr className="border-b bg-background/60">
                    <td colSpan={visibleCount} className="p-0">
                      {renderExpanded(row.original)}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Leading column with a chevron toggling the row's expanded sub-view. */
export function expanderColumn<T>(): ColumnDef<T> {
  return {
    id: '_expand',
    header: () => <span className="sr-only">Expand</span>,
    enableSorting: false,
    enableHiding: false,
    meta: { className: 'w-7 pr-0' },
    cell: ({ row }) =>
      row.getCanExpand() ? (
        <button
          type="button"
          onClick={row.getToggleExpandedHandler()}
          aria-label={row.getIsExpanded() ? 'Collapse row' : 'Expand row'}
          aria-expanded={row.getIsExpanded()}
          className="rounded-sm p-0.5 text-muted-foreground hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
        >
          <ChevronRight className={cn('size-3.5 transition-transform', row.getIsExpanded() && 'rotate-90')} />
        </button>
      ) : null,
  }
}
