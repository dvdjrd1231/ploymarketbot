import { useAccount, useEngineState, useStats } from '../lib/live'
import { useNow } from '../lib/clock'
import { ageLabel, money, moneySigned, pct } from '../lib/format'
import { TickCell } from './common'

function Stat({
  label, value, sub, tone,
}: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  tone?: 'up' | 'down' | 'accent'
}) {
  return (
    <div className={`stat${tone ? ` ${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      <div className="stat-sub">{sub ?? ' '}</div>
    </div>
  )
}

/**
 * The number strip. Every figure here is pushed, not polled — balance from the
 * account loop, unrealized P/L recomputed from streamed marks several times a
 * second.
 */
export function StatCards() {
  const account = useAccount()
  const stats = useStats()
  const state = useEngineState()
  const now = useNow()

  const equity = account.balance + stats.openValue
  const portfolio = stats.portfolioPnl

  return (
    <div className="stat-grid">
      <Stat
        label={state.paper ? 'Virtual balance' : 'Wallet balance'}
        value={<TickCell value={account.balance} format={money} />}
        sub={account.updatedTs
          ? `updated ${ageLabel(account.updatedTs, now)}`
          : 'not connected'}
      />
      <Stat
        label="Portfolio value"
        value={<TickCell value={equity} format={money} />}
        sub={`${money(account.balance)} cash + ${money(stats.openValue)} positions`}
        tone="accent"
      />
      <Stat
        label="Open P/L"
        value={<TickCell value={stats.unrealizedPnl} format={moneySigned} />}
        sub={`${pct(stats.openReturnPct)} on ${money(stats.openCost)} at risk`}
        tone={stats.unrealizedPnl >= 0 ? 'up' : 'down'}
      />
      <Stat
        label="Realized today"
        value={<TickCell value={stats.realizedPnlToday} format={moneySigned} />}
        sub={`${stats.closed} settled · ${stats.skipped} skipped`}
        tone={stats.realizedPnlToday >= 0 ? 'up' : 'down'}
      />
      <Stat
        label="Total P/L"
        value={<TickCell value={portfolio} format={moneySigned} />}
        sub="open + realized today"
        tone={portfolio >= 0 ? 'up' : 'down'}
      />
      <Stat
        label="Open positions"
        value={String(stats.openPositions)}
        sub={state.mode === 'fixed_50'
          ? 'Fixed 50% — one at a time'
          : 'Proportional — many allowed'}
      />
      <Stat
        label="Win rate"
        value={stats.settledTotal ? pct(stats.winRate, false) : '—'}
        sub={stats.settledTotal
          ? `${stats.winsTotal}/${stats.settledTotal} settled trades`
          : 'no settled trades yet'}
      />
    </div>
  )
}
