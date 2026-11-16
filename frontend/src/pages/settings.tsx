import { useQueryClient } from '@tanstack/react-query'
import { ArrowRight, OctagonX, Pause, Play, RotateCcw, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { toast } from 'sonner'
import { AddressChip } from '@/components/common/address'
import { Tag } from '@/components/common/badges'
import { ConfirmDialog } from '@/components/common/confirm-dialog'
import { DecimalInput } from '@/components/common/decimal-input'
import { Num } from '@/components/common/num'
import { PageHeader, Panel } from '@/components/common/panel'
import { ErrorState, SkeletonLines } from '@/components/common/states'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useControl } from '@/features/controls'
import {
  COPY_FIELDS,
  HEDGE_ACTIVATION_FIELDS,
  HEDGE_ADVANCED_FIELDS,
  HEDGE_THRESHOLD_FIELDS,
  PAPER_FIELDS,
  RISK_FIELDS,
  SIZING_FIELDS,
} from '@/features/settings/schema'
import { ConflictBanner, FieldList, SaveBar } from '@/features/settings/setting-field'
import { useSettingsForm, type SettingsForm } from '@/features/settings/use-settings-form'
import { usePatchWallet } from '@/features/wallet-mutations'
import { api } from '@/lib/api'
import { normalizeDecimal, signDecimal } from '@/lib/decimal'
import { useSettings, useWallets } from '@/lib/queries'

const ALL_FIELDS = [
  ...SIZING_FIELDS,
  ...RISK_FIELDS,
  ...COPY_FIELDS,
  ...HEDGE_ACTIVATION_FIELDS,
  ...HEDGE_THRESHOLD_FIELDS,
  ...HEDGE_ADVANCED_FIELDS,
  ...PAPER_FIELDS,
]

const SECTIONS = [
  { id: 'sizing', label: 'Sizing' },
  { id: 'risk', label: 'Risk' },
  { id: 'copy', label: 'Copy' },
  { id: 'hedge', label: 'Hedge' },
  { id: 'paper', label: 'Paper' },
  { id: 'wallets', label: 'Followed wallets' },
  { id: 'controls', label: 'Controls' },
] as const

function ResetPaper() {
  const qc = useQueryClient()
  const settings = useSettings()
  const current = settings.data?.settings.paper.starting_bankroll_usd ?? null
  const [value, setValue] = useState<string | null>(null)
  const display = value ?? normalizeDecimal(current) ?? ''
  const valid = signDecimal(display) === 1 && !/[eE]/.test(display)

  return (
    <div className="flex flex-col gap-2 border-t px-3 py-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <Label htmlFor="paper-starting-bankroll" className="text-[12.5px] font-normal">
          Starting bankroll
        </Label>
        <div className="text-[11px] text-muted-foreground">
          Applying a new bankroll wipes paper history. Current: <Num value={current} kind="usd" />
        </div>
      </div>
      <div className="flex items-start gap-2">
        <DecimalInput
          id="paper-starting-bankroll"
          data-testid="paper-starting-bankroll"
          prefix="$"
          className="w-40"
          value={display}
          invalid={!valid}
          onChange={(e) => setValue(e.target.value)}
        />
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="outline" className="border-neg/40 text-neg hover:bg-neg/10" disabled={!valid} data-testid="btn-reset-paper">
              <RotateCcw /> Reset paper account…
            </Button>
          }
          title="Reset paper account?"
          description={
            <>
              This permanently wipes all paper positions, orders, fills and equity history, and restarts with a bankroll of{' '}
              <Num value={display} kind="usd" className="text-foreground" />. Source wallet history is kept.
            </>
          }
          confirmLabel="Reset account"
          destructive
          onConfirm={async () => {
            await api.resetPaper(normalizeDecimal(display)!)
            setValue(null)
            toast.success('Paper account reset')
            await qc.invalidateQueries()
          }}
        />
      </div>
    </div>
  )
}

function FollowedWallets() {
  const q = useWallets()
  const patch = usePatchWallet()
  return (
    <Panel
      id="wallets"
      title="Followed wallets"
      actions={
        <Link to="/wallets" className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
          Manage <ArrowRight className="size-3" />
        </Link>
      }
    >
      {q.isPending ? (
        <SkeletonLines rows={3} />
      ) : !q.data ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.length === 0 ? (
        <div className="px-3 py-5 text-center text-[12px] text-muted-foreground">
          No wallets yet. <Link to="/wallets" className="text-primary hover:underline">Add a wallet</Link> to start monitoring.
        </div>
      ) : (
        <ul>
          {q.data.map((w) => (
            <li key={String(w.id)} className="flex items-center justify-between gap-3 border-b border-border/60 px-3 py-2 last:border-b-0">
              <div className="flex min-w-0 flex-col">
                <span className="truncate text-[12.5px]">{w.nickname ?? 'Unnamed'}</span>
                <AddressChip address={w.address} />
              </div>
              <label className="flex items-center gap-2 text-[11.5px] text-muted-foreground">
                {w.follow ? 'copying' : 'monitoring'}
                <Switch
                  checked={w.follow}
                  disabled={patch.isPending}
                  onCheckedChange={(follow) => patch.mutate({ id: w.id, body: { follow } })}
                  aria-label={`Copy ${w.nickname ?? w.address}`}
                />
              </label>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function Controls({ form }: { form: SettingsForm }) {
  const q = useSettings()
  const onApplied = (env: { version: number }) => form.rebaseAfterOwnChange(env.version)
  const pause = useControl('pause', onApplied)
  const resume = useControl('resume', onApplied)
  const stop = useControl('emergencyStop', onApplied)
  const clear = useControl('clearEmergency', onApplied)
  const s = q.data?.settings
  const paused = s?.strategies_paused ?? false
  const emergency = s?.emergency_stop ?? false

  return (
    <Panel id="controls" title="Controls" className={emergency ? 'border-destructive/60' : undefined}>
      <div className="flex flex-col gap-3 p-3">
        <div className="flex flex-wrap items-center gap-2 text-[12px]">
          <span className="text-muted-foreground">State</span>
          {!s ? (
            <span className="text-faint">…</span>
          ) : emergency ? (
            <Tag tone="danger">emergency stop</Tag>
          ) : paused ? (
            <Tag tone="warn">paused</Tag>
          ) : (
            <Tag tone="pos">running</Tag>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            data-testid="btn-pause"
            disabled={!s || paused || pause.isPending}
            onClick={() => pause.mutate()}
          >
            <Pause /> Pause all strategies
          </Button>
          <Button
            size="sm"
            variant="outline"
            data-testid="btn-resume"
            disabled={!s || !paused || resume.isPending}
            onClick={() => resume.mutate()}
          >
            <Play /> Resume
          </Button>
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="destructive" data-testid="btn-emergency-stop" disabled={!s || emergency}>
                <OctagonX /> Emergency stop
              </Button>
            }
            title="Engage emergency stop?"
            description="Immediately halts all copy and hedge execution and releases open reservations. Trading stays halted until the emergency stop is cleared — resuming strategies does not clear it."
            confirmLabel="Engage emergency stop"
            destructive
            onConfirm={() => stop.mutateAsync()}
          />
          {emergency && (
            <ConfirmDialog
              trigger={
                <Button size="sm" variant="outline" data-testid="btn-clear-emergency">
                  <ShieldCheck /> Clear emergency stop
                </Button>
              }
              title="Clear emergency stop?"
              description={
                paused
                  ? 'Strategies remain paused until you resume them.'
                  : 'Strategies are not paused: copying and hedging resume immediately.'
              }
              confirmLabel="Clear emergency stop"
              onConfirm={() => clear.mutateAsync()}
            />
          )}
        </div>
        <p className="text-[11px] text-muted-foreground">
          Resume does not clear an emergency stop. Paper mode only simulates fills; no live orders are placed unless the backend
          is explicitly configured for live trading.
        </p>
      </div>
    </Panel>
  )
}

export function SettingsPage() {
  const form = useSettingsForm(ALL_FIELDS)
  const settings = useSettings()
  const location = useLocation()

  useEffect(() => {
    if (!location.hash) return
    document.getElementById(location.hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [location.hash, form.ready])

  if (!form.ready && settings.isError) {
    return (
      <Panel>
        <ErrorState error={settings.error} onRetry={() => void settings.refetch()} title="Failed to load settings" />
      </Panel>
    )
  }

  return (
    <>
      <PageHeader title="Settings" description="Strategy configuration. Changes apply to new signals once saved." />
      <ConflictBanner form={form} />
      <div className="grid gap-3 lg:grid-cols-[160px_1fr]">
        <nav aria-label="Settings sections" className="hidden lg:block">
          <ul className="sticky top-0 flex flex-col gap-0.5 text-[12px]">
            {SECTIONS.map((s) => (
              <li key={s.id}>
                <a href={`#${s.id}`} className="block rounded-sm px-2 py-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
                  {s.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="flex min-w-0 flex-col gap-3">
          <div className="grid gap-3 2xl:grid-cols-2">
            <Panel id="sizing" title="Sizing">
              <FieldList fields={SIZING_FIELDS} form={form} />
            </Panel>
            <Panel id="risk" title="Risk">
              <FieldList fields={RISK_FIELDS} form={form} />
            </Panel>
            <Panel id="copy" title="Copy">
              <FieldList fields={COPY_FIELDS} form={form} />
            </Panel>
            <Panel
              id="hedge"
              title="Hedge"
              actions={
                <Link to="/hedges" className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
                  Scanner <ArrowRight className="size-3" />
                </Link>
              }
            >
              <FieldList fields={HEDGE_ACTIVATION_FIELDS} form={form} />
              <div className="label-caps border-y bg-background/40 px-3 py-1.5">Thresholds</div>
              <FieldList fields={HEDGE_THRESHOLD_FIELDS} form={form} />
              <div className="label-caps border-y bg-background/40 px-3 py-1.5">Advanced</div>
              <FieldList fields={HEDGE_ADVANCED_FIELDS} form={form} />
            </Panel>
            <Panel id="paper" title="Paper">
              <FieldList fields={PAPER_FIELDS} form={form} />
              <ResetPaper />
            </Panel>
            <div className="flex flex-col gap-3">
              <FollowedWallets />
              <Controls form={form} />
            </div>
          </div>
          <div className="sticky bottom-14 z-20 rounded-sm border bg-card/95 px-3 py-2 backdrop-blur md:bottom-0">
            <SaveBar form={form} testId="settings-save" />
          </div>
        </div>
      </div>
    </>
  )
}
