import { AlertTriangle, Loader2, RotateCcw, Save } from 'lucide-react'
import { DecimalInput } from '@/components/common/decimal-input'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { DEFAULT_SUFFIX, pathOf, type FieldDef } from './schema'
import type { SettingsForm } from './use-settings-form'

export function SettingField({ field, form }: { field: FieldDef; form: SettingsForm }) {
  const id = `setting-${pathOf(field).replace('.', '-')}`
  const value = form.value(field)
  const error = form.error(field)
  const dirty = form.isDirty(field)

  let control
  if (!form.ready) {
    control = <Skeleton className="h-8 w-full" />
  } else if (field.kind === 'bool') {
    control = (
      <div className="flex h-8 items-center justify-end">
        <Switch
          id={id}
          data-testid={field.testId}
          checked={value === 'true'}
          onCheckedChange={(v) => form.setValue(field, v ? 'true' : 'false')}
        />
      </div>
    )
  } else if (field.kind === 'enum') {
    control = (
      <NativeSelect
        id={id}
        data-testid={field.testId}
        size="sm"
        className="w-full text-[12.5px]"
        value={value}
        onChange={(e) => form.setValue(field, e.target.value)}
      >
        {field.options?.map((o) => (
          <NativeSelectOption key={o.value} value={o.value}>
            {o.label}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    )
  } else {
    control = (
      <DecimalInput
        id={id}
        data-testid={field.testId}
        prefix={field.kind === 'usd' ? '$' : undefined}
        suffix={field.suffix ?? DEFAULT_SUFFIX[field.kind]}
        value={value}
        placeholder={field.optional ? 'No limit' : undefined}
        invalid={Boolean(error)}
        aria-describedby={error ? `${id}-error` : undefined}
        onChange={(e) => form.setValue(field, e.target.value)}
      />
    )
  }

  return (
    <div
      data-dirty={dirty || undefined}
      className={cn(
        'relative grid grid-cols-1 gap-1.5 border-b border-border/60 px-3 py-2 last:border-b-0 sm:grid-cols-[1fr_220px] sm:items-center sm:gap-4',
        dirty && 'bg-primary/[0.04]',
      )}
    >
      {dirty && <span aria-hidden className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-primary" />}
      <div className="min-w-0">
        <Label htmlFor={id} className="text-[12.5px] font-normal text-foreground">
          {field.label}
          {dirty && <span className="label-caps ml-1.5 text-[9.5px] text-primary">modified</span>}
        </Label>
        {field.help && <div className="mt-0.5 text-[11px] text-muted-foreground">{field.help}</div>}
      </div>
      <div>
        {control}
        {error && (
          <div id={`${id}-error`} role="alert" className="mt-1 text-right text-[11px] text-neg">
            {error}
          </div>
        )}
      </div>
    </div>
  )
}

export function FieldList({ fields, form }: { fields: readonly FieldDef[]; form: SettingsForm }) {
  return (
    <div>
      {fields.map((f) => (
        <SettingField key={pathOf(f)} field={f} form={form} />
      ))}
    </div>
  )
}

/** 409 prompt: the server copy moved on since editing began. */
export function ConflictBanner({ form }: { form: SettingsForm }) {
  if (!form.conflict) return null
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-sm border border-warn/30 bg-warn/10 px-3 py-2 text-[12px]">
      <AlertTriangle className="size-4 text-warn" />
      <span className="flex-1">
        Settings were changed elsewhere since you started editing. Reload the latest version — your unsaved edits are kept
        and re-applied on top — then save again.
      </span>
      <Button size="xs" variant="outline" onClick={() => void form.reload()}>
        <RotateCcw /> Reload latest
      </Button>
    </div>
  )
}

export function SaveBar({
  form,
  testId,
  className,
}: {
  form: SettingsForm
  testId?: string
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-center justify-end gap-2', className)}>
      {form.formError && <span className="mr-auto text-[12px] text-neg">{form.formError}</span>}
      {!form.formError && (
        <span className="mr-auto text-[11.5px] text-muted-foreground">
          {form.dirtyCount > 0 ? (
            <span className="text-primary">
              {form.dirtyCount} unsaved change{form.dirtyCount === 1 ? '' : 's'}
            </span>
          ) : (
            'No unsaved changes'
          )}
          {form.version !== null && <span className="num ml-2 text-faint">v{form.version}</span>}
        </span>
      )}
      <Button size="sm" variant="ghost" onClick={form.discard} disabled={form.dirtyCount === 0 || form.saving}>
        Discard
      </Button>
      <Button
        size="sm"
        data-testid={testId}
        onClick={form.save}
        disabled={form.dirtyCount === 0 || form.hasErrors || form.saving || form.conflict}
      >
        {form.saving ? <Loader2 className="animate-spin" /> : <Save />}
        Save
      </Button>
    </div>
  )
}
