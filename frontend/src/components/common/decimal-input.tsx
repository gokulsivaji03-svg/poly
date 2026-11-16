import type { ComponentProps, ReactNode } from 'react'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/** Text input for decimal strings with optional prefix/suffix adornments ("$", "%", "¢"). */
export function DecimalInput({
  prefix,
  suffix,
  invalid,
  className,
  ...props
}: Omit<ComponentProps<'input'>, 'prefix' | 'type'> & {
  prefix?: ReactNode
  suffix?: ReactNode
  invalid?: boolean
}) {
  return (
    <div className={cn('relative flex items-center', className)}>
      {prefix && (
        <span className="pointer-events-none absolute left-2.5 text-[12px] text-muted-foreground" aria-hidden>
          {prefix}
        </span>
      )}
      <Input
        type="text"
        inputMode="decimal"
        autoComplete="off"
        spellCheck={false}
        aria-invalid={invalid || undefined}
        className={cn('num h-8 text-right text-[12.5px]', prefix ? 'pl-6' : '', suffix ? 'pr-9' : '')}
        {...props}
      />
      {suffix && (
        <span className="pointer-events-none absolute right-2.5 text-[11.5px] text-muted-foreground" aria-hidden>
          {suffix}
        </span>
      )}
    </div>
  )
}
