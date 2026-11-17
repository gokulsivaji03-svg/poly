import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StageLatency } from './stage-latency'

const stages = [
  { stage: 'taped', n: 200, p50_ms: 1.41, p99_ms: 3.6 },
  { stage: 'decided', n: 200, p50_ms: 0.02, p99_ms: 0.04 },
  { stage: 'executed', n: 200, p50_ms: 3.52, p99_ms: 9.1 },
  { stage: 'total', n: 200, p50_ms: 9.9, p99_ms: 22.5 },
]

describe('StageLatency', () => {
  it('lists each stage then the receipt-to-booked total', () => {
    render(<StageLatency stages={stages} />)
    const table = screen.getByRole('table', { name: 'Copy latency by stage' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.textContent)).toEqual([
      'taped1.41 ms3.60 ms',
      'decided0.02 ms0.04 ms',
      'executed3.52 ms9.10 ms',
      'total9.90 ms23 ms',
    ])
    expect(screen.getByText('last 200 copies')).toBeInTheDocument()
  })

  it('explains an empty pipeline instead of showing zeros', () => {
    render(<StageLatency stages={[]} />)
    expect(screen.getByText('No timed copies yet')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })
})
