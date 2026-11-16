import { Check, Copy } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { truncateAddress } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Truncated hex address with a copy-to-clipboard affordance. */
export function AddressChip({ address, className }: { address: string; className?: string }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const t = window.setTimeout(() => setCopied(false), 1200)
    return () => clearTimeout(t)
  }, [copied])

  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await navigator.clipboard.writeText(address)
      setCopied(true)
    } catch {
      toast.error('Clipboard unavailable')
    }
  }

  return (
    <span className={cn('inline-flex items-center gap-1', className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="num text-[12px] text-muted-foreground outline-none">
            {truncateAddress(address)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="num">{address}</TooltipContent>
      </Tooltip>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy address"
        className="rounded-sm p-0.5 text-faint transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
      >
        {copied ? <Check className="size-3 text-pos" /> : <Copy className="size-3" />}
      </button>
    </span>
  )
}
