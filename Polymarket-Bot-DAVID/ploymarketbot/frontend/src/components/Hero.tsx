import { useMemo } from 'react'
import { useEquity, usePositions, useStats } from '../lib/live'
import { useNow } from '../lib/clock'
import { countdownFull, money, moneySigned } from '../lib/format'

/**
 * Countdown to the soonest-settling open position.
 *
 * It ticks off a locally-aligned one-second clock against the absolute end
 * timestamp the server sent, so it stays exact whether or not an update has
 * arrived recently.
 */
function Countdown() {
  const positions = usePositions()
  const now = useNow()

  const soonest = useMemo(() => {
    let best: (typeof positions)[number] | null = null
    for (const p of positions) {
      if (!p.marketEndTs) continue
      if (!best || p.marketEndTs < best.marketEndTs!) best = p
    }
    return best
  }, [positions])

  const { text, urgent } = countdownFull(soonest?.marketEndTs, now)

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="panel-title">Time until market expiration</span>
        {positions.length > 1 && (
          <span className="tiny faint-text" style={{ marginLeft: 'auto' }}>
            soonest of {positions.length}
          </span>
        )}
      </div>
      <div className="panel-body">
        <div className={`countdown-value${urgent ? ' urgent' : ''}`}>{text}</div>
        <div className="countdown-market">
          {soonest
            ? soonest.question || soonest.tokenId.slice(0, 24)
            : 'No open position with a known settle time.'}
        </div>
      </div>
    </div>
  )
}

/** Portfolio equity sparkline, sampled client-side from the pushed frames. */
function EquityCurve() {
  const samples = useEquity()
  const stats = useStats()

  const path = useMemo(() => {
    if (samples.length < 2) return null
    const values = samples.map((s) => s.equity)
    const min = Math.min(...values)
    const max = Math.max(...values)
    const w = 100
    const h = 100
    const step = w / (samples.length - 1)
    // A flat balance should read as a flat line through the middle, not as a
    // line pinned to the floor — normalising a zero range would do the latter.
    const spread = max - min
    const flat = spread < Math.max(0.01, Math.abs(max) * 1e-4)
    const points = samples.map((s, i) => {
      const x = i * step
      const y = flat ? h / 2 : h - ((s.equity - min) / spread) * (h - 12) - 6
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    return {
      line: `M ${points.join(' L ')}`,
      area: `M ${points.join(' L ')} L ${w},${h} L 0,${h} Z`,
      first: values[0],
      last: values[values.length - 1],
      min,
      max,
    }
  }, [samples])

  const rising = path ? path.last >= path.first : true
  const stroke = rising ? 'var(--up)' : 'var(--down)'

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="panel-title">Portfolio</span>
        <span className="tiny" style={{ marginLeft: 'auto' }}>
          <span className="faint-text">at risk </span>
          <span className="mono">{money(stats.openCost)}</span>
          <span className="faint-text"> · P/L </span>
          <span className={`mono ${stats.portfolioPnl >= 0 ? 'up-text' : 'down-text'}`}>
            {moneySigned(stats.portfolioPnl)}
          </span>
        </span>
      </div>
      <div className="panel-body" style={{ paddingTop: 8, paddingBottom: 10 }}>
        {path ? (
          <svg className="spark" viewBox="0 0 100 100" preserveAspectRatio="none">
            <defs>
              <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity="0.24" />
                <stop offset="100%" stopColor={stroke} stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d={path.area} fill="url(#equityFill)" />
            <path
              d={path.line}
              fill="none"
              stroke={stroke}
              strokeWidth="1.4"
              vectorEffect="non-scaling-stroke"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        ) : (
          <div className="spark" style={{
            display: 'grid', placeItems: 'center', color: 'var(--text-faint)',
            fontSize: 12.5,
          }}>
            Equity curve builds while the bot runs.
          </div>
        )}
      </div>
    </div>
  )
}

export function Hero() {
  return (
    <div className="hero">
      <Countdown />
      <EquityCurve />
    </div>
  )
}
