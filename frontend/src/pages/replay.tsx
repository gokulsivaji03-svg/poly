import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Info, Loader2, Play, X } from 'lucide-react'
import { useMemo, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { OutcomeBadge, SideBadge, SignalStatusBadge, Tag, type Tone } from '@/components/common/badges'
import { MarketCell, ReasonsCell } from '@/components/common/cells'
import { numCol, timeCol } from '@/components/common/column-helpers'
import { DataTable } from '@/components/common/data-table'
import { DecimalInput } from '@/components/common/decimal-input'
import { Num } from '@/components/common/num'
import { Panel, Stat } from '@/components/common/panel'
import { EmptyState, ErrorState, SkeletonLines } from '@/components/common/states'
import { TimeSeriesChart, type ChartSeries } from '@/components/common/time-series-chart'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ApiError, api, type ReplayRun, type ReplaySignal, type ReplayStatus } from '@/lib/api'
import { CHART_COLORS, toChartPoints } from '@/lib/chart-data'
import { normalizeDecimal, signDecimal } from '@/lib/decimal'
import { fmtDateTime, fmtMs, truncateAddress } from '@/lib/format'
import { qk, useReplayRun, useReplayRuns, useSettings, useWallets } from '@/lib/queries'
import { cn, errorMessage } from '@/lib/utils'

const LOOKAHEAD_NOTE =
  'Uses only order-book snapshots recorded at or before each replayed moment; market resolutions after the replay time are ignored.'

const STATUS_TONE: Record<ReplayStatus, Tone> = { RUNNING: 'replay', COMPLETED: 'pos', FAILED: 'neg' }

function ReplayStatusBadge({ status }: { status: ReplayStatus }) {
  return (
    <Tag tone={STATUS_TONE[status] ?? 'muted'} className="gap-1">
      {status === 'RUNNING' && <Loader2 className="size-3 animate-spin" />}
      {status}
    </Tag>
  )
}

function ReplayBadge() {
  return (
    <Tag tone="replay" title="Historical simulation — not live paper trading" className="h-5 px-2 text-[11px]">
      <span data-testid="replay-badge">REPLAY</span>
    </Tag>
  )
}

/** `<input type="datetime-local">` value for a Date, in local time. */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function durationMs(run: ReplayRun): number | null {
  if (!run.finished_at) return null
  const ms = Date.parse(run.finished_at) - Date.parse(run.created_at)
  return Number.isFinite(ms) ? ms : null
}

// ---------------------------------------------------------------------------------------------
// Form

function ReplayForm({ onStarted, disabled }: { onStarted: (id: string) => void; disabled: boolean }) {
  const qc = useQueryClient()
  const wallets = useWallets()
  const settings = useSettings()
  const [start, setStart] = useState(() => toLocalInput(new Date(Date.now() - 7 * 864e5)))
  const [end, setEnd] = useState(() => toLocalInput(new Date()))
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bankroll, setBankroll] = useState<string | null>(null)
  const [delay, setDelay] = useState('2')
  const [error, setError] = useState<string | null>(null)

  const bankrollValue = bankroll ?? normalizeDecimal(settings.data?.settings.paper.starting_bankroll_usd) ?? ''
  const startMs = Date.parse(start)
  const endMs = Date.parse(end)
  const delaySeconds = Number(delay)

  const problems: string[] = []
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) problems.push('Choose a start and end time.')
  else if (startMs >= endMs) problems.push('Start must be before end.')
  else if (endMs - startMs > 31 * 864e5) problems.push('Replay windows are limited to 31 days.')
  if (selected.size === 0) problems.push('Select at least one wallet.')
  if (selected.size > 50) problems.push('Select no more than 50 wallets.')
  if (signDecimal(bankrollValue) !== 1) problems.push('Bankroll must be a positive decimal.')
  if (delay.trim() !== '' && (!Number.isInteger(delaySeconds) || delaySeconds < 0 || delaySeconds > 3600)) {
    problems.push('Delay must be a whole number from 0 to 3600 seconds.')
  }

  const startRun = useMutation({
    mutationFn: () =>
      api.startReplay({
        start: new Date(startMs).toISOString(),
        end: new Date(endMs).toISOString(),
        wallet_ids: [...selected].map(Number),
        starting_bankroll: normalizeDecimal(bankrollValue)!,
        ...(delay.trim() !== '' ? { detection_delay_s: delaySeconds } : {}),
      }),
    onSuccess: (res) => {
      setError(null)
      void qc.invalidateQueries({ queryKey: qk.replayRuns })
      onStarted(String(res.id))
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) setError('A replay is already running. Wait for it to finish.')
      else setError(errorMessage(err))
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (startRun.isPending || disabled) return
    if (problems.length > 0) {
      setError(problems[0]!)
      return
    }
    startRun.mutate()
  }

  const toggle = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })

  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone

  return (
    <Panel title="New replay" className="border-replay/30">
      <form onSubmit={submit} noValidate className="flex flex-col gap-3 p-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="replay-start" className="label-caps">
              Start
            </Label>
            <Input
              id="replay-start"
              data-testid="replay-start"
              type="datetime-local"
              className="num h-8 text-[12.5px]"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="replay-end" className="label-caps">
              End
            </Label>
            <Input
              id="replay-end"
              data-testid="replay-end"
              type="datetime-local"
              className="num h-8 text-[12.5px]"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>
        <div className="text-[11px] text-muted-foreground">Times are in your local zone ({tz}); sent as UTC.</div>

        <fieldset className="flex flex-col gap-1">
          <legend className="label-caps mb-1 flex w-full items-center justify-between">
            <span>Wallets</span>
            {wallets.data && wallets.data.length > 0 && (
              <button
                type="button"
                className="text-[10.5px] tracking-normal text-muted-foreground normal-case hover:text-foreground"
                onClick={() =>
                  setSelected(
                    selected.size === wallets.data.length ? new Set() : new Set(wallets.data.map((w) => String(w.id))),
                  )
                }
              >
                {selected.size === wallets.data.length ? 'Clear' : 'Select all'}
              </button>
            )}
          </legend>
          {wallets.isPending ? (
            <SkeletonLines rows={2} className="p-0" />
          ) : !wallets.data ? (
            <ErrorState error={wallets.error} onRetry={() => void wallets.refetch()} />
          ) : wallets.data.length === 0 ? (
            <div className="rounded-sm border px-3 py-3 text-[12px] text-muted-foreground">
              No wallets. <Link to="/wallets" className="text-primary hover:underline">Add a wallet</Link> to replay its history.
            </div>
          ) : (
            <ul className="max-h-48 overflow-y-auto rounded-sm border" data-testid="replay-wallets">
              {wallets.data.map((w) => {
                const id = String(w.id)
                const inputId = `replay-wallet-${id}`
                return (
                  <li key={id} className="flex items-center gap-2.5 border-b border-border/60 px-2.5 py-1.5 last:border-b-0">
                    <Checkbox id={inputId} checked={selected.has(id)} onCheckedChange={(v) => toggle(id, v === true)} />
                    <Label htmlFor={inputId} className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 font-normal">
                      <span className="truncate text-[12.5px]">{w.nickname ?? 'Unnamed'}</span>
                      <span className="num text-[11px] text-muted-foreground">{truncateAddress(w.address)}</span>
                    </Label>
                  </li>
                )
              })}
            </ul>
          )}
        </fieldset>

        {settings.isError && bankroll === null && (
          <ErrorState error={settings.error} onRetry={() => void settings.refetch()} title="Failed to load default bankroll" />
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="replay-bankroll" className="label-caps">
              Starting bankroll
            </Label>
            <DecimalInput
              id="replay-bankroll"
              data-testid="replay-bankroll"
              prefix="$"
              value={bankrollValue}
              onChange={(e) => setBankroll(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="replay-delay" className="label-caps">
              Detection delay
            </Label>
            <DecimalInput id="replay-delay" data-testid="replay-delay" suffix="s" value={delay} onChange={(e) => setDelay(e.target.value)} />
          </div>
        </div>
        <p className="flex items-start gap-1.5 text-[11px] text-muted-foreground">
          <Info className="mt-0.5 size-3 shrink-0" />
          Uses current strategy settings, with strategies unpaused, emergency stop cleared and execution delay zeroed in the
          isolated replay account. No settings overrides are sent. {LOOKAHEAD_NOTE}
        </p>
        {error && (
          <p role="alert" className="rounded-sm border border-neg/30 bg-neg/10 px-2.5 py-1.5 text-[12px] text-neg">
            {error}
          </p>
        )}
        <Button
          type="submit"
          size="sm"
          data-testid="replay-submit"
          disabled={startRun.isPending || disabled}
          className="bg-replay text-[#130f1f] hover:bg-replay/90"
          title={disabled ? 'A replay is already running' : undefined}
        >
          {startRun.isPending ? <Loader2 className="animate-spin" /> : <Play />}
          {disabled ? 'Replay running…' : 'Start replay'}
        </Button>
      </form>
    </Panel>
  )
}

// ---------------------------------------------------------------------------------------------
// Runs table

function runColumns(selectedId: string | null): ColumnDef<ReplayRun>[] {
  return [
    {
      id: 'id',
      header: 'Run',
      accessorFn: (r) => String(r.id),
      cell: ({ row: { original: r } }) => (
        <Link
          to={`?run=${encodeURIComponent(String(r.id))}`}
          className={cn('num text-[12px] hover:text-replay', selectedId === String(r.id) && 'text-replay')}
        >
          #{String(r.id)}
        </Link>
      ),
    },
    {
      id: 'status',
      header: 'Status',
      accessorFn: (r) => r.status,
      cell: ({ row: { original: r } }) => <ReplayStatusBadge status={r.status} />,
    },
    timeCol<ReplayRun>('created_at', 'Started', (r) => r.created_at),
    {
      id: 'range',
      header: 'Replayed period',
      accessorFn: (r) => r.params.start,
      cell: ({ row: { original: r } }) => (
        <span className="num text-[11.5px] text-muted-foreground">
          {fmtDateTime(r.params.start) ?? '—'} → {fmtDateTime(r.params.end) ?? '—'}
        </span>
      ),
    },
    numCol<ReplayRun>('wallets', 'Wallets', (r) => r.params.wallet_ids.length, 'int'),
    numCol<ReplayRun>('starting_bankroll', 'Bankroll', (r) => r.params.starting_bankroll, 'usd', { dp: 0 }),
    numCol<ReplayRun>('final_equity', 'Final equity', (r) => r.summary?.final_equity, 'usd'),
    numCol<ReplayRun>('return_pct', 'Return', (r) => r.summary?.return_pct, 'pct', { signed: true, pnl: true }),
    numCol<ReplayRun>('executed', 'Executed', (r) => r.summary?.signals.EXECUTED, 'int'),
    numCol<ReplayRun>('skipped', 'Skipped', (r) => r.summary?.signals.SKIPPED, 'int'),
    numCol<ReplayRun>('events_replayed', 'Events', (r) => r.summary?.events_replayed, 'int'),
    numCol<ReplayRun>('duration', 'Took', (r) => durationMs(r), 'ms'),
    {
      id: 'error',
      header: 'Error',
      accessorFn: (r) => r.error ?? undefined,
      enableSorting: false,
      cell: ({ row: { original: r } }) =>
        r.error ? (
          <span className="block max-w-[240px] truncate text-[12px] text-neg" title={r.error}>
            {r.error}
          </span>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
  ]
}

// ---------------------------------------------------------------------------------------------
// Detail

const signalColumns: ColumnDef<ReplaySignal>[] = [
  timeCol<ReplaySignal>('t', 'Replay time', (s) => s.t),
  {
    id: 'wallet',
    header: 'Wallet',
    accessorFn: (s) => s.wallet ?? undefined,
    cell: ({ row: { original: s } }) =>
      s.wallet ? (
        <span className={cn('text-[12px]', s.wallet.startsWith('0x') && 'num')}>
          {s.wallet.startsWith('0x') ? truncateAddress(s.wallet) : s.wallet}
        </span>
      ) : (
        <span className="text-faint">—</span>
      ),
  },
  {
    id: 'status',
    header: 'Status',
    accessorFn: (s) => s.status,
    cell: ({ row: { original: s } }) => <SignalStatusBadge status={s.status} />,
  },
  {
    id: 'market',
    header: 'Market',
    accessorFn: (s) => s.title ?? undefined,
    cell: ({ row: { original: s } }) => (
      <span className="flex items-center gap-1.5">
        <SideBadge side={s.side} />
        <OutcomeBadge outcome={s.outcome} />
        <MarketCell title={s.title} className="max-w-[280px]" />
      </span>
    ),
  },
  numCol<ReplaySignal>('source_price', 'Src price', (s) => s.source_price, 'cents'),
  numCol<ReplaySignal>('avg_fill_price', 'Avg fill', (s) => s.avg_fill_price, 'cents'),
  numCol<ReplaySignal>('filled_cost', 'Fill cost', (s) => s.filled_cost, 'usd'),
  {
    id: 'reasons',
    header: 'Reasons',
    accessorFn: (s) => s.reasons.join(','),
    enableSorting: false,
    cell: ({ row: { original: s } }) => <ReasonsCell reasons={s.reasons} />,
  },
]

function RunDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useReplayRun(id)
  const run = q.data
  const series = useMemo<ChartSeries[]>(
    () => [
      {
        id: 'equity',
        type: 'area',
        title: 'Replay equity',
        color: CHART_COLORS.replay,
        data: toChartPoints(run?.equity_curve ?? [], (p) => p.t, (p) => p.equity),
      },
    ],
    [run?.equity_curve],
  )
  const s = run?.summary

  return (
    <Panel
      testId="replay-detail"
      className="border-replay/40"
      title={
        <span className="flex items-center gap-2">
          <ReplayBadge /> Run #{id}
        </span>
      }
      actions={
        <Button size="icon-xs" variant="ghost" onClick={onClose} aria-label="Close run">
          <X />
        </Button>
      }
    >
      {q.isPending ? (
        <SkeletonLines rows={8} />
      ) : !run ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} title="Failed to load replay run" />
      ) : (
        <div className="flex flex-col">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b px-3 py-2 text-[12px]">
            <ReplayStatusBadge status={run.status} />
            <span className="num text-muted-foreground">
              {fmtDateTime(run.params.start)} → {fmtDateTime(run.params.end)}
            </span>
            <span className="text-muted-foreground">
              {run.params.wallet_ids.length} wallet{run.params.wallet_ids.length === 1 ? '' : 's'}
            </span>
            {run.params.detection_delay_s != null && (
              <span className="num text-muted-foreground">delay {run.params.detection_delay_s}s</span>
            )}
            {durationMs(run) !== null && <span className="num text-muted-foreground">took {fmtMs(durationMs(run))}</span>}
          </div>
          {run.error && (
            <p role="alert" className="mx-3 mt-2 rounded-sm border border-neg/30 bg-neg/10 px-2.5 py-1.5 text-[12px] text-neg">
              {run.error}
            </p>
          )}
          {s?.ledger_ok === false && (
            <p role="alert" className="mx-3 mt-2 rounded-sm border border-neg/30 bg-neg/10 px-2.5 py-1.5 text-[12px] text-neg">
              Replay ledger verification failed. Do not rely on these financial results.
            </p>
          )}
          <section
            aria-label="Replay summary"
            className="grid grid-cols-2 gap-px border-b bg-border sm:grid-cols-4 xl:grid-cols-8 [&>*]:bg-card"
          >
            <Stat label="Starting bankroll" value={<Num value={s?.starting_bankroll ?? run.params.starting_bankroll} kind="usd" />} />
            <Stat label="Final equity" value={<Num value={s?.final_equity} kind="usd" unavailableReason={run.status === 'RUNNING' ? 'still running' : undefined} />} />
            <Stat label="Return" value={<Num value={s?.return_pct} kind="pct" signed pnl />} />
            <Stat label="Realized P&L" value={<Num value={s?.realized_pnl} kind="usd" signed pnl />} />
            <Stat label="Unrealized P&L" value={<Num value={s?.unrealized_pnl} kind="usd" signed pnl />} />
            <Stat label="Locked profit" value={<Num value={s?.locked_profit} kind="usd" signed pnl />} />
            <Stat label="Events replayed" value={<Num value={s?.events_replayed} kind="int" />} />
            <Stat label="Book snapshots" value={<Num value={s?.snapshots_used} kind="int" />} />
          </section>
          {s && (
            <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
              <span className="label-caps">Signals</span>
              {Object.entries(s.signals).map(([status, n]) => (
                <span key={status} className="inline-flex items-center gap-1">
                  <SignalStatusBadge status={status} />
                  <span className="num text-[12px]">{n}</span>
                </span>
              ))}
            </div>
          )}
          <div className="h-[260px] border-b p-2" data-testid="replay-equity-chart">
            {series[0]!.data.length > 0 ? (
              <TimeSeriesChart series={series} height={244} />
            ) : (
              <EmptyState
                title={run.status === 'RUNNING' ? 'Replay in progress…' : 'No equity points'}
                description={run.status === 'RUNNING' ? 'Results appear when the replay finishes.' : undefined}
              />
            )}
          </div>
          <p className="flex items-start gap-1.5 border-b px-3 py-2 text-[11.5px] text-muted-foreground">
            <Info className="mt-0.5 size-3 shrink-0 text-replay" />
            {LOOKAHEAD_NOTE}
          </p>
          <DataTable
            testId="replay-signals-table"
            data={run.signals}
            columns={signalColumns}
            initialSorting={[{ id: 't', desc: false }]}
            maxHeight={480}
            empty={<EmptyState title="No signals" description="No source trades fell inside the replayed period." />}
          />
        </div>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------------------------

export function ReplayPage() {
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('run')
  const runs = useReplayRuns()
  const running = runs.data?.some((r) => r.status === 'RUNNING') ?? false
  const columns = useMemo(() => runColumns(selectedId), [selectedId])

  return (
    <>
      <div
        role="note"
        className="flex flex-wrap items-center gap-3 rounded-sm border border-replay/40 bg-replay/[0.07] px-3 py-2.5"
      >
        <ReplayBadge />
        <div className="min-w-0 flex-1">
          <h1 className="text-[15px] font-semibold tracking-tight">Historical replay</h1>
          <p className="text-[12px] text-muted-foreground">
            Simulates copy trading over a past period using recorded data. Results are reported separately from live paper
            trading.
          </p>
        </div>
      </div>
      <div className="grid gap-3 xl:grid-cols-[380px_1fr]">
        <ReplayForm onStarted={(id) => setParams({ run: id })} disabled={running} />
        <Panel title="Runs" actions={running && <span className="text-[11px] text-replay">refreshing every 2s</span>}>
          <DataTable
            testId="replay-runs-table"
            data={runs.data}
            columns={columns}
            getRowId={(r) => String(r.id)}
            isLoading={runs.isPending}
            error={runs.error}
            onRetry={() => void runs.refetch()}
            initialSorting={[{ id: 'created_at', desc: true }]}
            rowClassName={(r) => (String(r.id) === selectedId ? 'bg-replay/[0.06]' : undefined)}
            maxHeight={520}
            empty={
              <EmptyState
                title="No replays yet"
                description="Pick a period and wallets, then start a replay to see how copying them would have performed."
              />
            }
          />
        </Panel>
      </div>
      {selectedId && <RunDetail key={selectedId} id={selectedId} onClose={() => setParams({})} />}
    </>
  )
}
