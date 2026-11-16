import { Loader2, SlidersHorizontal } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { DecimalInput } from '@/components/common/decimal-input'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { ApiError, type SizingOverride, type WalletSummary } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import { DEFAULT_SUFFIX, SIZING_FIELDS, toApi, toDisplay, validate } from './settings/schema'
import { summarizeOverride } from './sizing'
import { usePatchWallet } from './wallet-mutations'

type Draft = Record<string, string>

function toDraft(override: SizingOverride | null): Draft {
  return Object.fromEntries(
    SIZING_FIELDS.map((field) => [
      field.key,
      toDisplay(field, override?.[field.key as keyof SizingOverride]),
    ]),
  )
}

export function SizingOverrideDialog({ wallet }: { wallet: WalletSummary }) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>(() => toDraft(wallet.sizing_override))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const patch = usePatchWallet()

  const onOpenChange = (o: boolean) => {
    if (patch.isPending) return
    setOpen(o)
    if (o) {
      setDraft(toDraft(wallet.sizing_override))
      setErrors({})
    }
  }

  const save = async (override: SizingOverride | null) => {
    try {
      await patch.mutateAsync({ id: wallet.id, body: { sizing_override: override } })
      setOpen(false)
    } catch (err) {
      const fe = err instanceof ApiError ? err.fieldErrors : {}
      const mapped: Record<string, string> = {}
      for (const [path, msg] of Object.entries(fe)) {
        const key = path.split('.').pop()!
        if (SIZING_FIELDS.some((field) => field.key === key)) mapped[key] = msg
      }
      if (Object.keys(mapped).length === 0) mapped.form = errorMessage(err)
      setErrors(mapped)
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (patch.isPending) return
    const nextErrors: Record<string, string> = {}
    const override: Record<string, unknown> = {}
    for (const field of SIZING_FIELDS) {
      const raw = (draft[field.key] ?? '').trim()
      if (!raw) continue
      const problem = validate(field, raw)
      if (problem) nextErrors[field.key] = problem
      else override[field.key] = toApi(field, raw)
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return
    void save(Object.keys(override).length > 0 ? override as SizingOverride : null)
  }

  const summary = summarizeOverride(wallet.sizing_override)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <button
          type="button"
          className="group inline-flex max-w-[180px] items-center gap-1.5 rounded-sm px-1 py-0.5 text-left text-[12px] hover:bg-secondary focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={`Edit sizing override for ${wallet.nickname ?? wallet.address}`}
        >
          <SlidersHorizontal className="size-3 shrink-0 text-faint group-hover:text-foreground" />
          <span className={summary === 'global' ? 'text-muted-foreground' : 'num truncate text-foreground'}>{summary}</span>
        </button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto rounded-sm sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-[15px]">Sizing override</DialogTitle>
          <DialogDescription className="text-[12.5px]">
            Overrides the global sizing for <span className="text-foreground">{wallet.nickname ?? wallet.address}</span>.
            Leave a field blank to inherit the global value.
          </DialogDescription>
        </DialogHeader>
        <form id={`sizing-${wallet.id}`} onSubmit={submit} className="flex flex-col gap-3" noValidate>
          {SIZING_FIELDS.map((field) => (
            <div key={field.key} className="grid grid-cols-[1fr_180px] items-start gap-2">
              <div>
                <Label htmlFor={`${field.key}-${wallet.id}`} className="text-[12.5px]">
                  {field.label}
                </Label>
                {field.help && <div className="text-[11px] text-muted-foreground">{field.help}</div>}
              </div>
              <div>
                {field.kind === 'enum' ? (
                  <NativeSelect
                    id={`${field.key}-${wallet.id}`}
                    size="sm"
                    value={draft[field.key] ?? ''}
                    onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.value }))}
                    className="w-full"
                  >
                    <NativeSelectOption value="">Inherit global</NativeSelectOption>
                    {field.options?.map((option) => (
                      <NativeSelectOption key={option.value} value={option.value}>
                        {option.label}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                ) : (
                  <DecimalInput
                    id={`${field.key}-${wallet.id}`}
                    prefix={field.kind === 'usd' ? '$' : undefined}
                    suffix={field.suffix ?? DEFAULT_SUFFIX[field.kind]}
                    placeholder="global"
                    value={draft[field.key] ?? ''}
                    invalid={Boolean(errors[field.key])}
                    onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.value }))}
                  />
                )}
                {errors[field.key] && <div role="alert" className="mt-1 text-[11px] text-neg">{errors[field.key]}</div>}
              </div>
            </div>
          ))}
          {errors.form && (
            <p role="alert" className="rounded-sm border border-neg/30 bg-neg/10 px-2.5 py-1.5 text-[12px] text-neg">
              {errors.form}
            </p>
          )}
        </form>
        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={patch.isPending || !wallet.sizing_override}
            onClick={() => void save(null)}
          >
            Clear override
          </Button>
          <Button type="submit" form={`sizing-${wallet.id}`} size="sm" disabled={patch.isPending}>
            {patch.isPending && <Loader2 className="animate-spin" />}
            Save override
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
