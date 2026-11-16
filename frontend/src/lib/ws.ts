/**
 * Realtime WebSocket client: exponential backoff with jitter, heartbeat watchdog and ping.
 * Framework-agnostic so the reconnect logic is unit-testable with fake timers.
 */

export type ConnState = 'connecting' | 'connected' | 'disconnected'

export interface ConnStatus {
  state: ConnState
  /** Consecutive failed attempts since the last successful open. */
  attempt: number
  /** Epoch ms of the next scheduled reconnect, when waiting. */
  nextRetryAt: number | null
  /** True once a connection has ever been established in this session. */
  everConnected: boolean
}

export interface WsMessage {
  type: string
  data?: unknown
  ts?: string
}

export interface BackoffOptions {
  baseMs: number
  maxMs: number
  /** Fraction of the delay randomized away (0 = none, 1 = full jitter). */
  jitter: number
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 1000, maxMs: 30_000, jitter: 0.5 }

/**
 * Delay before reconnect attempt `attempt` (0-based): min(max, base·2^attempt), with the top
 * `jitter` fraction randomized so many clients don't reconnect in lockstep.
 */
export function backoffDelay(attempt: number, opts: BackoffOptions = DEFAULT_BACKOFF, random: () => number = Math.random): number {
  const exp = Math.min(opts.maxMs, opts.baseMs * 2 ** Math.max(0, attempt))
  return Math.round(exp * (1 - opts.jitter * random()))
}

/** Minimal surface of the browser WebSocket we depend on (lets tests inject a fake). */
export interface WebSocketLike {
  readonly readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code: number; reason?: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
}

const CONNECTING = 0
const OPEN = 1

/** Close codes a server typically uses for an invalid/expired token. */
const AUTH_CLOSE_CODES = new Set([1008, 4001, 4003, 4401, 4403])

export interface RealtimeSocketOptions {
  /** Returns the URL to connect to, or null when there is no credential (stays disconnected). */
  url: () => string | null
  onMessage: (msg: WsMessage) => void
  onStatus: (status: ConnStatus) => void
  /** Invoked when the server closes with an auth-related code. */
  onAuthFailure?: () => void
  createSocket?: (url: string) => WebSocketLike
  backoff?: BackoffOptions
  random?: () => number
  /** No inbound frame (incl. heartbeat/pong) within this window => connection presumed dead. */
  heartbeatTimeoutMs?: number
  pingIntervalMs?: number
  connectTimeoutMs?: number
  now?: () => number
}

export class RealtimeSocket {
  private readonly opts: Required<Omit<RealtimeSocketOptions, 'onAuthFailure'>> & Pick<RealtimeSocketOptions, 'onAuthFailure'>
  private ws: WebSocketLike | null = null
  private status: ConnStatus = { state: 'disconnected', attempt: 0, nextRetryAt: null, everConnected: false }
  private retryTimer: number | undefined
  private watchdogTimer: number | undefined
  private pingTimer: number | undefined
  private running = false

  constructor(options: RealtimeSocketOptions) {
    this.opts = {
      createSocket: (url) => new WebSocket(url) as unknown as WebSocketLike,
      backoff: DEFAULT_BACKOFF,
      random: Math.random,
      heartbeatTimeoutMs: 40_000,
      pingIntervalMs: 20_000,
      connectTimeoutMs: 10_000,
      now: Date.now,
      ...options,
    }
  }

  getStatus(): ConnStatus {
    return this.status
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.connect()
  }

  stop(): void {
    this.running = false
    this.clearTimers()
    this.dropSocket()
    this.setStatus({ state: 'disconnected', nextRetryAt: null })
  }

  /** Skip the remaining backoff wait and try immediately. */
  reconnectNow(): void {
    if (!this.running || this.status.state !== 'disconnected') return
    this.clearRetry()
    this.connect()
  }

  private connect(): void {
    if (!this.running) return
    const url = this.opts.url()
    if (!url) {
      this.setStatus({ state: 'disconnected', nextRetryAt: null })
      return
    }
    this.setStatus({ state: 'connecting', nextRetryAt: null })
    let sock: WebSocketLike
    try {
      sock = this.opts.createSocket(url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = sock
    this.armWatchdog(this.opts.connectTimeoutMs)

    sock.onopen = () => {
      if (sock !== this.ws) return
      this.setStatus({ state: 'connected', attempt: 0, nextRetryAt: null, everConnected: true })
      this.armWatchdog(this.opts.heartbeatTimeoutMs)
      this.pingTimer = window.setInterval(() => {
        if (sock === this.ws && sock.readyState === OPEN) sock.send(JSON.stringify({ type: 'ping' }))
      }, this.opts.pingIntervalMs)
    }
    sock.onmessage = (ev) => {
      if (sock !== this.ws) return
      this.armWatchdog(this.opts.heartbeatTimeoutMs)
      let msg: WsMessage
      try {
        msg = JSON.parse(String(ev.data)) as WsMessage
      } catch {
        return
      }
      if (!msg || typeof msg.type !== 'string' || msg.type === 'pong') return
      this.opts.onMessage(msg)
    }
    sock.onerror = () => {
      // A close event always follows; reconnect is handled there.
    }
    sock.onclose = (ev) => {
      if (sock !== this.ws) return
      this.ws = null
      this.clearTimers()
      if (!this.running) return
      if (AUTH_CLOSE_CODES.has(ev.code)) this.opts.onAuthFailure?.()
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (!this.running) return
    const delay = backoffDelay(this.status.attempt, this.opts.backoff, this.opts.random)
    this.setStatus({ state: 'disconnected', attempt: this.status.attempt + 1, nextRetryAt: this.opts.now() + delay })
    this.clearRetry()
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = undefined
      this.connect()
    }, delay)
  }

  /** Heartbeat/connect watchdog: silence beyond the window tears the socket down. */
  private armWatchdog(ms: number): void {
    clearTimeout(this.watchdogTimer)
    this.watchdogTimer = window.setTimeout(() => {
      this.watchdogTimer = undefined
      this.clearTimers()
      this.dropSocket()
      this.scheduleReconnect()
    }, ms)
  }

  private dropSocket(): void {
    const sock = this.ws
    this.ws = null
    if (!sock) return
    sock.onmessage = sock.onerror = sock.onclose = null
    if (sock.readyState === CONNECTING) {
      // Closing a CONNECTING socket makes browsers log a warning; close it once it opens instead.
      sock.onopen = () => sock.close(1000, 'client reset')
      return
    }
    sock.onopen = null
    try {
      sock.close(1000, 'client reset')
    } catch {
      // already closed
    }
  }

  private clearRetry(): void {
    clearTimeout(this.retryTimer)
    this.retryTimer = undefined
  }

  private clearTimers(): void {
    this.clearRetry()
    clearTimeout(this.watchdogTimer)
    this.watchdogTimer = undefined
    clearInterval(this.pingTimer)
    this.pingTimer = undefined
  }

  private setStatus(patch: Partial<ConnStatus>): void {
    this.status = { ...this.status, ...patch }
    this.opts.onStatus(this.status)
  }
}

export function buildWsUrl(token: string, loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc.host}/api/ws?token=${encodeURIComponent(token)}`
}
