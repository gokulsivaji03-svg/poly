import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { AlertTriangle, Loader2, Plus, Trash2 } from 'lucide-react'
import { useMemo, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { toast } from 'sonner'
import { AddressChip } from '@/components/common/address'
import { CoverageBadge } from '@/components/common/badges'
import { TimeCell } from '@/components/common/cells'
import { decimalSort, numCol } from '@/components/common/column-helpers'
import { ConfirmDialog } from '@/components/common/confirm-dialog'
import { DataTable, expanderColumn } from '@/components/common/data-table'
import { EquityEstimate } from '@/components/common/equity-estimate'
import { Num } from '@/components/common/num'
import { PageHeader, Panel } from '@/components/common/panel'
import { EmptyState } from '@/components/common/states'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { sourceTradeColumnsCompact } from '@/features/feed-columns'
import { SizingOverrideDialog } from '@/features/sizing-override-dialog'
import { useDeleteWallet, usePatchWallet } from '@/features/wallet-mutations'
import { api, ApiError, type SourceTrade, type WalletSummary } from '@/lib/api'
import { addDecimal } from '@/lib/decimal'
import { fmtMs, fmtUsd } from '@/lib/format'
import { qk, useWallets, useWalletTrades } from '@/lib/queries'
import { errorMessage } from '@/lib/utils'

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

function AddWalletForm() {
  const qc = useQueryClient()
  const [address, setAddress] = useState('')
  const [nickname, setNickname] = useState('')
  const [touched, setTouched] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  const trimmed = address.trim()
  const invalid = trimmed.length > 0 && !ADDRESS_RE.test(trimmed)
  const showInvalid = invalid && touched

  const add = useMutation({
    mutationFn: () => api.addWallet({ address: trimmed, nickname: nickname.trim() || undefined, follow: true }),
    onSuccess: (wallet) => {
      qc.setQueryData<{ wallets: WalletSummary[] }>(qk.wallets, (old) =>
        old && !old.wallets.some((w) => String(w.id) === String(wallet.id)) ? { wallets: [...old.wallets, wallet] } : old,
      )
      void qc.invalidateQueries({ queryKey: qk.wallets })
      void qc.invalidateQueries({ queryKey: qk.dashboard })
      toast.success('Wallet added', { description: `Monitoring ${wallet.nickname ?? wallet.address}` })
      setAddress('')
      setNickname('')
      setTouched(false)
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) setServerError('This wallet is already being monitored.')
      else if (err instanceof ApiError && err.status === 422) setServerError(`Invalid address: ${err.message}`)
      else setServerError(errorMessage(err))
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    setServerError(null)
    if (!ADDRESS_RE.test(trimmed)) return
    add.mutate()
  }

  return (
    <Panel title="Add wallet">
      <form onSubmit={submit} noValidate className="flex flex-col gap-2 p-3 md:flex-row md:items-start">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <Label htmlFor="wallet-address" className="label-caps">
            Address
          </Label>
          <Input
            id="wallet-address"
            data-testid="wallet-address-input"
            placeholder="0x…"
            autoComplete="off"
            spellCheck={false}
            className="num h-8 text-[12.5px]"
            value={address}
            aria-invalid={showInvalid || undefined}
            aria-describedby="wallet-address-error"
            onChange={(e) => {
              setAddress(e.target.value)
              setServerError(null)
            }}
            onBlur={() => setTouched(true)}
          />
          <div id="wallet-address-error" className="min-h-4 text-[11px]">
            {showInvalid ? (
              <span className="text-neg">Must be 0x followed by 40 hex characters.</span>
            ) : serverError ? (
              <span className="text-neg" role="alert">
                {serverError}
              </span>
            ) : (
              <span className="text-muted-foreground">Public Polygon address of a Polymarket trader.</span>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1 md:w-56">
          <Label htmlFor="wallet-nickname" className="label-caps">
            Nickname
          </Label>
          <Input
            id="wallet-nickname"
            data-testid="wallet-nickname-input"
            placeholder="optional"
            className="h-8 text-[12.5px]"
            maxLength={64}
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
          />
        </div>
        <Button
          type="submit"
          size="sm"
          data-testid="wallet-add-submit"
          disabled={add.isPending || trimmed.length === 0}
          className="md:mt-[18px]"
        >
          {add.isPending ? <Loader2 className="animate-spin" /> : <Plus />}
          Add & follow
        </Button>
      </form>
    </Panel>
  )
}

const recentTradeColumns = sourceTradeColumnsCompact.filter((c) => c.id !== 'wallet')

function RecentTrades({ walletId }: { walletId: string }) {
  const q = useWalletTrades(walletId, 20)
  return (
    <div className="border-l-2 border-primary/40 py-1 pl-6">
      <div className="flex items-center justify-between px-3 py-1.5">
        <span className="label-caps">Recent trades</span>
        <Link to={`/wallets/${encodeURIComponent(walletId)}`} className="text-[11px] text-muted-foreground hover:text-foreground">
          Full history →
        </Link>
      </div>
      <DataTable<SourceTrade>
        data={q.data}
        columns={recentTradeColumns}
        getRowId={(t) => String(t.id)}
        isLoading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        skeletonRows={3}
        empty={<div className="px-3 py-3 text-[12px] text-muted-foreground">No trades detected for this wallet yet.</div>}
      />
    </div>
  )
}

function FollowSwitch({ wallet }: { wallet: WalletSummary }) {
  const patch = usePatchWallet()
  return (
    <Switch
      checked={wallet.follow}
      disabled={patch.isPending}
      onCheckedChange={(follow) => patch.mutate({ id: wallet.id, body: { follow } })}
      aria-label={`${wallet.follow ? 'Stop copying' : 'Copy'} ${wallet.nickname ?? wallet.address}`}
      data-testid="wallet-follow-toggle"
    />
  )
}

function RemoveWallet({ wallet }: { wallet: WalletSummary }) {
  const del = useDeleteWallet()
  return (
    <ConfirmDialog
      trigger={
        <Button variant="ghost" size="icon-xs" aria-label={`Remove ${wallet.nickname ?? wallet.address}`} className="text-muted-foreground hover:text-neg">
          <Trash2 />
        </Button>
      }
      title="Remove wallet?"
      description={
        <>
          Stop monitoring <span className="text-foreground">{wallet.nickname ?? wallet.address}</span>. Its trade
          history and any open copied positions are kept.
        </>
      }
      confirmLabel="Remove"
      destructive
      onConfirm={() => del.mutateAsync(wallet.id)}
    />
  )
}

function observedPnl(w: WalletSummary): string | null {
  return addDecimal(w.observed_realized_pnl, w.observed_unrealized_pnl)
}

const columns: ColumnDef<WalletSummary>[] = [
  expanderColumn<WalletSummary>(),
  {
    id: 'wallet',
    header: 'Wallet',
    accessorFn: (w) => w.nickname ?? w.address,
    enableHiding: false,
    meta: { label: 'Wallet' },
    cell: ({ row: { original: w } }) => (
      <div className="flex flex-col leading-tight">
        <Link
          to={`/wallets/${encodeURIComponent(String(w.id))}`}
          className="font-medium hover:text-primary focus-visible:text-primary focus-visible:outline-none"
        >
          {w.nickname ?? 'Unnamed'}
        </Link>
        <AddressChip address={w.address} />
      </div>
    ),
  },
  {
    id: 'estimated_equity',
    header: 'Est. equity',
    accessorFn: (w) => w.estimated_equity ?? undefined,
    sortingFn: decimalSort,
    sortUndefined: 'last',
    meta: { align: 'right', label: 'Estimated equity' },
    cell: ({ row: { original: w } }) => (
      <div className="flex flex-col items-end gap-0.5">
        <EquityEstimate
          equity={w.estimated_equity}
          coverage={w.equity_coverage}
          lowerBound={w.equity_is_lower_bound}
          confidence={null}
        />
        <CoverageBadge coverage={w.equity_coverage} confidence={w.equity_confidence} />
      </div>
    ),
  },
  {
    id: 'last_trade_at',
    header: 'Last activity',
    accessorFn: (w) => w.last_trade_at ?? undefined,
    sortUndefined: 'last',
    meta: { label: 'Last activity' },
    cell: ({ row: { original: w } }) => (
      <div className="flex flex-col leading-tight">
        <TimeCell iso={w.last_trade_at} relative />
        <span className="flex items-center gap-1 text-[10.5px] text-faint">
          {w.last_error ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span tabIndex={0} className="inline-flex items-center gap-1 text-neg outline-none">
                  <AlertTriangle className="size-3" /> observation error
                </span>
              </TooltipTrigger>
              <TooltipContent>{w.last_error}</TooltipContent>
            </Tooltip>
          ) : w.freshness_s != null ? (
            <span className="num">observed {w.freshness_s}s ago</span>
          ) : (
            <span>never observed</span>
          )}
          {w.ingestion_delay_ms != null && <span className="num">· lag {fmtMs(w.ingestion_delay_ms)}</span>}
        </span>
      </div>
    ),
  },
  {
    id: 'observed_pnl',
    header: 'Observed P&L',
    accessorFn: (w) => observedPnl(w) ?? undefined,
    sortingFn: decimalSort,
    sortUndefined: 'last',
    meta: { align: 'right', label: 'Observed P&L' },
    cell: ({ row: { original: w } }) => {
      const total = observedPnl(w)
      return (
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0} className="cursor-help outline-none">
              <Num value={total} kind="usd" signed pnl unavailableReason="realized or unrealized P&L unknown" />
            </span>
          </TooltipTrigger>
          <TooltipContent>
            <div className="num text-[11.5px]">
              realized {fmtUsd(w.observed_realized_pnl, { signed: true }) ?? '—'}
              <br />
              unrealized {fmtUsd(w.observed_unrealized_pnl, { signed: true }) ?? '—'}
            </div>
          </TooltipContent>
        </Tooltip>
      )
    },
  },
  numCol<WalletSummary>('open_positions', 'Positions', (w) => w.open_positions, 'int', { label: 'Open positions' }),
  numCol<WalletSummary>('trades_7d', 'Trades 7d', (w) => w.trades_7d, 'int'),
  {
    id: 'follow',
    header: 'Copy',
    accessorFn: (w) => (w.follow ? 1 : 0),
    meta: { align: 'center', label: 'Copy status' },
    cell: ({ row: { original: w } }) => <FollowSwitch wallet={w} />,
  },
  {
    id: 'sizing',
    header: 'Sizing',
    enableSorting: false,
    meta: { label: 'Sizing override' },
    cell: ({ row: { original: w } }) => <SizingOverrideDialog wallet={w} />,
  },
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    enableHiding: false,
    meta: { align: 'right' },
    cell: ({ row: { original: w } }) => <RemoveWallet wallet={w} />,
  },
]

export function WalletsPage() {
  const q = useWallets()
  const followed = useMemo(() => q.data?.filter((w) => w.follow).length ?? 0, [q.data])
  return (
    <>
      <PageHeader
        title="Wallets"
        description={q.data ? `${q.data.length} monitored · ${followed} copied` : 'Monitored source wallets'}
      />
      <AddWalletForm />
      <Panel title="Monitored wallets">
        <DataTable
          testId="wallets-table"
          data={q.data}
          columns={columns}
          getRowId={(w) => String(w.id)}
          isLoading={q.isPending}
          error={q.error}
          onRetry={() => void q.refetch()}
          columnToggle
          renderExpanded={(w) => <RecentTrades walletId={String(w.id)} />}
          empty={
            <EmptyState
              title="No wallets yet"
              description="Add a wallet to start monitoring. New trades are detected within seconds and copied according to your sizing settings."
            />
          }
        />
      </Panel>
    </>
  )
}
