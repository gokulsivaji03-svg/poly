/**
 * Settings field descriptors and display <-> API conversions.
 *
 * Percent fields ("_pct") are fractions on the API but edited as percent; per-share amounts and the
 * max hedge price are edited in cents. Both conversions are exact decimal-string shifts.
 */
import type { SettingsPatch, SettingsSection, StrategySettings } from '@/lib/api'
import { compareDecimal, isDecimalString, normalizeDecimal, shiftDecimal } from '@/lib/decimal'

export type FieldKind = 'usd' | 'pct' | 'cents' | 'decimal' | 'int' | 'bool' | 'enum'

export interface FieldDef {
  section: SettingsSection
  key: string
  label: string
  kind: FieldKind
  help?: string
  options?: readonly { value: string; label: string }[]
  /** Unit adornment for numeric inputs (defaults by kind). */
  suffix?: string
  /** Zero is rejected by the backend for strictly-positive settings. */
  positive?: boolean
  /** Upper bound in display units, inclusive (e.g. 100 for percent). */
  max?: string
  maxExclusive?: boolean
  testId?: string
}

export const pathOf = (f: Pick<FieldDef, 'section' | 'key'>) => `${f.section}.${f.key}`

const opts = (...values: [string, string][]) => values.map(([value, label]) => ({ value, label }))

export const SIZING_FIELDS: FieldDef[] = [
  {
    section: 'sizing',
    key: 'mode',
    label: 'Sizing method',
    kind: 'enum',
    options: opts(
      ['PROPORTIONAL', 'Proportional to source allocation'],
      ['FIXED_AMOUNT', 'Fixed USD amount'],
      ['FIXED_PCT_OF_USER', 'Fixed % of your bankroll'],
      ['SOURCE_MULTIPLE', 'Multiple of source cost'],
    ),
    help: 'copy = your equity × source allocation % × multiplier (proportional)',
  },
  { section: 'sizing', key: 'multiplier', label: 'Multiplier', kind: 'decimal', positive: true, max: '10', suffix: '×', testId: 'settings-multiplier', help: 'Scales proportional copy size' },
  { section: 'sizing', key: 'fixed_amount_usd', label: 'Fixed amount', kind: 'usd', positive: true, help: 'USD per trade (fixed amount / fallback)' },
  { section: 'sizing', key: 'fixed_pct_of_user', label: '% of your bankroll', kind: 'pct', positive: true, max: '100', help: 'Per trade, fixed-% method' },
  { section: 'sizing', key: 'source_multiple', label: 'Source multiple', kind: 'decimal', positive: true, suffix: '×', help: 'Multiple of the source trade cost' },
  {
    section: 'sizing',
    key: 'unknown_equity_policy',
    label: 'Unknown source equity',
    kind: 'enum',
    options: opts(['SKIP', 'Skip the trade'], ['FALLBACK_FIXED', 'Fall back to fixed amount']),
  },
  {
    section: 'sizing',
    key: 'lower_bound_equity_policy',
    label: 'Lower-bound source equity',
    kind: 'enum',
    options: opts(['USE_CAPPED', 'Use, capped by risk limits'], ['SKIP', 'Skip the trade']),
    help: 'When source cash is unobservable (positions-only estimate)',
  },
  { section: 'sizing', key: 'min_source_confidence', label: 'Min equity confidence', kind: 'decimal', max: '1', help: '0–1; below this the trade is skipped' },
]

export const RISK_FIELDS: FieldDef[] = [
  { section: 'risk', key: 'max_pct_per_trade', label: 'Max % per trade', kind: 'pct', positive: true, max: '100', help: 'Of your equity' },
  { section: 'risk', key: 'max_position_usd', label: 'Max position', kind: 'usd', positive: true, help: 'Per token' },
  { section: 'risk', key: 'max_market_exposure_usd', label: 'Max market exposure', kind: 'usd', positive: true, help: 'Per condition, both outcomes' },
  { section: 'risk', key: 'max_total_exposure_pct', label: 'Max total exposure', kind: 'pct', positive: true, max: '100', help: 'Open positions as % of equity' },
  { section: 'risk', key: 'min_liquidity_usd', label: 'Min book liquidity', kind: 'usd' },
  { section: 'risk', key: 'max_price_diff_pct', label: 'Max price drift vs source', kind: 'pct', max: '100' },
  { section: 'risk', key: 'max_slippage_pct', label: 'Max slippage vs best ask', kind: 'pct', max: '100' },
  { section: 'risk', key: 'max_detection_delay_s', label: 'Max detection delay', kind: 'int', positive: true, suffix: 's', help: 'Older source trades are skipped' },
  { section: 'risk', key: 'min_cash_reserve_usd', label: 'Min cash reserve', kind: 'usd' },
  { section: 'risk', key: 'daily_loss_limit_usd', label: 'Daily loss limit', kind: 'usd', positive: true },
  { section: 'risk', key: 'max_book_age_s', label: 'Max book age', kind: 'int', positive: true, suffix: 's' },
  { section: 'risk', key: 'min_order_usd', label: 'Min order size', kind: 'usd' },
  { section: 'risk', key: 'max_entry_price', label: 'Max entry price', kind: 'cents', positive: true, max: '100', help: 'Skip source buys priced above this' },
  { section: 'risk', key: 'max_buys_per_market', label: 'Max buys per market', kind: 'int', positive: true, help: 'Filled copy buys per condition; stops averaging down' },
]

export const COPY_FIELDS: FieldDef[] = [
  { section: 'copy', key: 'enabled', label: 'Copy trading enabled', kind: 'bool' },
  {
    section: 'copy',
    key: 'sell_policy',
    label: 'Sell policy',
    kind: 'enum',
    options: opts(['PROPORTIONAL', 'Proportional'], ['FULL_EXIT', 'Full exit'], ['IGNORE', 'Ignore source sells']),
  },
  {
    section: 'copy',
    key: 'hedged_sell_policy',
    label: 'Hedged sell policy',
    kind: 'enum',
    options: opts(['PRESERVE_HEDGE', 'Preserve hedge (sell unhedged only)'], ['UNWIND_IF_BETTER', 'Unwind if better than locked value']),
  },
  { section: 'copy', key: 'copy_sells', label: 'Copy source sells', kind: 'bool' },
]

export const HEDGE_ACTIVATION_FIELDS: FieldDef[] = [
  { section: 'hedge', key: 'enabled', label: 'Hedge scanner enabled', kind: 'bool' },
  {
    section: 'hedge',
    key: 'mode',
    label: 'Hedge mode',
    kind: 'enum',
    options: opts(['ALERT_ONLY', 'Alert only'], ['AUTO_PAPER', 'Automatic paper hedging']),
  },
]

export const HEDGE_THRESHOLD_FIELDS: FieldDef[] = [
  { section: 'hedge', key: 'min_net_gap_per_share', label: 'Min net gap / share', kind: 'cents', help: 'After fees and other costs' },
  { section: 'hedge', key: 'min_abs_profit_usd', label: 'Min absolute profit', kind: 'usd' },
  { section: 'hedge', key: 'min_return_pct', label: 'Min return', kind: 'pct', help: 'Locked profit / capital required' },
  { section: 'hedge', key: 'min_liquidity_usd', label: 'Min liquidity', kind: 'usd', help: 'Profitable depth on the opposite book' },
  { section: 'hedge', key: 'max_hedge_price', label: 'Max hedge price', kind: 'cents', positive: true, max: '100', maxExclusive: true },
  { section: 'hedge', key: 'min_hedge_shares', label: 'Min hedge quantity', kind: 'decimal', suffix: 'sh' },
  { section: 'hedge', key: 'max_hedge_shares', label: 'Max hedge quantity', kind: 'decimal', positive: true, suffix: 'sh' },
  { section: 'hedge', key: 'full_hedge_only', label: 'Full hedge only', kind: 'bool', help: 'Skip partial hedges' },
]

export const HEDGE_ADVANCED_FIELDS: FieldDef[] = [
  { section: 'hedge', key: 'other_cost_per_share', label: 'Other cost / share', kind: 'cents', help: 'Gas, settlement and other per-pair costs' },
  { section: 'hedge', key: 'allow_neg_risk', label: 'Allow neg-risk markets', kind: 'bool' },
  { section: 'hedge', key: 'scan_interval_s', label: 'Scan interval', kind: 'int', positive: true, suffix: 's' },
]

export const PAPER_FIELDS: FieldDef[] = [
  { section: 'paper', key: 'execution_delay_ms', label: 'Execution delay', kind: 'int', max: '60000', suffix: 'ms', help: 'Simulated order latency' },
  {
    section: 'paper',
    key: 'buy_fee_mode',
    label: 'Buy fee mode',
    kind: 'enum',
    options: opts(['shares', 'Deduct from shares'], ['cash', 'Charge in cash']),
  },
  { section: 'paper', key: 'share_step', label: 'Share step', kind: 'decimal', positive: true, suffix: 'sh', help: 'Order size granularity' },
  { section: 'paper', key: 'extra_slippage_bps', label: 'Extra slippage', kind: 'int', max: '1000', suffix: 'bps' },
]

export const DEFAULT_SUFFIX: Partial<Record<FieldKind, string>> = { pct: '%', cents: '¢' }

function readRaw(settings: StrategySettings, f: FieldDef): unknown {
  const section = settings[f.section] as unknown as Record<string, unknown> | undefined
  return section?.[f.key]
}

/** API value -> string shown in the form. */
export function toDisplay(f: FieldDef, raw: unknown): string {
  if (f.kind === 'bool') return raw ? 'true' : 'false'
  if (raw === null || raw === undefined) return ''
  const s = String(raw)
  if (f.kind === 'enum') return s
  if (f.kind === 'pct' || f.kind === 'cents') return shiftDecimal(s, 2) ?? s
  return normalizeDecimal(s) ?? s
}

export function displayFromSettings(settings: StrategySettings, f: FieldDef): string {
  return toDisplay(f, readRaw(settings, f))
}

export type ApiValue = string | number | boolean | null

/** Form string -> API value (fraction strings for percent, dollars for cents). */
export function toApi(f: FieldDef, display: string): ApiValue {
  if (f.kind === 'bool') return display === 'true'
  if (f.kind === 'enum') return display
  const s = display.trim()
  if (s === '') return null
  if (f.kind === 'int') return Number(s)
  if (f.kind === 'pct' || f.kind === 'cents') return shiftDecimal(s, -2)
  return normalizeDecimal(s)
}

/** Client-side validation of a form string; null when valid. */
export function validate(f: FieldDef, display: string): string | null {
  if (f.kind === 'bool' || f.kind === 'enum') return null
  const s = display.trim()
  if (s === '') return 'Required'
  if (!isDecimalString(s) || /[eE]/.test(s)) return 'Enter a decimal number'
  if (s.startsWith('-')) return 'Must be ≥ 0'
  if (f.positive && compareDecimal(s, '0') === 0) return 'Must be > 0'
  if (f.kind === 'int' && (s.includes('.') || !Number.isSafeInteger(Number(s)))) return 'Whole number within the safe integer range'
  if (f.max !== undefined) {
    const comparison = compareDecimal(s, f.max)
    if (comparison === 1 || (f.maxExclusive && comparison === 0)) return `Must be ${f.maxExclusive ? '<' : '≤'} ${f.max}`
  }
  return null
}

/** Display values are equal when they map to the same canonical API value. */
export function sameValue(f: FieldDef, a: string, b: string): boolean {
  const x = toApi(f, a)
  const y = toApi(f, b)
  return x === y
}

export function buildPatch(entries: { field: FieldDef; display: string }[]): SettingsPatch {
  const patch: Record<string, Record<string, ApiValue>> = {}
  for (const { field, display } of entries) {
    ;(patch[field.section] ??= {})[field.key] = toApi(field, display)
  }
  return patch as SettingsPatch
}
