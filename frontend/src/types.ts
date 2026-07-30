export interface EngineState {
  state: 'idle' | 'connecting' | 'running' | 'stopping' | 'stopped' | 'error'
  detail: string
  connected: boolean
  running: boolean
  mode: string
  modeLabel: string
  paper: boolean
  dryRun: boolean
  targetWallet: string
  address: string
  startedTs: number
  uptime: number
}

export interface Account {
  address: string
  balance: number
  positionValue: number
  buyingPower: number
  equity: number
  connected: boolean
  paper: boolean
  accountLabel: string
  updatedTs: number
}

export interface Position {
  id: number
  targetTradeId: string
  marketId: string
  tokenId: string
  outcome: string
  question: string
  side: string
  mode: string
  targetPrice: number
  targetUsdc: number
  entryPrice: number
  size: number
  cost: number
  status: string
  orderId: string
  reason: string
  openedTs: number
  closedTs: number | null
  marketEndTs: number | null
  realizedPnl: number
  currentPrice: number
  marketValue: number
  unrealizedPnl: number
  priceSource: string
  priceTs: number
  syncNote: string
}

export interface Order {
  id: number
  orderId: string
  tokenId: string
  marketId: string
  question: string
  outcome: string
  side: string
  price: number
  size: number
  filledSize: number
  remaining: number
  status: string
  error: string
  tradeId: number | null
  createdTs: number
  updatedTs: number
  pending: boolean
}

export interface Stats {
  open: number
  closed: number
  skipped: number
  failed: number
  realizedPnlToday: number
  realizedPnlTotal: number
  settledTotal: number
  winsTotal: number
  winRate: number
  openPositions: number
  unrealizedPnl: number
  openCost: number
  openValue: number
  openReturnPct: number
  portfolioPnl: number
  targetAccountValue: number | null
}

export interface LogLine {
  ts: number
  level: string
  message: string
}

export interface FeedHealth {
  state: string
  error: string
  tracked?: number
  subscribed?: number
  markets?: number
  lastMessageTs: number
  messages?: number
  reconnects: number
  ageSeconds?: number | null
}

export interface Feeds {
  prices: FeedHealth
  user: FeedHealth
  books: Array<{
    tokenId: string
    bid: number | null
    ask: number | null
    mid: number | null
    spread: number | null
    lastTrade: number | null
    updated: number
    source: string
  }>
}

export interface Settings {
  target_wallet: string
  my_wallet_address: string
  trading_mode: string
  signature_type: number
  funder_address: string
  poll_interval_seconds: number
  clob_host: string
  gamma_host: string
  data_api_host: string
  clob_ws_host: string
  chain_id: number
  max_copy_fraction_of_target: number
  min_order_usdc: number
  dry_run: boolean
  paper_trading: boolean
  paper_starting_balance: number
  restore_previous_session: boolean
  max_expiry_days: number
  copy_exits: boolean
  min_seconds_to_expiry: number
  push_interval_ms: number
  balance_refresh_seconds: number
  market_refresh_seconds: number
  reconcile_seconds: number
  use_price_stream: boolean
  rest_price_fallback_seconds: number
  hasPrivateKey: boolean
  secureStorage: boolean
  storageBackend: string
}

export interface TargetTrade {
  tradeId: string
  marketId: string
  tokenId: string
  outcome: string
  side: string
  price: number
  size: number
  usdcSize: number
  timestamp: number
  question: string
  slug: string
}

export interface Connection {
  status: 'connecting' | 'open' | 'closed'
  attempts: number
  lastMessage: number
  latency: number
}
