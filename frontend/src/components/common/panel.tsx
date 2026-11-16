import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  testId,
  id,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  testId?: string
  id?: string
}) {
  return (
    <section
      id={id}
      data-testid={testId}
      className={cn('flex min-w-0 flex-col rounded-sm border bg-card', className)}
    >
      {(title || actions) && (
        <header className="flex min-h-9 flex-wrap items-center justify-between gap-2 border-b px-3 py-1.5">
          {title && <h2 className="label-caps text-[11px] text-foreground/75">{title}</h2>}
          {actions && <div className="flex items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn('min-w-0 flex-1', bodyClassName)}>{children}</div>
    </section>
  )
}

/** Label (small, muted, uppercase) over a prominent value. */
export function Stat({
  label,
  value,
  sub,
  testId,
  className,
}: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  testId?: string
  className?: string
}) {
  return (
    <div data-testid={testId} className={cn('min-w-0 px-3 py-2.5', className)}>
      <div className="label-caps truncate">{label}</div>
      <div className="mt-1 truncate text-[17px] leading-tight font-medium text-foreground">{value}</div>
      {sub && <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

/** Compact key/value row used in detail cards. */
export function KV({ label, children, className }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex items-baseline justify-between gap-3 py-1', className)}>
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span className="text-right text-[12.5px]">{children}</span>
    </div>
  )
}

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-[15px] font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-0.5 text-[12px] text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
