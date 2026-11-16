import type { LucideIcon } from 'lucide-react'
import { AlertTriangle, Inbox, RotateCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn, errorMessage } from '@/lib/utils'

export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <Icon className="size-5 text-faint" aria-hidden />
      <div className="text-[13px] font-medium text-foreground/90">{title}</div>
      {description && <div className="max-w-sm text-[12px] text-muted-foreground">{description}</div>}
      {action && <div className="mt-1">{action}</div>}
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
