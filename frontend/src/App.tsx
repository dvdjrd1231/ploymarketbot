import { useCallback, useEffect, useState } from 'react'
import { connectLive, useConnection, useOrders, usePositions, useHistory } from './lib/live'
import { Header } from './components/Header'
import { StatCards } from './components/StatCards'
import { Hero } from './components/Hero'
import { PositionsTable } from './components/PositionsTable'
import { OrdersTable } from './components/OrdersTable'
import { HistoryTable } from './components/HistoryTable'
import { ActivityLog } from './components/ActivityLog'
import { TargetPanel } from './components/TargetPanel'
import { SettingsPanel } from './components/SettingsPanel'
import { Notice } from './components/common'

type Tab = 'positions' | 'orders' | 'history' | 'target' | 'log' | 'settings'

interface Toast {
  id: number
  tone: 'error' | 'success'
  message: string
}

let toastId = 0

export default function App() {
  const [tab, setTab] = useState<Tab>('positions')
  const [toasts, setToasts] = useState<Toast[]>([])
  const conn = useConnection()
  const positions = usePositions()
  const orders = useOrders()
  const history = useHistory()

  useEffect(() => { connectLive() }, [])

  const pushToast = useCallback((tone: Toast['tone'], message: string) => {
    const id = ++toastId
    setToasts((current) => [...current.slice(-3), { id, tone, message }])
    window.setTimeout(
      () => setToasts((current) => current.filter((t) => t.id !== id)),
      tone === 'error' ? 9000 : 4500,
    )
  }, [])

  const onError = useCallback((m: string) => pushToast('error', m), [pushToast])
  const onInfo = useCallback((m: string) => pushToast('success', m), [pushToast])

  const pendingOrders = orders.filter((o) => o.pending).length

  const TABS: Array<{ key: Tab; label: string; count?: number }> = [
    { key: 'positions', label: 'Positions', count: positions.length },
    { key: 'orders', label: 'Orders', count: pendingOrders || undefined },
    { key: 'history', label: 'History', count: history.length || undefined },
    { key: 'target', label: 'Target wallet' },
    { key: 'log', label: 'Activity log' },
    { key: 'settings', label: 'Settings' },
  ]

  return (
    <div className="app">
      <Header onError={onError} />

      <div className="app-body">
        {conn.status !== 'open' && (
          <Notice tone="warn">
            {conn.status === 'connecting' && conn.attempts === 0
              ? 'Connecting to the local server…'
              : `Lost connection to the local server — reconnecting (attempt ${conn.attempts}). `
                + 'Trading continues on the server; this page will catch up automatically.'}
          </Notice>
        )}

        <StatCards />
        <Hero />

        <section className="panel content-panel">
          <div className="tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                className={`tab${tab === t.key ? ' active' : ''}`}
                onClick={() => setTab(t.key)}
              >
                {t.label}
                {t.count !== undefined && t.count > 0 && (
                  <span className="tab-count">{t.count}</span>
                )}
              </button>
            ))}
          </div>

          <div className="content-scroll">
            {tab === 'positions' && (
              <PositionsTable onError={onError} onInfo={onInfo} />
            )}
            {tab === 'orders' && <OrdersTable onError={onError} onInfo={onInfo} />}
            {tab === 'history' && <HistoryTable />}
            {tab === 'target' && <TargetPanel onError={onError} />}
            {tab === 'log' && <ActivityLog onError={onError} />}
            {tab === 'settings' && (
              <SettingsPanel onError={onError} onInfo={onInfo} />
            )}
          </div>
        </section>
      </div>

      <div className="toasts">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.tone}`}>
            <span className={toast.tone === 'error' ? 'down-text' : 'up-text'}>
              {toast.tone === 'error' ? '⚠' : '✓'}
            </span>
            <span className="grow">{toast.message}</span>
            <button
              className="toast-close"
              onClick={() => setToasts((c) => c.filter((t) => t.id !== toast.id))}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
