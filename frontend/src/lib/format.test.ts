import { describe, expect, it } from 'vitest'
import { fmtMs } from './format'

describe('fmtMs', () => {
  it('keeps sub-10 ms stage timings readable', () => {
    expect(fmtMs(0.024)).toBe('0.02 ms')
    expect(fmtMs(3.47)).toBe('3.47 ms')
  })

  it('rounds larger values and switches units', () => {
    expect(fmtMs(182.4)).toBe('182 ms')
    expect(fmtMs(1836)).toBe('1.84 s')
    expect(fmtMs(null)).toBeNull()
  })
})
