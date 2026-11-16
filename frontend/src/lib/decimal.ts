/**
 * Tiny exact decimal-string arithmetic.
 *
 * The API sends money, shares, prices and percentages as decimal strings. We never parse them
 * as floats for arithmetic or display; values are held as a BigInt coefficient and a base-10
 * scale (value = coef / 10^scale). Floats are only produced by `toNumber` for chart plotting.
 */

export interface Dec {
  coef: bigint
  scale: number
}

const DECIMAL_RE = /^([+-])?(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/

const pow10 = (n: number): bigint => 10n ** BigInt(n)

/** Parse a decimal string (plain or exponent notation). Returns null for anything else. */
export function parseDec(input: string | null | undefined): Dec | null {
  if (input == null) return null
  const s = input.trim()
  const m = DECIMAL_RE.exec(s)
  if (!m) return null
  const [, sign = '', int = '', frac = '', exp = '0'] = m
  if (int.length === 0 && frac.length === 0) return null
  let coef = BigInt(`${sign === '-' ? '-' : ''}${int || '0'}${frac}`)
  let scale = frac.length - Number.parseInt(exp, 10)
  if (scale < 0) {
    coef *= pow10(-scale)
    scale = 0
  }
  return normalize({ coef, scale })
}

export function isDecimalString(input: string | null | undefined): boolean {
  return parseDec(input) !== null
}

/** Strip trailing zeros from the fraction so equal values have equal representations. */
function normalize(d: Dec): Dec {
  let { coef, scale } = d
  while (scale > 0 && coef % 10n === 0n) {
    coef /= 10n
    scale -= 1
  }
  return { coef, scale }
}

/** Canonical plain-notation string: no exponent, no trailing fraction zeros, no "-0". */
export function decToString(d: Dec): string {
  const neg = d.coef < 0n
  const digits = (neg ? -d.coef : d.coef).toString()
  if (d.scale === 0) return neg ? `-${digits}` : digits
  const padded = digits.padStart(d.scale + 1, '0')
  const int = padded.slice(0, padded.length - d.scale)
  const frac = padded.slice(padded.length - d.scale)
  return `${neg ? '-' : ''}${int}.${frac}`
}

/** Normalize a decimal string ("012.3400" -> "12.34", "1e-3" -> "0.001"). */
export function normalizeDecimal(input: string | null | undefined): string | null {
  const d = parseDec(input)
  return d ? decToString(d) : null
}

function align(a: Dec, b: Dec): [bigint, bigint, number] {
  const scale = Math.max(a.scale, b.scale)
  return [a.coef * pow10(scale - a.scale), b.coef * pow10(scale - b.scale), scale]
}

/** Exact sum; null if either side is unavailable or not a decimal. */
export function addDecimal(a: string | null | undefined, b: string | null | undefined): string | null {
  const da = parseDec(a)
  const db = parseDec(b)
  if (!da || !db) return null
  const [x, y, scale] = align(da, db)
  return decToString(normalize({ coef: x + y, scale }))
}

export function negateDecimal(a: string | null | undefined): string | null {
  const d = parseDec(a)
  return d ? decToString({ coef: -d.coef, scale: d.scale }) : null
}

/** Multiply by 10^n exactly (n may be negative). */
export function shiftDecimal(input: string | null | undefined, n: number): string | null {
  const d = parseDec(input)
  if (!d) return null
  let scale = d.scale - n
  let coef = d.coef
  if (scale < 0) {
    coef *= pow10(-scale)
    scale = 0
  }
  return decToString(normalize({ coef, scale }))
}

/** API fraction ("0.02") -> UI percent ("2"). */
export function fractionToPercent(fraction: string | null | undefined): string | null {
  return shiftDecimal(fraction, 2)
}

/** UI percent ("2") -> API fraction ("0.02"), exact. */
export function percentToFraction(percent: string | null | undefined): string | null {
  return shiftDecimal(percent, -2)
}

/** -1, 0 or 1; null if unavailable. */
export function signDecimal(input: string | null | undefined): -1 | 0 | 1 | null {
  const d = parseDec(input)
  if (!d) return null
  return d.coef === 0n ? 0 : d.coef < 0n ? -1 : 1
}

/** Compare two decimals; null if either is unavailable. */
export function compareDecimal(
  a: string | null | undefined,
  b: string | null | undefined,
): -1 | 0 | 1 | null {
  const da = parseDec(a)
  const db = parseDec(b)
  if (!da || !db) return null
  const [x, y] = align(da, db)
  return x === y ? 0 : x < y ? -1 : 1
}

/** Round half away from zero to `dp` fraction digits. */
export function roundDec(d: Dec, dp: number): Dec {
  if (d.scale <= dp) return d
  const divisor = pow10(d.scale - dp)
  const neg = d.coef < 0n
  const abs = neg ? -d.coef : d.coef
  let q = abs / divisor
  const r = abs % divisor
  if (r * 2n >= divisor) q += 1n
  return { coef: neg ? -q : q, scale: dp }
}

export interface FormatDecimalOptions {
  /** Maximum (and by default minimum) fraction digits. */
  dp: number
  /** Minimum fraction digits (defaults to `dp`). */
  minDp?: number
  /** Thousands separators. Default true. */
  group?: boolean
  /** Prefix "+" on positive values. */
  signed?: boolean
}

export interface DecimalParts {
  sign: '' | '-' | '+'
  int: string
  frac: string
}

/** Round and split into sign / grouped integer / fraction. */
export function decimalParts(d: Dec, opts: FormatDecimalOptions): DecimalParts {
  const { dp, minDp = dp, group = true, signed = false } = opts
  const r = roundDec(d, dp)
  const neg = r.coef < 0n
  const digits = (neg ? -r.coef : r.coef).toString().padStart(r.scale + 1, '0')
  let int = digits.slice(0, digits.length - r.scale)
  let frac = r.scale > 0 ? digits.slice(digits.length - r.scale) : ''
  // Trim optional fraction zeros down to minDp, then pad up to minDp.
  while (frac.length > minDp && frac.endsWith('0')) frac = frac.slice(0, -1)
  frac = frac.padEnd(minDp, '0')
  if (group) int = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const isZero = r.coef === 0n
  const sign: DecimalParts['sign'] = isZero ? '' : neg ? '-' : signed ? '+' : ''
  return { sign, int, frac }
}

/** Format a decimal string; null when unavailable/unparseable. */
export function formatDecimal(
  input: string | null | undefined,
  opts: FormatDecimalOptions,
): string | null {
  const d = parseDec(input)
  if (!d) return null
  const { sign, int, frac } = decimalParts(d, opts)
  return `${sign}${int}${frac ? `.${frac}` : ''}`
}

/** Float conversion — ONLY for chart plotting. */
export function toNumber(input: string | null | undefined): number | null {
  const d = parseDec(input)
  if (!d) return null
  return Number(decToString(d))
}
