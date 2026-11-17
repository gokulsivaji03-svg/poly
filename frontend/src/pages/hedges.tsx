import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo } from 'react'
import { toast } from 'sonner'
import { HedgeStateBadge, Tag } from '@/components/common/badges'
import { MonoId, TimeCell } from '@/components/common/cells'
import { timeCol } from '@/components/common/column-helpers'
import { DataTable } from '@/components/common/data-table'
import { Num } from '@/components/common/num'
import { KV, PageHeader, Panel } from '@/components/common/panel'
import { EmptyState } from '@/components/common/states'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { HEDGE_HIDDEN_BY_DEFAULT, hedgeColumns, sortOpportunities } from '@/features/hedge-columns'
import { HEDGE_THRESHOLD_FIELDS } from '@/features/settings/schema'
import { ConflictBanner, FieldList, SaveBar } from '@/features/settings/setting-field'
import { useSettingsForm } from '@/features/settings/use-settings-form'
import { api, type HedgeEvent, type HedgeMode, type HedgeSettings } from '@/lib/api'
import { addDecimal } from '@/lib/decimal'
import { fmtMs, humanizeCode } from '@/lib/format'
import { useHedgeEvents, useHedges, useSettings } from '@/lib/queries'
import { applySettingsEnvelope } from '@/lib/realtime-cache'
import { errorMessage } from '@/lib/utils'

const MODE_LABEL: Record<HedgeMode, string> = {
  ALERT_ONLY: 'Alert only',
  AUTO_PAPER: 'Automatic paper hedging',
}

function detailText(detail: unknown): string {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}

const eventColumns: ColumnDef<HedgeEvent>[] = [
  timeCol<HedgeEvent>('created_at', 'Time', (e) => e.created_at),
  {
    id: 'kind',
    header: 'Event',
    accessorFn: (e) => e.kind,
    cell: ({ row: { original: e } }) => <span className="num text-[11.5px]">{e.kind}</span>,
  },
  {
    id: 'transition',
    header: 'Transition',
    accessorFn: (e) => `${e.from_state ?? ''}>${e.to_state ?? ''}`,
    cell: ({ row: { original: e } }) =>
      e.to_state ? (
        <span className="inline-flex items-center gap-1">
          {e.from_state ? <HedgeStateBadge state={e.from_state} /> : <span className="text-faint">∅</span>}
          <span className="text-faint">→</span>
          <HedgeStateBadge state={e.to_state} />
        </span>
      ) : (
        <span className="text-faint">—</span>
      ),
  },
  {
    id: 'condition_id',
    header: 'Condition',
    accessorFn: (e) => e.condition_id,
    enableSorting: false,
    cell: ({ row: { original: e } }) => <MonoId value={e.condition_id} />,
  },
  {
    id: 'detail',
    header: 'Detail',
    enableSorting: false,
    cell: ({ row: { original: e } }) => {
      const text = detailText(e.detail)
      return text ? (
        <span className="block max-w-[420px] truncate text-[12px] text-muted-foreground" title={text}>
          {text}
        </span>
      ) : (
        <span className="text-faint">—</span>
      )
    },
  },
]

/** Immediate (non-form) hedge controls: enable + mode. */
function useHedgeToggle(onApplied: (version: number) => void) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (patch: Partial<HedgeSettings>) => api.putSettings({ hedge: patch }),
    onSuccess: (env, patch) => {
      applySettingsEnvelope(qc, env)
      onApplied(env.version)
      void qc.invalidateQueries({ queryKey: ['hedges'] })
      if (patch.mode) toast.success(`Hedge mode: ${MODE_LABEL[patch.mode]}`)
      else if (patch.enabled !== undefined) toast.success(patch.enabled ? 'Hedge scanner enabled' : 'Hedge scanner disabled')
    },
    onError: (err) => toast.error('Could not update hedge settings', { description: errorMessage(err) }),
  })
}

function ScannerPanel() {
  const hedges = useHedges()
  const scanner = hedges.data?.scanner
  const opps = hedges.data?.opportunities
  const viable = opps?.filter((o) => o.viable) ?? []
  const lockable = viable.reduce<string | null>((acc, o) => (o.net_profit === null ? acc : (addDecimal(acc ?? '0', o.net_profit) ?? acc)), null)

  return (
    <Panel title="Scanner status">
      {!scanner ? (
        <div className="flex flex-col gap-2 p-3">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-4" />
          ))}
        </div>
      ) : (
        <div className="px-3 py-2">
          <KV label="Scanner">
            <Tag tone={scanner.enabled ? 'pos' : 'muted'}>{scanner.enabled ? 'enabled' : 'disabled'}</Tag>
          </KV>
          <KV label="Mode">
            <Tag tone={scanner.mode === 'AUTO_PAPER' ? 'accent' : 'info'}>{MODE_LABEL[scanner.mode] ?? scanner.mode}</Tag>
          </KV>
          <KV label="Last scan">
            <TimeCell iso={scanner.last_scan_at} relative />
          </KV>
          <KV label="Scan duration">
            <span className="num">{fmtMs(scanner.scan_duration_ms) ?? '—'}</span>
          </KV>
          <KV label="Positions scanned">
            <Num value={scanner.positions_scanned} kind="int" />
          </KV>
          <KV label="Viable now">
            <span className="num">
              {viable.length}
              <span className="text-faint"> / {opps?.length ?? 0}</span>
            </span>
          </KV>
          <KV label="Lockable profit (viable)">
            <Num value={lockable} kind="usd" signed pnl unavailableReason="no viable opportunities" />
          </KV>
        </div>
      )}
    </Panel>
  )
}

export function HedgesPage() {
  const hedges = useHedges()
  const events = useHedgeEvents()
  const settings = useSettings()
  const form = useSettingsForm(HEDGE_THRESHOLD_FIELDS, { successMessage: 'Hedge thresholds saved' })
  const toggle = useHedgeToggle(form.rebaseAfterOwnChange)
  const hs = settings.data?.settings.hedge
  const pending = toggle.isPending ? toggle.variables : undefined
  const enabled = pending?.enabled ?? hs?.enabled ?? false
  const mode = pending?.mode ?? hs?.mode ?? 'ALERT_ONLY'

  const data = useMemo(() => (hedges.data ? sortOpportunities(hedges.data.opportunities) : undefined), [hedges.data])

  return (
    <>
      <PageHeader
        title="Hedges"
        description="Complementary-outcome hedges: buy the opposite outcome when combined cost per share leaves a locked profit after fees."
      />
      <div className="grid gap-3 xl:grid-cols-[320px_1fr]">
        <div className="flex flex-col gap-3">
          <ScannerPanel />
          <Panel title="Activation">
            <div className="flex flex-col gap-3 p-3">
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="hedge-enabled" className="text-[12.5px] font-normal">
                  Hedge scanner enabled
                </Label>
                <Switch
                  id="hedge-enabled"
                  data-testid="hedge-enabled-toggle"
                  checked={enabled}
                  disabled={!hs || toggle.isPending}
                  onCheckedChange={(v) => toggle.mutate({ enabled: v })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="hedge-mode" className="text-[12.5px] font-normal">
                  Mode
                </Label>
                <NativeSelect
                  id="hedge-mode"
                  data-testid="hedge-mode-select"
                  size="sm"
                  className="w-full text-[12.5px]"
                  value={mode}
                  disabled={!hs || toggle.isPending}
                  onChange={(e) => toggle.mutate({ mode: e.target.value as HedgeMode })}
                >
                  <NativeSelectOption value="ALERT_ONLY">{MODE_LABEL.ALERT_ONLY}</NativeSelectOption>
                  <NativeSelectOption value="AUTO_PAPER">{MODE_LABEL.AUTO_PAPER}</NativeSelectOption>
                </NativeSelect>
                <p className="text-[11px] text-muted-foreground">
                  {mode === 'AUTO_PAPER'
                    ? 'Viable hedges are executed automatically as paper orders.'
                    : 'Viable hedges are surfaced here; execute them manually.'}
                </p>
              </div>
            </div>
          </Panel>
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <ConflictBanner form={form} />
          <Panel title="Thresholds">
            <div className="grid xl:grid-cols-2 xl:[&>*:first-child]:border-r">
              <FieldList fields={HEDGE_THRESHOLD_FIELDS.slice(0, 4)} form={form} />
              <FieldList fields={HEDGE_THRESHOLD_FIELDS.slice(4)} form={form} />
            </div>
            <div className="border-t px-3 py-2">
              <SaveBar form={form} testId="hedge-settings-save" />
            </div>
          </Panel>
        </div>
      </div>
      <Panel title="Opportunities">
        <DataTable
          testId="hedges-table"
          data={data}
          columns={hedgeColumns}
          getRowId={(o) => o.id}
          isLoading={hedges.isPending}
          error={hedges.error}
          onRetry={() => void hedges.refetch()}
          columnToggle
          initialHidden={HEDGE_HIDDEN_BY_DEFAULT}
          rowClassName={(o) => (o.viable ? undefined : 'text-foreground/70')}
          empty={
            <EmptyState
              title="No open positions to hedge"
              description={
                hs && !hs.enabled
                  ? 'The hedge scanner is disabled. Enable it above to evaluate open positions.'
                  : 'Each open primary position appears here with its best complementary hedge, or the reasons it is not viable.'
              }
            />
          }
        />
      </Panel>
      <Panel title="Hedge event log">
        <DataTable
          data={events.data}
          columns={eventColumns}
          getRowId={(e) => String(e.id)}
          isLoading={events.isPending}
          error={events.error}
          onRetry={() => void events.refetch()}
          initialSorting={[{ id: 'created_at', desc: true }]}
          maxHeight={420}
          empty={<EmptyState title="No hedge events yet" description={`State transitions such as ${humanizeCode('HEDGE_OPPORTUNITY').toLowerCase()} → fully hedged are logged here.`} />}
        />
      </Panel>
    </>
  )
}
