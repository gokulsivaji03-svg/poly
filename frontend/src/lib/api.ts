/**
 * Typed REST client mirroring the polycopy HTTP API contract.
 *
 * Conventions: money/shares/prices/percent fields are decimal strings (`Decimal`); `null` means
 * unavailable. Percentages are fractions ("0.02" = 2%).
 */
import { getToken } from './auth'

export type Decimal = string
export type Iso = string

// ---------------------------------------------------------------------------------------------
// Auth & health

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
  expires_at: Iso
}

export interface Me {
  username: string
}

export type WorkerState = 'running' | 'stopped' | 'error' | 'standby'

export interface WorkerStatus {
  state: WorkerState
  last_run_at: Iso | null
  last_error: string | null
}

export interface Ready {
  ready: boolean
  database: boolean
  leader: boolean
  workers: Record<string, WorkerStatus>
}

// ---------------------------------------------------------------------------------------------
// Dashboard & portfolio

export type Mode = 'PAPER' | 'REPLAY' | 'LIVE'
export type HedgeMode = 'ALERT_ONLY' | 'AUTO_PAPER'

export interface PortfolioSummary {
  account_id: string | number
  as_of: Iso
  starting_bankroll: Decimal
  equity: Decimal | null
  cash: Decimal | null
  reserved: Decimal | null
  available_cash: Decimal | null
  invested: Decimal | null
  positions_value: Decimal | null
  realized_pnl: Decimal | null
  unrealized_pnl: Decimal | null
  locked_profit: Decimal | null
  fees_paid: Decimal | null
  unmarked_positions: number
  day_pnl: Decimal | null
}

export interface Dashboard {
  portfolio: PortfolioSummary
  followed_wallets: number
  trades_copied_today: number
  mode: Mode
  strategies_paused: boolean
  emergency_stop: boolean
  copy_enabled: boolean
  hedge_enabled: boolean
  hedge_mode: HedgeMode
}

export type EquityRange = '1d' | '7d' | '30d' | 'all'

export interface EquityPoint {
  t: Iso
  equity: Decimal | null
  cash: Decimal | null
  positions_value: Decimal | null
  realized_pnl: Decimal | null
  unrealized_pnl: Decimal | null
  locked_profit: Decimal | null
}

export type HedgeState =
  | 'UNHEDGED'
  | 'HEDGE_OPPORTUNITY'
  | 'HEDGE_PENDING'
  | 'PARTIALLY_HEDGED'
  | 'FULLY_HEDGED'
  | 'HEDGE_FAILED'
  | 'SETTLED'

export type LotOrigin = 'COPY' | 'HEDGE'

export interface Lot {
  id: string | number
  origin: LotOrigin
  source_wallet: string | null
  entry_price: Decimal
  shares_acquired: Decimal
  shares_remaining: Decimal
  matched_shares: Decimal
  cost_remaining: Decimal
  fee_usdc: Decimal
  opened_at: Iso
}

export interface Position {
  condition_id: string
  token_id: string
  opposite_token_id: string | null
  question: string
  outcome: string
  shares: Decimal
  matched_shares: Decimal
  unmatched_shares: Decimal
  hedged_pct: Decimal | null
  avg_entry: Decimal | null
  cost_basis: Decimal | null
  mark: Decimal | null
  executable_bid: Decimal | null
  market_value: Decimal | null
  unrealized_pnl: Decimal | null
  locked_profit: Decimal | null
  hedge_state: HedgeState
  origins: LotOrigin[]
  lots: Lot[]
}

export interface Fill {
  gross_shares: Decimal
  net_shares: Decimal
  notional: Decimal
  fee_usdc: Decimal
  cash_delta: Decimal
  avg_price: Decimal | null
  worst_price: Decimal | null
}

export interface Order {
  id: string | number
  kind: string
  side: 'BUY' | 'SELL'
  status: string
  token_id: string
  condition_id: string
  requested_budget: Decimal | null
  requested_shares: Decimal | null
  limit_price: Decimal | null
  reason: string | null
  created_at: Iso
  completed_at: Iso | null
  exec_latency_ms: number | null
  book_age_ms: number | null
  fill: Fill | null
}

// ---------------------------------------------------------------------------------------------
// Wallets

export type SizingMode = 'PROPORTIONAL' | 'FIXED_AMOUNT' | 'FIXED_PCT_OF_USER' | 'SOURCE_MULTIPLE'
export type EquityCoverage = 'POSITIONS_AND_CASH' | 'POSITIONS_ONLY' | 'UNAVAILABLE'

export type SizingOverride = Partial<SizingSettings>

export interface WalletSummary {
  id: number | string
  address: string
  nickname: string | null
  follow: boolean
  added_at: Iso
  sizing_override: SizingOverride | null
  estimated_equity: Decimal | null
  equity_coverage: EquityCoverage
  equity_is_lower_bound: boolean
  equity_confidence: Decimal | null
  equity_as_of: Iso | null
  positions_value: Decimal | null
  cash_balance: Decimal | null
  open_positions: number | null
  observed_realized_pnl: Decimal | null
  observed_unrealized_pnl: Decimal | null
  last_trade_at: Iso | null
  last_success_at: Iso | null
  last_error: string | null
  freshness_s: number | null
  ingestion_delay_ms: number | null
  trades_7d: number
}

export interface WalletMetrics {
  trade_count: number
  buy_count: number
  sell_count: number
  trades_per_day: Decimal | null
  avg_trade_usd: Decimal | null
  avg_position_usd: Decimal | null
  copied_count: number
  skipped_count: number
  copy_rate: Decimal | null
  avg_price_diff_bps: Decimal | null
  median_detection_lag_ms: number | null
  skip_reasons: Record<string, number>
}

export interface WalletEquityPoint {
  t: Iso
  equity: Decimal | null
  positions_value: Decimal | null
  cash_balance: Decimal | null
  coverage: EquityCoverage
  confidence: Decimal | null
}

export interface AllocationPoint {
  t: Iso
  source_alloc_pct: Decimal | null
  source_cost: Decimal | null
  source_equity: Decimal | null
  signal_status: SignalStatus | string | null
}

export interface SourcePosition {
  token_id: string
  condition_id: string
  title: string
  outcome: string
  size: Decimal
  avg_price: Decimal | null
  current_price: Decimal | null
  current_value: Decimal | null
  realized_pnl: Decimal | null
  unrealized_pnl: Decimal | null
}

export interface WalletDetail extends WalletSummary {
  metrics: WalletMetrics
  equity_history: WalletEquityPoint[]
  allocation_history: AllocationPoint[]
  positions: SourcePosition[]
}

export interface AddWalletBody {
  address: string
  nickname?: string
  follow?: boolean
}

export interface PatchWalletBody {
  nickname?: string
  follow?: boolean
  sizing_override?: SizingOverride | null
}

// ---------------------------------------------------------------------------------------------
// Feeds

export type SignalStatus = 'PENDING' | 'SKIPPED' | 'REJECTED' | 'EXECUTED' | 'PARTIAL' | 'FAILED'

export interface SourceTrade {
  id: number | string
  wallet_id: number | string
  wallet_address: string
  wallet_nickname: string | null
  type: string
  side: 'BUY' | 'SELL' | null
  condition_id: string | null
  token_id: string | null
  outcome: string | null
  title: string | null
  slug: string | null
  size: Decimal | null
  usdc_size: Decimal | null
  price: Decimal | null
  tx_hash: string | null
  block_ts: Iso | null
  detected_at: Iso
  detection_lag_ms: number | null
  is_backfill: boolean
  signal_id: number | null
  signal_status: string | null
}

export interface Reason {
  code: string
  message: string
}

export interface CopySignal {
  id: number | string
  source_event_id: number | string
  wallet_id: number | string
  wallet_nickname: string | null
  wallet_address: string
  status: SignalStatus
  side: 'BUY' | 'SELL' | null
  title: string | null
  outcome: string | null
  token_id: string | null
  condition_id: string | null
  source_price: Decimal | null
  source_shares: Decimal | null
  source_cost: Decimal | null
  source_equity: Decimal | null
  source_equity_coverage: EquityCoverage | string | null
  source_confidence: Decimal | null
  source_alloc_pct: Decimal | null
  user_equity: Decimal | null
  proposed_pct: Decimal | null
  target_budget: Decimal | null
  approved_budget: Decimal | null
  limit_price: Decimal | null
  filled_shares: Decimal | null
  filled_cost: Decimal | null
  avg_fill_price: Decimal | null
  price_diff_bps: Decimal | null
  /** Price per share paid beyond the source's fill (negative: better than the source). */
  premium: Decimal | null
  /** Each pipeline stage's own duration in this copy, plus `total`. */
  stages_ms: Record<string, number> | null
  reasons: Reason[]
  detected_at: Iso | null
  decided_at: Iso | null
  executed_at: Iso | null
  detection_lag_ms: number | null
  signal_latency_ms: number | null
  exec_latency_ms: number | null
  order_id: number | string | null
}

export interface CopyQuality {
  hours: number
  signals: number
  filled: number
  skipped: number
  failed: number
  premium_p50: Decimal | null
  premium_p90: Decimal | null
  /** Share of fills at or better than the source's price, 0 to 1. */
  at_source_share: Decimal | null
  detect_to_fill_ms_p50: number | null
  detect_to_fill_ms_p99: number | null
  skip_reasons: { code: string; n: number }[]
}

// ---------------------------------------------------------------------------------------------
// Hedging

export interface HedgeScanner {
  enabled: boolean
  mode: HedgeMode
  last_scan_at: Iso | null
  scan_duration_ms: number | null
  positions_scanned: number | null
}

export interface HedgeOpportunity {
  id: string
  condition_id: string
  question: string
  primary_token_id: string
  primary_outcome: string
  hedge_token_id: string | null
  hedge_outcome: string | null
  state: HedgeState
  viable: boolean
  reasons: string[]
  existing_shares: Decimal
  unhedged_shares: Decimal
  existing_entry_price: Decimal | null
  hedge_best_ask: Decimal | null
  hedge_avg_price: Decimal | null
  hedge_worst_price: Decimal | null
  combined_cost_per_share: Decimal | null
  matched_shares: Decimal | null
  net_profit_per_share: Decimal | null
  net_profit: Decimal | null
  gross_profit: Decimal | null
  primary_fees: Decimal | null
  hedge_fees: Decimal | null
  capital_required: Decimal | null
  expected_return: Decimal | null
  depth_usd: Decimal | null
  quoted_at: Iso | null
  book_age_ms: number | null
}

export interface HedgeOpportunities {
  scanner: HedgeScanner
  opportunities: HedgeOpportunity[]
}

export interface HedgeExecuteResult {
  status: string
  order_id: number | string | null
  matched_shares: Decimal | null
  reason: string | null
  /** Hedge state of the position after the attempt. */
  state: HedgeState | null
}

export interface HedgeMergeResult {
  /** USDC released by the simulated CTF merge of matched pairs. */
  proceeds: Decimal | null
}

export interface HedgeEvent {
  id: number | string
  condition_id: string
  primary_token_id: string | null
  kind: string
  from_state: HedgeState | string | null
  to_state: HedgeState | string | null
  detail: unknown
  created_at: Iso
}

// ---------------------------------------------------------------------------------------------
// Settings

export interface SizingSettings {
  mode: SizingMode
  multiplier: Decimal
  fixed_amount_usd: Decimal
  fixed_pct_of_user: Decimal
  source_multiple: Decimal
  unknown_equity_policy: 'SKIP' | 'FALLBACK_FIXED'
  lower_bound_equity_policy: 'USE_CAPPED' | 'SKIP'
  min_source_confidence: Decimal
  /** Each followed wallet sizes from, and may hold at most, an equal slice of equity. */
  split_across_wallets: boolean
}

/** A null limit is switched off. */
export interface RiskSettings {
  max_pct_per_trade: Decimal | null
  max_position_usd: Decimal | null
  max_market_exposure_usd: Decimal | null
  max_total_exposure_pct: Decimal | null
  min_liquidity_usd: Decimal
  max_price_diff_pct: Decimal
  max_slippage_pct: Decimal
  max_detection_delay_s: number
  min_cash_reserve_usd: Decimal
  daily_loss_limit_usd: Decimal | null
  max_book_age_s: number
  min_order_usd: Decimal
  max_entry_price: Decimal
  max_buys_per_market: number | null
}

export interface CopySettings {
  enabled: boolean
  sell_policy: 'PROPORTIONAL' | 'FULL_EXIT' | 'IGNORE'
  hedged_sell_policy: 'PRESERVE_HEDGE' | 'UNWIND_IF_BETTER'
  copy_sells: boolean
}

export interface HedgeSettings {
  enabled: boolean
  mode: HedgeMode
  min_net_gap_per_share: Decimal
  min_abs_profit_usd: Decimal
  min_return_pct: Decimal
  min_liquidity_usd: Decimal
  max_hedge_price: Decimal
  min_hedge_shares: Decimal
  max_hedge_shares: Decimal
  full_hedge_only: boolean
  other_cost_per_share: Decimal
  allow_neg_risk: boolean
  scan_interval_s: number
}

export interface PaperSettings {
  starting_bankroll_usd: Decimal
  execution_delay_ms: number
  buy_fee_mode: 'shares' | 'cash'
  share_step: Decimal
  extra_slippage_bps: number
}

export interface StrategySettings {
  sizing: SizingSettings
  risk: RiskSettings
  copy: CopySettings
  hedge: HedgeSettings
  paper: PaperSettings
  strategies_paused: boolean
  emergency_stop: boolean
}

export type SettingsSection = 'sizing' | 'risk' | 'copy' | 'hedge' | 'paper'

export type SettingsPatch = {
  [K in SettingsSection]?: Partial<StrategySettings[K]>
} & { strategies_paused?: boolean; emergency_stop?: boolean }

export interface SettingsEnvelope {
  version: number
  settings: StrategySettings
}

// ---------------------------------------------------------------------------------------------
// System

export interface Venue {
  id: string
  name: string
  live_supported: boolean
  live_enabled: boolean
  reason: string | null
  geoblock: { blocked: boolean; country: string | null; region: string | null; checked_at: Iso | null } | null
}

export interface SystemMetrics {
  source_detection_lag_ms_p50: number | null
  source_detection_lag_ms_p95: number | null
  data_api_lag_s: number | string | null
  signal_latency_ms_p50: number | null
  exec_latency_ms_p50: number | null
  book_age_ms_p50: number | null
  ws_connected: boolean | null
  ws_reconnects: number | null
  chain_stream_connected: boolean | null
  chain_stream_reconnects: number | null
  mempool_stream_connected: boolean | null
  mempool_stream_reconnects: number | null
  mempool_streams_up: number | null
  mempool_streams_total: number | null
  chain_detection_lag_s: number | null
  http_errors_5m: number | null
  last_poll_at: Iso | null
  copy_stages: CopyStage[]
}

export interface CopyStage {
  stage: string
  n: number
  p50_ms: number
  p99_ms: number
}

export interface SystemHealth {
  mode: Mode
  venues: Venue[]
  workers: Record<string, WorkerStatus>
  metrics: SystemMetrics
  ledger_ok: boolean
  ledger_problems: string[]
}

export interface SystemEvent {
  id: number | string
  level: string
  kind: string
  message: string
  data: unknown
  created_at: Iso
}

// ---------------------------------------------------------------------------------------------
// Historical replay

export type ReplayStatus = 'RUNNING' | 'COMPLETED' | 'FAILED'

export interface ReplayParams {
  start: Iso
  end: Iso
  wallet_ids: number[]
  starting_bankroll: Decimal
  /** Integer seconds, from 0 through 3600. */
  detection_delay_s?: number
  /** `history` fetches activity and CLOB minute bars; `recorded` uses only local recordings. */
  prices?: ReplayPrices
  /** `source` mode: each copy pays the source price plus a uniform 0 to this (dollars). */
  premium_max?: Decimal
  settings?: SettingsPatch
}

export type ReplayPrices = 'history' | 'source' | 'recorded'

export interface ReplaySummary {
  final_equity: Decimal | null
  starting_bankroll: Decimal | null
  return_pct: Decimal | null
  realized_pnl: Decimal | null
  unrealized_pnl: Decimal | null
  locked_profit: Decimal | null
  signals: Partial<Record<SignalStatus, number>>
  events_replayed: number | null
  snapshots_used: number | null
  /** Absent on runs from before history replays. */
  prices?: ReplayPrices
  minute_bars_used?: number
  activity_fetched?: number
  /** Wallets with no equity snapshot before the window, sized from their earliest one. */
  equity_backfilled?: number
  ledger_ok: boolean
}

export interface ReplayRun {
  id: number | string
  status: ReplayStatus
  params: ReplayParams
  created_at: Iso
  finished_at: Iso | null
  error: string | null
  summary: ReplaySummary | null
}

export interface ReplaySignal {
  t: Iso
  wallet: string | null
  side: 'BUY' | 'SELL' | null
  title: string | null
  outcome: string | null
  status: SignalStatus | string
  source_price: Decimal | null
  avg_fill_price: Decimal | null
  filled_cost: Decimal | null
  reasons: string[]
}

export interface ReplayDetail extends ReplayRun {
  equity_curve: { t: Iso; equity: Decimal | null }[]
  signals: ReplaySignal[]
}

// ---------------------------------------------------------------------------------------------
// Transport

export const API_BASE = '/api'

/** FastAPI-style validation item. */
interface ValidationItem {
  loc?: (string | number)[]
  msg?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  /**
   * Field errors keyed by dotted path with transport prefixes stripped
   * (["body","settings","risk","max_pct_per_trade"] -> "risk.max_pct_per_trade").
   */
  get fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {}
    if (!Array.isArray(this.detail)) return out
    for (const item of this.detail as ValidationItem[]) {
      if (!item?.loc) continue
      const path = item.loc.filter((p) => p !== 'body' && p !== 'settings').join('.')
      if (path) out[path] = item.msg ?? 'invalid'
    }
    return out
  }
}

function messageFromDetail(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const first = detail[0] as ValidationItem | undefined
    if (first?.msg) return first.loc ? `${first.loc.slice(1).join('.')}: ${first.msg}` : first.msg
  }
  if (status === 0) return 'Network error — backend unreachable'
  return `Request failed (${status})`
}

let unauthorizedHandler: (() => void) | null = null

/** Registered by the auth layer: invoked on any 401 response. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

export async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, null, messageFromDetail(0, null))
  }

  if (res.status === 204) return undefined as T
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!res.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data
    if (res.status === 401 && path !== '/auth/login') unauthorizedHandler?.()
    throw new ApiError(res.status, detail, messageFromDetail(res.status, detail))
  }
  return data as T
}

const qs = (params: Record<string, string | number | undefined | null>): string => {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  const s = sp.toString()
  return s ? `?${s}` : ''
}

const enc = encodeURIComponent

export const api = {
  login: (username: string, password: string) => request<LoginResponse>('POST', '/auth/login', { username, password }),
  me: () => request<Me>('GET', '/auth/me'),
  ready: () => request<Ready>('GET', '/ready'),

  dashboard: () => request<Dashboard>('GET', '/dashboard'),
  equityHistory: (range: EquityRange) =>
    request<{ points: EquityPoint[] }>('GET', `/portfolio/equity-history${qs({ range })}`),
  positions: () => request<{ positions: Position[] }>('GET', '/positions'),
  orders: (limit = 100) => request<{ orders: Order[] }>('GET', `/orders${qs({ limit })}`),

  wallets: () => request<{ wallets: WalletSummary[] }>('GET', '/wallets'),
  wallet: (id: string) => request<WalletDetail>('GET', `/wallets/${enc(id)}`),
  walletTrades: (id: string, limit = 100, beforeId?: string | number) =>
    request<{ trades: SourceTrade[] }>('GET', `/wallets/${enc(id)}/trades${qs({ limit, before_id: beforeId })}`),
  addWallet: (body: AddWalletBody) => request<WalletSummary>('POST', '/wallets', body),
  patchWallet: (id: string | number, body: PatchWalletBody) =>
    request<WalletSummary>('PATCH', `/wallets/${enc(String(id))}`, body),
  deleteWallet: (id: string | number) => request<void>('DELETE', `/wallets/${enc(String(id))}`),

  sourceTrades: (limit = 50, walletId?: string | number) =>
    request<{ trades: SourceTrade[] }>('GET', `/source-trades${qs({ limit, wallet_id: walletId })}`),
  copySignals: (limit = 50) => request<{ signals: CopySignal[] }>('GET', `/copy-signals${qs({ limit })}`),
  copyQuality: (hours = 24) => request<CopyQuality>('GET', `/copy-quality${qs({ hours })}`),

  hedgeOpportunities: () => request<HedgeOpportunities>('GET', '/hedges/opportunities'),
  /** Opportunity ids are "<condition_id>:<primary_token_id>" — always URL-encoded. */
  executeHedge: (id: string) => request<HedgeExecuteResult>('POST', `/hedges/${enc(id)}/execute`),
  mergeHedge: (id: string) => request<HedgeMergeResult>('POST', `/hedges/${enc(id)}/merge`),
  hedgeEvents: (limit = 100) => request<{ events: HedgeEvent[] }>('GET', `/hedges/events${qs({ limit })}`),

  settings: () => request<SettingsEnvelope>('GET', '/settings'),
  putSettings: (settings: SettingsPatch, expectedVersion?: number) =>
    request<SettingsEnvelope>('PUT', '/settings', { settings, expected_version: expectedVersion }),
  pause: () => request<SettingsEnvelope>('POST', '/control/pause'),
  resume: () => request<SettingsEnvelope>('POST', '/control/resume'),
  emergencyStop: () => request<SettingsEnvelope>('POST', '/control/emergency-stop'),
  clearEmergency: () => request<SettingsEnvelope>('POST', '/control/clear-emergency'),
  resetPaper: (startingBankroll: Decimal) =>
    request<PortfolioSummary>('POST', '/control/reset-paper', { starting_bankroll: startingBankroll }),

  systemHealth: () => request<SystemHealth>('GET', '/system/health'),
  systemEvents: (limit = 100) => request<{ events: SystemEvent[] }>('GET', `/system/events${qs({ limit })}`),

  startReplay: (params: ReplayParams) => request<{ id: number | string; status: ReplayStatus }>('POST', '/replay', params),
  replayRuns: () => request<{ runs: ReplayRun[] }>('GET', '/replay'),
  replayRun: (id: string) => request<ReplayDetail>('GET', `/replay/${enc(id)}`),
}
