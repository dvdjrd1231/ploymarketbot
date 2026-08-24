import { useState } from 'react'
import {
  useConnection, useEngineState, useFeeds, useSettings,
} from '../lib/live'
import { useNow } from '../lib/clock'
import { duration, shortAddress } from '../lib/format'
import { api } from '../lib/api'
import { Pill } from './common'

/**
 * Top bar: identity, every health signal that matters, and Start/Stop.
 *
 * The three feed pills answer the question the old build could not: *is what
 * I'm looking at actually live?* Server = this browser's socket, Prices = the
 * Polymarket book stream, Exchange = the authenticated order channel.
 */
export function Header({ onError }: { onError: (message: string) => void }) {
  const state = useEngineState()
  const conn = useConnection()
  const feeds = useFeeds()
  const settings = useSettings()
  const now = useNow()
  const [busy, setBusy] = useState(false)

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await action()
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const engineTone =
    state.state === 'running' ? (state.connected ? 'up' : 'warn')
      : state.state === 'error' ? 'down'
        : state.state === 'connecting' || state.state === 'stopping' ? 'warn'
          : 'neutral'

  const priceState = feeds.prices?.state ?? 'idle'
  const priceTone =
    priceState === 'live' ? 'up'
      : priceState === 'connecting' || priceState === 'retrying' ? 'warn'
        : priceState === 'off' ? 'info' : 'neutral'

  const userState = feeds.user?.state ?? 'off'
  const userTone =
    userState === 'live' ? 'up'
      : userState === 'connecting' || userState === 'retrying' ? 'warn'
        : 'neutral'

  const mode = state.paper ? 'PAPER' : state.dryRun ? 'DRY-RUN' : 'LIVE'
  const modeTone = state.paper ? 'info' : state.dryRun ? 'warn' : 'down'

  return (
    <header className="header">
      <div className="brand">
        <div className="brand-mark">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
            stroke="#34d399" strokeWidth="2.4" strokeLinecap="round"
            strokeLinejoin="round">
            <path d="M4 17l5-6 4 4 6-8" />
          </svg>
        </div>
        <div>
          <div className="brand-title">Polymarket Copy Trader</div>
          <div className="brand-sub">
            {state.modeLabel}
            {state.targetWallet ? ` · following ${shortAddress(state.targetWallet)}` : ''}
          </div>
        </div>
      </div>

      <div className="header-spacer" />

      <div className="header-meta">
        <Pill tone={modeTone as any}>{mode}</Pill>

        <Pill tone={conn.status === 'open' ? 'up' : 'down'}
          live={conn.status === 'open'}>
          Server
          {conn.status === 'open' && conn.latency > 0
            ? ` ${conn.latency}ms`
            : conn.status === 'open' ? '' : ' offline'}
        </Pill>

        <Pill tone={priceTone as any} live={priceState === 'live'}>
          Prices {priceState === 'live'
            ? `${feeds.prices.subscribed ?? 0} live`
            : priceState}
        </Pill>

        {!state.paper && (
          <Pill tone={userTone as any} live={userState === 'live'}>
            Exchange {userState}
          </Pill>
        )}

        <Pill tone={engineTone as any} live={state.state === 'running'}>
          {state.state === 'running'
            ? `Running · ${duration((now - state.startedTs * 1000) / 1000)}`
            : state.detail || state.state}
        </Pill>
      </div>

      <div className="header-actions">
        {state.running || state.state === 'connecting' ? (
          <button className="btn danger" disabled={busy}
            onClick={() => run(api.stop)}>
            ■ Stop
          </button>
        ) : (
          <button className="btn primary"
            disabled={busy || !settings?.target_wallet}
            title={!settings?.target_wallet
              ? 'Set a target wallet in Settings first'
              : 'Start copying'}
            onClick={() => run(api.start)}>
            ▶ Start
          </button>
        )}
      </div>
    </header>
  )
}
