/**
 * Live data store.
 *
 * A single WebSocket feeds a plain external store that React reads through
 * `useSyncExternalStore`. Two properties matter for keeping the UI fluid:
 *
 *   - **Slice-level subscriptions.** A positions frame notifies only the
 *     components that read `positions`; the settings form, the log and the
 *     header do not re-render. With a busy book that is the difference between
 *     a smooth dashboard and a stuttering one.
 *   - **The socket lives outside React.** Reconnects, backoff and buffering are
 *     not tied to a component's lifecycle, so a re-render never drops the feed
 *     and a dropped feed never unmounts anything.
 */

import { useCallback, useSyncExternalStore } from 'react'
import type {
  Account, Connection, EngineState, Feeds, LogLine, Order, Position, Settings,
  Stats,
} from '../types'

export type SliceName =
  | 'state' | 'settings' | 'account' | 'positions' | 'orders' | 'history'
  | 'stats' | 'feeds' | 'logs' | 'conn' | 'equity'

/** Server-side equity sample; `t` is unix seconds. */
export interface EquitySample { t: number; equity: number; pnl: number }

const EMPTY_POSITIONS: Position[] = []
const EMPTY_ORDERS: Order[] = []
const EMPTY_LOGS: LogLine[] = []
const EMPTY_EQUITY: EquitySample[] = []

const DEFAULT_STATE: EngineState = {
  state: 'idle', detail: 'Connecting to the local server…', connected: false,
  running: false, mode: 'fixed_50', modeLabel: 'Fixed 50%', paper: false,
  dryRun: true, targetWallet: '', address: '', startedTs: 0, uptime: 0,
}

const DEFAULT_ACCOUNT: Account = {
  address: '', balance: 0, positionValue: 0, buyingPower: 0, equity: 0,
  connected: false, paper: false, accountLabel: '', updatedTs: 0,
}

const DEFAULT_STATS: Stats = {
  open: 0, closed: 0, skipped: 0, failed: 0, realizedPnlToday: 0,
  realizedPnlTotal: 0, settledTotal: 0, winsTotal: 0, winRate: 0,
  openPositions: 0, unrealizedPnl: 0, openCost: 0, openValue: 0,
  openReturnPct: 0, portfolioPnl: 0, targetAccountValue: null,
}

const DEFAULT_FEEDS: Feeds = {
  prices: { state: 'idle', error: '', lastMessageTs: 0, reconnects: 0 },
  user: { state: 'off', error: '', lastMessageTs: 0, reconnects: 0 },
  books: [],
}

const DEFAULT_CONN: Connection = {
  status: 'connecting', attempts: 0, lastMessage: 0, latency: 0,
}

const store: Record<SliceName, unknown> = {
  state: DEFAULT_STATE,
  settings: null,
  account: DEFAULT_ACCOUNT,
  positions: EMPTY_POSITIONS,
  orders: EMPTY_ORDERS,
  history: EMPTY_POSITIONS,
  stats: DEFAULT_STATS,
  feeds: DEFAULT_FEEDS,
  logs: EMPTY_LOGS,
  conn: DEFAULT_CONN,
  equity: EMPTY_EQUITY,
}

const listeners = new Map<SliceName, Set<() => void>>()

function emit(slice: SliceName) {
  const set = listeners.get(slice)
  if (set) for (const fn of set) fn()
}

function setSlice(slice: SliceName, value: unknown) {
  store[slice] = value
  emit(slice)
}

function subscribe(slice: SliceName, cb: () => void) {
  let set = listeners.get(slice)
  if (!set) {
    set = new Set()
    listeners.set(slice, set)
  }
  set.add(cb)
  return () => { set!.delete(cb) }
}

export function useSlice<T>(slice: SliceName): T {
  return useSyncExternalStore(
    useCallback((cb: () => void) => subscribe(slice, cb), [slice]),
    useCallback(() => store[slice] as T, [slice]),
  )
}

export const useEngineState = () => useSlice<EngineState>('state')
export const useAccount = () => useSlice<Account>('account')
export const usePositions = () => useSlice<Position[]>('positions')
export const useHistory = () => useSlice<Position[]>('history')
export const useOrders = () => useSlice<Order[]>('orders')
export const useStats = () => useSlice<Stats>('stats')
export const useFeeds = () => useSlice<Feeds>('feeds')
export const useLogs = () => useSlice<LogLine[]>('logs')
export const useConnection = () => useSlice<Connection>('conn')
export const useSettings = () => useSlice<Settings | null>('settings')
export const useEquity = () => useSlice<EquitySample[]>('equity')

// --- log buffer -------------------------------------------------------------

const MAX_LOGS = 600

function appendLog(line: LogLine) {
  const current = store.logs as LogLine[]
  const next = current.length >= MAX_LOGS
    ? current.slice(current.length - MAX_LOGS + 1).concat(line)
    : current.concat(line)
  setSlice('logs', next)
}

// --- socket -----------------------------------------------------------------

type Frame = { type: string; data: unknown; ts: number }

let socket: WebSocket | null = null
let attempts = 0
let reconnectTimer: number | undefined
let pingTimer: number | undefined
let lastPingSent = 0

function setConn(patch: Partial<Connection>) {
  setSlice('conn', { ...(store.conn as Connection), ...patch })
}

function applySnapshot(data: any) {
  if (!data) return
  if (data.state) setSlice('state', data.state)
  if (data.settings) setSlice('settings', data.settings)
  if (data.account) setSlice('account', data.account)
  setSlice('positions', data.positions ?? EMPTY_POSITIONS)
  setSlice('history', data.history ?? EMPTY_POSITIONS)
  setSlice('orders', data.orders ?? EMPTY_ORDERS)
  if (data.stats) setSlice('stats', data.stats)
  if (data.feeds) setSlice('feeds', data.feeds)
  setSlice('equity', data.equity ?? EMPTY_EQUITY)
  setSlice('logs', (data.logs ?? EMPTY_LOGS).slice(-MAX_LOGS))
}

function handle(frame: Frame) {
  setConn({ lastMessage: Date.now() })
  switch (frame.type) {
    case 'snapshot':
      applySnapshot(frame.data)
      break
    case 'state':
    case 'settings':
    case 'account':
    case 'positions':
    case 'orders':
    case 'history':
    case 'stats':
    case 'feeds':
    case 'equity':
      setSlice(frame.type as SliceName, frame.data)
      break
    case 'log':
      appendLog(frame.data as LogLine)
      break
    case 'logs:reset':
      setSlice('logs', EMPTY_LOGS)
      setSlice('equity', EMPTY_EQUITY)
      break
    case 'pong':
      if (lastPingSent) setConn({ latency: Date.now() - lastPingSent })
      break
    default:
      break
  }
}

export function connectLive() {
  if (socket && (socket.readyState === WebSocket.OPEN ||
                 socket.readyState === WebSocket.CONNECTING)) return

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  setConn({ status: 'connecting' })

  try {
    socket = new WebSocket(`${proto}//${location.host}/api/stream`)
  } catch {
    scheduleReconnect()
    return
  }

  socket.onopen = () => {
    attempts = 0
    setConn({ status: 'open', attempts: 0 })
    window.clearInterval(pingTimer)
    // A periodic ping gives the server's reader loop something to see, which is
    // how a half-open socket gets noticed instead of silently going stale.
    pingTimer = window.setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) {
        lastPingSent = Date.now()
        socket.send('ping')
      }
    }, 15000)
  }

  socket.onmessage = (event) => {
    try {
      handle(JSON.parse(event.data as string))
    } catch {
      /* ignore malformed frames rather than tearing down the feed */
    }
  }

  socket.onerror = () => { /* onclose does the recovery */ }

  socket.onclose = () => {
    window.clearInterval(pingTimer)
    socket = null
    setConn({ status: 'closed' })
    scheduleReconnect()
  }
}

function scheduleReconnect() {
  window.clearTimeout(reconnectTimer)
  attempts += 1
  setConn({ attempts })
  const delay = Math.min(500 * 2 ** Math.min(attempts, 5), 8000)
  reconnectTimer = window.setTimeout(connectLive, delay)
}

// Reconnect immediately when the tab comes back or the network returns, rather
// than waiting out a backoff the user can see.
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      attempts = 0
      connectLive()
    }
  })
  window.addEventListener('online', () => { attempts = 0; connectLive() })
}
