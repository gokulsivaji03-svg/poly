/**
 * Display formatters. All inputs are API decimal strings (or ints for counts/latencies);
 * every formatter returns `null` when the value is unavailable so callers can render the
 * em-dash "unavailable" marker instead of a misleading zero.
 */
import { decimalParts, parseDec, shiftDecimal, type Dec } from './decimal'

export const UNAVAILABLE = '—'

type Maybe<T> = T | null | undefined

export interface UsdOptions {
  dp?: number
  signed?: boolean
  /** Compact suffixes ($12.3K, $4.5M) for very large values. */
  compact?: boolean
}

export function fmtUsd(v: Maybe<string>, { dp = 2, signed = false, compact = false }: UsdOptions = {}): string | null {
  const d = parseDec(v)
  if (!d) return null
  if (compact) {
    const abs = d.coef < 0n ? -d.coef : d.coef
    const intDigits = abs.toString().length - d.scale
    if (intDigits > 6) return compactUsd(d, 6, 'M', signed)
    if (intDigits > 4) return compactUsd(d, 3, 'K', signed)
  }
  const { sign, int, frac } = decimalParts(d, { dp, signed })
  return `${sign}$${int}${frac ? `.${frac}` : ''}`
}

function compactUsd(d: Dec, shift: number, suffix: string, signed: boolean): string {
  const { sign, int, frac } = decimalParts({ coef: d.coef, scale: d.scale + shift }, { dp: 1, minDp: 0, signed })
  return `${sign}$${int}${frac ? `.${frac}` : ''}${suffix}`
}

export function fmtShares(v: Maybe<string>, dp = 2): string | null {
  const d = parseDec(v)
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, { dp })
  return `${sign}${int}${frac ? `.${frac}` : ''}`
}

/** Plain decimal number with fixed precision (e.g. confidence "0.62", multipliers). */
export function fmtNum(v: Maybe<string>, dp = 2, minDp = dp): string | null {
  const d = parseDec(v)
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, { dp, minDp })
  return `${sign}${int}${frac ? `.${frac}` : ''}`
}

/** Outcome price in dollars (0–1). `cents` renders "52.3¢". */
export function fmtPrice(v: Maybe<string>, { cents = false, dp }: { cents?: boolean; dp?: number } = {}): string | null {
  if (cents) {
    const d = parseDec(shiftDecimal(v, 2))
    if (!d) return null
    const { sign, int, frac } = decimalParts(d, { dp: dp ?? 2, minDp: 1 })
    return `${sign}${int}${frac ? `.${frac}` : ''}¢`
  }
  const d = parseDec(v)
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, { dp: dp ?? 4, minDp: 2 })
  return `${sign}${int}${frac ? `.${frac}` : ''}`
}

/** API fraction ("0.0215") -> "2.15%". */
export function fmtPct(v: Maybe<string>, { dp = 2, signed = false }: { dp?: number; signed?: boolean } = {}): string | null {
  const d = parseDec(shiftDecimal(v, 2))
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, { dp, signed })
  return `${sign}${int}${frac ? `.${frac}` : ''}%`
}

export function fmtBps(v: Maybe<string | number>, { dp = 1, signed = true }: { dp?: number; signed?: boolean } = {}): string | null {
  const d = parseDec(v == null ? null : String(v))
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, { dp, signed })
  return `${sign}${int}${frac ? `.${frac}` : ''} bps`
}

export function fmtInt(v: Maybe<number | string>): string | null {
  const d = parseDec(v == null ? null : String(v))
  if (!d) return null
  const { sign, int } = decimalParts(d, { dp: 0 })
  return `${sign}${int}`
}

/** Latency in ms: "850 ms", "4.10 s", "2.5 min". */
export function fmtMs(v: Maybe<number | string>): string | null {
  if (v == null || v === '') return null
  const ms = Number(v)
  if (!Number.isFinite(ms)) return null
  if (Math.abs(ms) < 1000) return `${Math.round(ms)} ms`
  if (Math.abs(ms) < 120_000) return `${(ms / 1000).toFixed(2)} s`
  return `${(ms / 60_000).toFixed(1)} min`
}

export function fmtSeconds(v: Maybe<number | string>): string | null {
  if (v == null || v === '') return null
  const s = Number(v)
  return Number.isFinite(s) ? fmtMs(s * 1000) : null
}

function toDate(iso: Maybe<string>): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function fmtTime(iso: Maybe<string>): string | null {
  const d = toDate(iso)
  return d ? timeFmt.format(d) : null
}

export function fmtDateTime(iso: Maybe<string>): string | null {
  const d = toDate(iso)
  return d ? dateTimeFmt.format(d) : null
}

export function fmtRelative(iso: Maybe<string>, now: number = Date.now()): string | null {
  const d = toDate(iso)
  if (!d) return null
  const s = Math.round((now - d.getTime()) / 1000)
  if (s < 0) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86_400)}d ago`
}

export function truncateAddress(address: string, head = 6, tail = 4): string {
  if (address.length <= head + tail + 1) return address
  return `${address.slice(0, head)}…${address.slice(-tail)}`
}

/** "STALE_SIGNAL" -> "Stale signal". */
export function humanizeCode(code: string): string {
  const s = code.replace(/_/g, ' ').toLowerCase()
  return s.charAt(0).toUpperCase() + s.slice(1)
}
