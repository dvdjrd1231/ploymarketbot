import { memo, useState } from 'react'
import { useEngineState, usePositions } from '../lib/live'
import { useNow } from '../lib/clock'
import { api } from '../lib/api'
import {
  cents, countdown, money, moneySigned, pct, price, shares,
} from '../lib/format'
import { Empty, OutcomeBadge, Pnl, TickCell } from './common'
import type { Position } from '../types'

/**
 * One open position.
 *
 * Memoised on the fields that actually change so a price tick on market A does
 * not repaint the row for market B — with a dozen positions and a busy book
 * that is the whole difference between smooth and janky.
 */
const Row = memo(function Row({
  position, now, canClose, onClose,
}: {
  position: Position
  now: number
  canClose: boolean
  onClose: (p: Position) => void
}) {
  const pnl = (position.currentPrice - position.entryPrice) * position.size
  const returnPct = position.cost ? (pnl / position.cost) * 100 : 0
  // The entry-price rule: our fill must never be worse than the target's entry.
  const entryOk = !position.targetPrice
    || position.entryPrice <= position.targetPrice + 1e-9
  const stale = position.priceTs > 0 && now / 1000 - position.priceTs > 15

  return (
    <tr>
      <td className="market-cell">
        <span className="market-name" title={position.question}>
          {position.question || position.tokenId.slice(0, 28)}
        </span>
        <span className="market-meta">
          <OutcomeBadge outcome={position.outcome} />
          <span>opened {new Date(position.openedTs * 1000)
            .toLocaleTimeString('en-GB', { hour12: false })}</span>
          {position.syncNote && (
            <span className="warn-text" title={position.syncNote}>⚠ sync</span>
          )}
        </span>
      </td>
      <td className="num" title={entryOk
        ? 'Filled at or better than the target entry ✓'
        : 'Fill was worse than the target entry'}>
        <span className={entryOk ? 'up-text' : 'down-text'}>
          {price(position.entryPrice)}
        </span>
      </td>
      <td className="num dim-text">{price(position.targetPrice)}</td>
      <td className="num">
        <span className={`source-dot ${position.priceSource}`}
          title={`Mark from ${position.priceSource === 'stream'
            ? 'the live Polymarket order-book stream'
            : position.priceSource === 'rest' ? 'a REST price poll'
              : 'the last known price'}`} />
        <TickCell
          value={position.currentPrice}
          format={cents}
          className={stale ? 'stale' : ''}
        />
      </td>
      <td className="num">{shares(position.size)}</td>
      <td className="num">{money(position.cost)}</td>
      <td className="num">
        <TickCell value={position.marketValue} format={money} />
      </td>
      <td className="num">
        <Pnl value={pnl} format={moneySigned} />
      </td>
      <td className="num">
        <Pnl value={returnPct} format={(v) => pct(v)} />
      </td>
      <td className="num warn-text">{countdown(position.marketEndTs, now)}</td>
      <td style={{ textAlign: 'right' }}>
        <button
          className="btn small ghost"
          disabled={!canClose}
          title={canClose
            ? 'Sell this position now at the best bid'
            : 'Start the bot to manage positions'}
          onClick={() => onClose(position)}
        >
          Close
        </button>
      </td>
    </tr>
  )
}, (prev, next) =>
  prev.position.currentPrice === next.position.currentPrice
  && prev.position.size === next.position.size
  && prev.position.marketValue === next.position.marketValue
  && prev.position.syncNote === next.position.syncNote
  && prev.position.priceSource === next.position.priceSource
  && prev.position.priceTs === next.position.priceTs
  && prev.canClose === next.canClose
  // Countdown cells need the clock, but only to second resolution.
  && Math.floor(prev.now / 1000) === Math.floor(next.now / 1000),
)

export function PositionsTable({
  onError, onInfo,
}: {
  onError: (message: string) => void
  onInfo: (message: string) => void
}) {
  const positions = usePositions()
  const state = useEngineState()
  const now = useNow()
  const [closing, setClosing] = useState<number | null>(null)

  const close = async (position: Position) => {
    const label = position.question || position.tokenId.slice(0, 24)
    if (!window.confirm(
      `Close this position now?\n\n${label}\n\n` +
      `${shares(position.size)} shares will be sold at the best available bid.`
    )) return
    setClosing(position.id)
    try {
      await api.closePosition(position.id)
      onInfo('Close order submitted.')
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setClosing(null)
    }
  }

  if (positions.length === 0) {
    return (
      <Empty
        title="No open positions"
        hint={state.running
          ? 'The bot is watching the target wallet. A position appears here the moment a qualifying trade is copied.'
          : 'Press Start to begin copying. Positions, live prices and P/L will stream in here.'}
      />
    )
  }

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Market</th>
            <th className="num">My entry</th>
            <th className="num">Target @</th>
            <th className="num">Live price</th>
            <th className="num">Shares</th>
            <th className="num">Cost</th>
            <th className="num">Value</th>
            <th className="num">Open P/L</th>
            <th className="num">Return</th>
            <th className="num">Settles in</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <Row
              key={position.id}
              position={position}
              now={now}
              canClose={state.running && closing !== position.id}
              onClose={close}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}
