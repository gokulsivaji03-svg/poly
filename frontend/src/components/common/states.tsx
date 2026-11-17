import { AlertTriangle, RotateCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn, errorMessage } from '@/lib/utils'

/** A table or panel with nothing in it yet: one muted line, no illustration. */
export function EmptyState({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-baseline gap-x-2 gap-y-1 px-3 py-3 text-left', className)}>
      <span className="text-[12px] text-muted-foreground">{title}</span>
      {description && <span className="text-[11.5px] text-faint">{description}</span>}
      {action && <div className="basis-full">{action}</div>}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
  title = 'Failed to load',
  className,
}: {
  error: unknown
  onRetry?: () => void
  title?: string
  className?: string
}) {
  return (
    <div role="alert" className={cn('flex flex-col items-center justify-center gap-2 px-4 py-8 text-center', className)}>
      <AlertTriangle className="size-5 text-neg" aria-hidden />
      <div className="text-[13px] font-medium">{title}</div>
      <div className="max-w-md text-[12px] text-muted-foreground">{errorMessage(error)}</div>
      {onRetry && (
        <Button size="xs" variant="outline" onClick={onRetry} className="mt-1">
          <RotateCw /> Retry
        </Button>
      )}
    </div>
  )
}

export function SkeletonLines({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2 p-3', className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-4" style={{ width: `${92 - ((i * 17) % 35)}%` }} />
      ))}
    </div>
  )
}
