import type { SizingMode, SizingOverride } from '@/lib/api'
import { fmtNum, fmtPct, fmtUsd } from '@/lib/format'

const SIZING_MODE_LABEL: Record<SizingMode, string> = {
  PROPORTIONAL: 'Proportional',
  FIXED_AMOUNT: 'Fixed amount',
  FIXED_PCT_OF_USER: 'Fixed % of bankroll',
  SOURCE_MULTIPLE: 'Source multiple',
}

/** One-line description of a per-wallet sizing override ("global" when none). */
export function summarizeOverride(o: SizingOverride | null | undefined): string {
  if (!o || Object.keys(o).length === 0) return 'global'
  const parts: string[] = []
  if (o.mode) parts.push(SIZING_MODE_LABEL[o.mode] ?? o.mode)
  if (o.multiplier != null) parts.push(`×${fmtNum(o.multiplier, 4, 0)}`)
  if (o.fixed_amount_usd != null) parts.push(fmtUsd(o.fixed_amount_usd) ?? o.fixed_amount_usd)
  if (o.fixed_pct_of_user != null) parts.push(fmtPct(o.fixed_pct_of_user, { dp: 2 }) ?? o.fixed_pct_of_user)
  if (o.source_multiple != null) parts.push(`${fmtNum(o.source_multiple, 4, 0)}× src`)
  if (o.unknown_equity_policy) parts.push(`unknown equity: ${o.unknown_equity_policy === 'SKIP' ? 'skip' : 'fixed'}`)
  if (o.lower_bound_equity_policy) parts.push(`lower bound: ${o.lower_bound_equity_policy === 'SKIP' ? 'skip' : 'capped'}`)
  if (o.min_source_confidence != null) parts.push(`confidence ≥ ${fmtNum(o.min_source_confidence, 2)}`)
  return parts.join(' · ')
}
