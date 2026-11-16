import { cn } from '@/lib/utils'

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  label,
  format = (o) => o.toUpperCase(),
}: {
  options: readonly T[]
  value: T
  onChange: (v: T) => void
  label: string
  format?: (o: T) => string
}) {
  return (
    <div role="group" aria-label={label} className="flex rounded-sm border p-0.5">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          aria-pressed={value === o}
          onClick={() => onChange(o)}
          className={cn(
            'num rounded-[2px] px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none',
            value === o && 'bg-secondary text-foreground',
          )}
        >
          {format(o)}
        </button>
      ))}
    </div>
  )
}
