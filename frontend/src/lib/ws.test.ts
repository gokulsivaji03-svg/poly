import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { backoffDelay, buildWsUrl, RealtimeSocket, type ConnStatus, type WebSocketLike, type WsMessage } from './ws'

class FakeSocket implements WebSocketLike {
  static instances: FakeSocket[] = []
  readyState = 0
  sent: string[] = []
  closedWith: number | undefined
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null

  readonly url: string
  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close(code = 1000) {
    this.closedWith = code
    this.readyState = 3
  }
  // --- test drivers
  open() {
    this.readyState = 1
    this.onopen?.({})
  }
  message(msg: WsMessage) {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
  serverClose(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

const BACKOFF = { baseMs: 1000, maxMs: 8000, jitter: 0 }

function setup(overrides: Partial<ConstructorParameters<typeof RealtimeSocket>[0]> = {}) {
  const messages: WsMessage[] = []
  const statuses: ConnStatus[] = []
  const onAuthFailure = vi.fn()
  const sock = new RealtimeSocket({
    url: () => 'ws://test/api/ws?token=t',
    onMessage: (m) => messages.push(m),
    onStatus: (s) => statuses.push(s),
    onAuthFailure,
    createSocket: (url) => new FakeSocket(url),
    backoff: BACKOFF,
    random: () => 0,
    heartbeatTimeoutMs: 40_000,
    pingIntervalMs: 20_000,
    connectTimeoutMs: 10_000,
    ...overrides,
  })
  const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1]!
  return { sock, messages, statuses, onAuthFailure, latest }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  FakeSocket.instances = []
})

afterEach(() => {
  vi.useRealTimers()
})

describe('backoffDelay', () => {
  it('grows exponentially and caps at max', () => {
    const opts = { baseMs: 1000, maxMs: 30_000, jitter: 0 }
    expect([0, 1, 2, 3, 4, 5, 6].map((a) => backoffDelay(a, opts))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000])
  })

  it('applies jitter within [delay·(1−jitter), delay]', () => {
    const opts = { baseMs: 1000, maxMs: 30_000, jitter: 0.5 }
    expect(backoffDelay(2, opts, () => 0)).toBe(4000)
    expect(backoffDelay(2, opts, () => 1)).toBe(2000)
    expect(backoffDelay(2, opts, () => 0.5)).toBe(3000)
  })
})

describe('RealtimeSocket', () => {
  it('connects, reports connected and forwards messages (dropping pong)', () => {
    const { sock, messages, statuses, latest } = setup()
    sock.start()
    expect(statuses.at(-1)?.state).toBe('connecting')
    latest().open()
    expect(sock.getStatus()).toMatchObject({ state: 'connected', attempt: 0, everConnected: true })
    latest().message({ type: 'snapshot', data: { dashboard: {} } })
    latest().message({ type: 'pong' })
    expect(messages.map((m) => m.type)).toEqual(['snapshot'])
  })

  it('reconnects with exponential backoff and resets after a successful open', () => {
    const { sock, latest } = setup()
    sock.start()
    latest().serverClose()
    expect(sock.getStatus()).toMatchObject({ state: 'disconnected', attempt: 1, nextRetryAt: Date.now() + 1000 })

    vi.advanceTimersByTime(999)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(2)
    expect(sock.getStatus().state).toBe('connecting')

    latest().serverClose()
    expect(sock.getStatus().attempt).toBe(2)
    vi.advanceTimersByTime(1999)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(3)

    latest().serverClose()
    vi.advanceTimersByTime(4000)
    latest().serverClose()
    vi.advanceTimersByTime(8000)
    latest().serverClose()
    // capped at maxMs (8000)
    expect(sock.getStatus().nextRetryAt).toBe(Date.now() + 8000)
    vi.advanceTimersByTime(8000)

    latest().open()
    expect(sock.getStatus()).toMatchObject({ state: 'connected', attempt: 0 })
    latest().serverClose()
    expect(sock.getStatus().nextRetryAt).toBe(Date.now() + 1000)
  })

  it('treats heartbeat silence as a dead connection', () => {
    const { sock, latest } = setup()
    sock.start()
    const first = latest()
    first.open()
    vi.advanceTimersByTime(30_000)
    first.message({ type: 'heartbeat', data: {} }) // resets the watchdog
    vi.advanceTimersByTime(39_999)
    expect(sock.getStatus().state).toBe('connected')
    vi.advanceTimersByTime(1)
    expect(first.closedWith).toBe(1000)
    expect(sock.getStatus()).toMatchObject({ state: 'disconnected', attempt: 1 })
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('gives up on a connection attempt that never opens', () => {
    const { sock } = setup()
    sock.start()
    vi.advanceTimersByTime(10_000)
    expect(sock.getStatus()).toMatchObject({ state: 'disconnected', attempt: 1 })
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('sends ping frames while open', () => {
    const { sock, latest } = setup()
    sock.start()
    latest().open()
    vi.advanceTimersByTime(20_000)
    latest().message({ type: 'pong' })
    vi.advanceTimersByTime(20_000)
    expect(latest().sent).toEqual(['{"type":"ping"}', '{"type":"ping"}'])
  })

  it('ignores events from a superseded socket', () => {
    const { sock, messages, latest } = setup()
    sock.start()
    const stale = latest()
    stale.serverClose()
    vi.advanceTimersByTime(1000)
    stale.message({ type: 'source_trade', data: { id: 1 } })
    expect(messages).toHaveLength(0)
  })

  it('stop() closes the socket and cancels pending reconnects', () => {
    const { sock, latest } = setup()
    sock.start()
    latest().serverClose()
    sock.stop()
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(1)
    expect(sock.getStatus().state).toBe('disconnected')
  })

  it('reconnectNow() skips the remaining backoff', () => {
    const { sock, latest } = setup()
    sock.start()
    latest().serverClose()
    sock.reconnectNow()
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('reports auth-flavoured closes and keeps retrying', () => {
    const { sock, onAuthFailure, latest } = setup()
    sock.start()
    latest().serverClose(1008)
    expect(onAuthFailure).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('stays disconnected without a credential', () => {
    const { sock } = setup({ url: () => null })
    sock.start()
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(0)
    expect(sock.getStatus().state).toBe('disconnected')
  })
})

describe('buildWsUrl', () => {
  it('derives ws/wss from the page protocol and encodes the token', () => {
    expect(buildWsUrl('a b', { protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/api/ws?token=a%20b')
    expect(buildWsUrl('t', { protocol: 'https:', host: 'x.io' })).toBe('wss://x.io/api/ws?token=t')
  })
})
