import { describe, expect, it } from 'vitest'
import { HEDGE_ADVANCED_FIELDS, HEDGE_THRESHOLD_FIELDS, PAPER_FIELDS, RISK_FIELDS, SIZING_FIELDS, toApi, toDisplay, validate } from './schema'

const fields = [...SIZING_FIELDS, ...RISK_FIELDS, ...HEDGE_THRESHOLD_FIELDS, ...HEDGE_ADVANCED_FIELDS, ...PAPER_FIELDS]
const field = (section: string, key: string) => fields.find((f) => f.section === section && f.key === key)!

describe('settings backend contract', () => {
  it('uses JSON integers for integer backend fields', () => {
    for (const [section, key] of [
      ['risk', 'max_detection_delay_s'], ['risk', 'max_book_age_s'], ['risk', 'max_buys_per_market'],
      ['hedge', 'scan_interval_s'], ['paper', 'execution_delay_ms'], ['paper', 'extra_slippage_bps'],
    ]) {
      const f = field(section!, key!)
      expect(f.kind).toBe('int')
      expect(toDisplay(f, 12)).toBe('12')
      expect(toApi(f, '12')).toBe(12)
      expect(validate(f, '12.5')).not.toBeNull()
      expect(validate(f, '9007199254740993')).not.toBeNull()
    }
  })

  it('enforces positive values only where the backend requires them', () => {
    for (const f of fields.filter((f) => f.positive)) expect(validate(f, '0')).toBe('Must be > 0')
    expect(validate(field('sizing', 'min_source_confidence'), '0')).toBeNull()
    expect(validate(field('paper', 'execution_delay_ms'), '0')).toBeNull()
    expect(validate(field('hedge', 'min_hedge_shares'), '0')).toBeNull()
  })

  it('requires a finite positive maximum hedge quantity instead of sending null', () => {
    const f = field('hedge', 'max_hedge_shares')
    expect(validate(f, '')).toBe('Required')
    expect(validate(f, '0')).toBe('Must be > 0')
    expect(toApi(f, '100000')).toBe('100000')
  })

  it('mirrors exclusive hedge price and paper execution bounds', () => {
    expect(validate(field('hedge', 'max_hedge_price'), '100')).toBe('Must be < 100')
    expect(validate(field('hedge', 'max_hedge_price'), '99.999')).toBeNull()
    expect(validate(field('paper', 'execution_delay_ms'), '60001')).toBe('Must be ≤ 60000')
    expect(validate(field('paper', 'extra_slippage_bps'), '1001')).toBe('Must be ≤ 1000')
    expect(validate(field('sizing', 'multiplier'), '10.1')).toBe('Must be ≤ 10')
  })

  it('keeps percent and cents conversions exact', () => {
    expect(toApi(field('sizing', 'fixed_pct_of_user'), '14.3')).toBe('0.143')
    expect(toApi(field('hedge', 'max_hedge_price'), '99.123456789012345678')).toBe('0.99123456789012345678')
  })
})
