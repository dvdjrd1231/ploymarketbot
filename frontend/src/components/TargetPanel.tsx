import { useCallback, useEffect, useState } from 'react'
import { useEngineState, useFeeds, useStats } from '../lib/live'
import { useNow } from '../lib/clock'
import { api } from '../lib/api'
import {
  ageLabel, dateTime, money, price, shares, shortAddress,
} from '../lib/format'
import { Empty, OutcomeBadge, Notice } from './common'
import type { TargetTrade } from '../types'

/**
 * What the followed wallet is doing, plus live feed diagnostics.
 *
 * This panel is the answer to "why didn't it copy that?" — you can see the
 * target's trade, its price, and cross-check it against the log entry that
 * explains the decision.
 */
export function TargetPanel({ onError }: { onError: (m: string) => void }) {
  const state = useEngineState()
  const stats = useStats()
  const feeds = useFeeds()
  const now = useNow()
  const [trades, setTrades] = useState<TargetTrade[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)

  const load = useCallback(async () => {
    if (!state.targetWallet) return
    setLoading(true)
    try {
      setTrades(await api.targetActivity(40))
      setLoaded(true)
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setLoading(false)
    }
  }, [state.targetWallet, onError])

  useEffect(() => { void load() }, [load])

  // This is reference data, not a live feed — a 30s refresh is plenty and keeps
  // the Data API request budget for the engine's own polling.
  useEffect(() => {
    const timer = window.setInterval(() => { void load() }, 30000)
    return () => window.clearInterval(timer)
  }, [load])

  if (!state.targetWallet) {
    return (
      <Empty
        title="No target wallet set"
        hint="Add the wallet address you want to follow in Settings, then start the bot."
      />
    )
  }

  const priceFeed = feeds.prices
  const userFeed = feeds.user

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="row">
        <div className="kv">
          <span className="kv-key">Following</span>
          <span className="kv-value" title={state.targetWallet}>
            {shortAddress(state.targetWallet)}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Their portfolio</span>
          <span className="kv-value">
            {stats.targetAccountValue !== null
              ? money(stats.targetAccountValue)
              : '—'}
          </span>
        </div>
        <div className="grow" />
        <button className="btn small ghost" onClick={() => void load()}
          disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <div className="row" style={{ gap: 18 }}>
        <div className="kv">
          <span className="kv-key">Price stream</span>
          <span className="kv-value">
            <span className={priceFeed.state === 'live' ? 'up-text' : 'warn-text'}>
              {priceFeed.state}
            </span>
            {priceFeed.state === 'live' && (
              <span className="faint-text">
                {' '}· {priceFeed.subscribed ?? 0} books ·{' '}
                {priceFeed.messages ?? 0} msgs ·{' '}
                {ageLabel(priceFeed.lastMessageTs, now)}
              </span>
            )}
            {priceFeed.reconnects > 0 && (
              <span className="faint-text"> · {priceFeed.reconnects} reconnects</span>
            )}
          </span>
        </div>
        {!state.paper && (
          <div className="kv">
            <span className="kv-key">Order stream</span>
            <span className="kv-value">
              <span className={userFeed.state === 'live' ? 'up-text' : 'dim-text'}>
                {userFeed.state}
              </span>
            </span>
          </div>
        )}
      </div>

      {priceFeed.error && (
        <Notice tone="warn">
          Price stream last error: {priceFeed.error}. Prices are falling back to
          REST polling, so they stay correct but refresh a little slower.
        </Notice>
      )}

      {trades.length === 0 ? (
        <Empty
          title={loaded ? 'No recent trades on this wallet' : 'Loading activity…'}
          hint={loaded
            ? 'Nothing has traded here recently. The bot only copies activity that happens after you press Start.'
            : undefined}
        />
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th className="num">Price</th>
                <th className="num">Shares</th>
                <th className="num">Notional</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((trade) => (
                <tr key={trade.tradeId}>
                  <td className="market-cell">
                    <span className="market-name" title={trade.question}>
                      {trade.question || trade.tokenId.slice(0, 28)}
                    </span>
                    <span className="market-meta">
                      <OutcomeBadge outcome={trade.outcome} />
                    </span>
                  </td>
                  <td>
                    <span className={trade.side === 'BUY' ? 'up-text' : 'down-text'}
                      style={{ fontWeight: 600 }}>
                      {trade.side}
                    </span>
                  </td>
                  <td className="num">{price(trade.price)}</td>
                  <td className="num">{shares(trade.size)}</td>
                  <td className="num">{money(trade.usdcSize)}</td>
                  <td className="tiny faint-text nowrap">
                    {dateTime(trade.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
