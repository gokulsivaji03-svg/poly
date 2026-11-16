import { Link } from 'react-router'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Reason } from '@/lib/api'
import { fmtDateTime, fmtRelative, fmtTime, humanizeCode, truncateAddress } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Tag } from './badges'
import { Unavailable } from './num'

export function TimeCell({ iso, relative = false }: { iso: string | null | undefined; relative?: boolean }) {
  const text = relative ? fmtRelative(iso) : fmtTime(iso)
  if (!text) return <Unavailable />
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="num cursor-default text-[12px] text-muted-foreground outline-none">
          {text}
        </span>
      </TooltipTrigger>
      <TooltipContent className="num">{fmtDateTime(iso)}</TooltipContent>
    </Tooltip>
  )
}

export function MarketCell({ title, sub, className }: { title: string | null | undefined; sub?: string | null; className?: string }) {
  if (!title) return <Unavailable reason="market metadata missing" />
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className={cn('block max-w-[280px] truncate outline-none', className)}>
          {title}
          {sub && <span className="ml-1.5 text-muted-foreground">{sub}</span>}
        </span>
      </TooltipTrigger>
      <TooltipContent>{title}</TooltipContent>
    </Tooltip>
  )
}

export function WalletCell({
  id,
  nickname,
  address,
}: {
  id: string | number
  nickname: string | null | undefined
  address: string
}) {
  return (
    <Link
      to={`/wallets/${encodeURIComponent(String(id))}`}
      className="inline-flex max-w-[160px] items-baseline gap-1.5 truncate hover:text-primary focus-visible:text-primary focus-visible:outline-none"
    >
      {nickname ? (
        <span className="truncate">{nickname}</span>
      ) : (
        <span className="num text-[12px]">{truncateAddress(address)}</span>
      )}
    </Link>
  )
}

/** Ordered reduction / skip reasons: codes as tags, messages in the tooltip. */
export function ReasonsCell({ reasons }: { reasons: Reason[] | string[] | null | undefined }) {
  if (!reasons || reasons.length === 0) return <span className="text-faint">—</span>
  const items = reasons.map((r) => (typeof r === 'string' ? { code: r, message: humanizeCode(r) } : r))
  const first = items[0]!
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 outline-none">
          <Tag tone="warn" className="normal-case">
            {first.code}
          </Tag>
          {items.length > 1 && <span className="num text-[11px] text-muted-foreground">+{items.length - 1}</span>}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm">
        <ol className="flex list-decimal flex-col gap-1 pl-4">
          {items.map((r, i) => (
            <li key={`${r.code}-${i}`}>
              <span className="num text-[11px] text-warn">{r.code}</span>
              {r.message && r.message !== r.code && <span className="block text-muted-foreground">{r.message}</span>}
            </li>
          ))}
        </ol>
      </TooltipContent>
    </Tooltip>
  )
}

export function MonoId({ value, head = 8 }: { value: string | number | null | undefined; head?: number }) {
  if (value == null || value === '') return <Unavailable />
  const s = String(value)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="num text-[11.5px] text-muted-foreground outline-none">
          {s.length > head + 5 ? truncateAddress(s, head, 4) : s}
        </span>
      </TooltipTrigger>
      <TooltipContent className="num break-all">{s}</TooltipContent>
    </Tooltip>
  )
}
