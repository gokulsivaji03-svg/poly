import { describe, expect, it } from 'vitest'
import {
  addDecimal,
  compareDecimal,
  formatDecimal,
  fractionToPercent,
  normalizeDecimal,
  parseDec,
  percentToFraction,
  shiftDecimal,
  signDecimal,
  toNumber,
} from './decimal'
import { fmtBps, fmtInt, fmtNum, fmtPct, fmtPrice, fmtShares, fmtUsd } from './format'

describe('parse / normalize', () => {
  it('normalizes plain and exponent notation', () => {
    expect(normalizeDecimal('012.3400')).toBe('12.34')
    expect(normalizeDecimal('12.000000')).toBe('12')
    expect(normalizeDecimal('.5')).toBe('0.5')
    expect(normalizeDecimal('5.')).toBe('5')
    expect(normalizeDecimal('1E-7')).toBe('0.0000001')
    expect(normalizeDecimal('2.5e3')).toBe('2500')
    expect(normalizeDecimal('-0.000')).toBe('0')
    expect(normalizeDecimal('+3.10')).toBe('3.1')
  })

  it('rejects non-decimals and treats null as unavailable', () => {
    for (const bad of ['', ' ', 'abc', '1,000', '1.2.3', '.', '-', 'NaN', 'Infinity', '0x10']) {
      expect(parseDec(bad)).toBeNull()
    }
    expect(parseDec(null)).toBeNull()
    expect(parseDec(undefined)).toBeNull()
    expect(normalizeDecimal(null)).toBeNull()
  })
})

describe('exact arithmetic (no float artefacts)', () => {
  it('adds without binary rounding errors', () => {
    expect(0.1 + 0.2).not.toBe(0.3) // the artefact we avoid
    expect(addDecimal('0.1', '0.2')).toBe('0.3')
    expect(addDecimal('1.005', '-0.005')).toBe('1')
    expect(addDecimal('-12.5', '2.25')).toBe('-10.25')
    expect(addDecimal('123456789012345678.123456', '0.000001')).toBe('123456789012345678.123457')
  })

  it('propagates unavailability instead of treating null as zero', () => {
    expect(addDecimal('1', null)).toBeNull()
    expect(addDecimal(undefined, '1')).toBeNull()
    expect(addDecimal('x', '1')).toBeNull()
  })

  it('shifts the decimal point exactly', () => {
    expect(shiftDecimal('1.23', 2)).toBe('123')
    expect(shiftDecimal('1.23', -4)).toBe('0.000123')
    expect(shiftDecimal('-0.5', 1)).toBe('-5')
    expect(shiftDecimal(null, 2)).toBeNull()
  })

  it('compares and signs exactly', () => {
    expect(compareDecimal('10', '9')).toBe(1)
    expect(compareDecimal('0.30', '0.3')).toBe(0)
    expect(compareDecimal('-1', '0')).toBe(-1)
    expect(compareDecimal(null, '0')).toBeNull()
    expect(signDecimal('-0.0001')).toBe(-1)
    expect(signDecimal('0.000')).toBe(0)
    expect(signDecimal(null)).toBeNull()
  })

  it('only converts to float for plotting', () => {
    expect(toNumber('12.5')).toBe(12.5)
    expect(toNumber(null)).toBeNull()
  })
})

describe('percent <-> fraction conversion', () => {
  it('converts API fractions to UI percent exactly', () => {
    expect(0.07 * 100).not.toBe(7)
    expect(fractionToPercent('0.07')).toBe('7')
    expect(0.29 * 100).not.toBe(29)
    expect(fractionToPercent('0.29')).toBe('29')
    expect(fractionToPercent('0.02')).toBe('2')
    expect(fractionToPercent('1')).toBe('100')
    expect(fractionToPercent('0.0001')).toBe('0.01')
    expect(fractionToPercent('1E-7')).toBe('0.00001')
  })

  it('converts UI percent to API fraction strings exactly', () => {
    expect(14.3 / 100).not.toBe(0.143)
    expect(percentToFraction('14.3')).toBe('0.143')
    expect(1.1 / 100).not.toBe(0.011)
    expect(percentToFraction('1.1')).toBe('0.011')
    expect(percentToFraction('2')).toBe('0.02')
    expect(percentToFraction('0')).toBe('0')
    expect(percentToFraction('100')).toBe('1')
    expect(percentToFraction('0.005')).toBe('0.00005')
  })

  it('round-trips', () => {
    for (const f of ['0.07', '0.143', '0.333333', '0.000001', '1.5']) {
      expect(percentToFraction(fractionToPercent(f))).toBe(f)
    }
  })

  it('returns null for unavailable or invalid input', () => {
    expect(fractionToPercent(null)).toBeNull()
    expect(percentToFraction('')).toBeNull()
    expect(percentToFraction('12%')).toBeNull()
  })
})

describe('formatting', () => {
  it('rounds half away from zero on the string, not a float', () => {
    expect((1.005).toFixed(2)).toBe('1.00') // float artefact
    expect(formatDecimal('1.005', { dp: 2 })).toBe('1.01')
    expect(formatDecimal('-1.005', { dp: 2 })).toBe('-1.01')
    expect(formatDecimal('2.675', { dp: 2 })).toBe('2.68')
    expect(formatDecimal('0.999', { dp: 2 })).toBe('1.00')
  })

  it('never prints negative zero', () => {
    expect(formatDecimal('-0.004', { dp: 2 })).toBe('0.00')
    expect(fmtUsd('-0.001', { signed: true })).toBe('$0.00')
  })

  it('formats currency, shares, prices, percent and bps', () => {
    expect(fmtUsd('1234567.891')).toBe('$1,234,567.89')
    expect(fmtUsd('-1234.5')).toBe('-$1,234.50')
    expect(fmtUsd('12.3', { signed: true })).toBe('+$12.30')
    expect(fmtUsd('1234567', { compact: true })).toBe('$1.2M')
    expect(fmtUsd('45210', { compact: true })).toBe('$45.2K')
    expect(fmtShares('1500.256')).toBe('1,500.26')
    expect(fmtPrice('0.523')).toBe('0.523')
    expect(fmtPrice('0.523', { cents: true })).toBe('52.3¢')
    expect(fmtPrice('0.5', { cents: true })).toBe('50.0¢')
    expect(fmtPct('0.0215')).toBe('2.15%')
    expect(fmtPct('0.07', { dp: 1 })).toBe('7.0%')
    expect(fmtPct('-0.005', { signed: true })).toBe('-0.50%')
    expect(fmtBps('12.34')).toBe('+12.3 bps')
    expect(fmtInt(4100)).toBe('4,100')
    expect(fmtNum('0.62')).toBe('0.62')
  })

  it('returns null (unavailable) rather than a zero for null input', () => {
    expect(fmtUsd(null)).toBeNull()
    expect(fmtPct(undefined)).toBeNull()
    expect(fmtPrice(null, { cents: true })).toBeNull()
    expect(fmtBps(null)).toBeNull()
    expect(fmtShares('garbage')).toBeNull()
  })
})
