import { useMemo, useState } from 'react'
import { useHistory } from '../lib/live'
import { dateTime, money, moneySigned, pct, price, shares } from '../lib/format'
import { Empty, OutcomeBadge, Pnl, StatusBadge } from './common'

type Filter = 'ALL' | 'CLOSED' | 'SKIPPED' | 'FAILED'

const FILTERS: Filter[] = ['ALL', 'CLOSED', 'SKIPPED', 'FAILED']

/**
 * Completed trade history.
 *
 * Skipped trades are first-class here: knowing *why* the bot passed on a trade
 * (entry price moved, market too far out, minimum not reachable) is as useful
 * as seeing the ones it took.
 */
export function HistoryTable() {
  const history = useHistory()
  const [filter, setFilter] = useState<Filter>('ALL')
  const [query, setQuery] = useState('')

  const counts = useMemo(() => {
    const c = { ALL: history.length, CLOSED: 0, SKIPPED: 0, FAILED: 0 }
    for (const t of history) {
      if (t.status === 'CLOSED') c.CLOSED += 1
      else if (t.status === 'SKIPPED') c.SKIPPED += 1
      else if (t.status === 'FAILED') c.FAILED += 1
    }
    return c
  }, [history])

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return history.filter((t) => {
      if (filter !== 'ALL' && t.status !== filter) return false
      if (!needle) return true
      return (t.question || '').toLowerCase().includes(needle)
        || (t.reason || '').toLowerCase().includes(needle)
    })
  }, [history, filter, query])

  const settled = useMemo(
    () => history.filter((t) => t.status === 'CLOSED'),
    [history],
  )
  const totalPnl = settled.reduce((sum, t) => sum + t.realizedPnl, 0)

  if (history.length === 0) {
    return (
      <Empty
        title="No completed trades yet"
        hint="Settled positions, and every trade the rules filtered out, are recorded here with the reason."
      />
    )
  }

  return (
    <>
      <div className="row" style={{ padding: '0 0 10px' }}>
        <div className="row" style={{ gap: 4 }}>
          {FILTERS.map((f) => (
            <button
              key={f}
              className={`btn small${filter === f ? '' : ' ghost'}`}
              onClick={() => setFilter(f)}
            >
              {f === 'ALL' ? 'All' : f.charAt(0) + f.slice(1).toLowerCase()}
              <span className="faint-text">{counts[f]}</span>
            </button>
          ))}
        </div>
        <div className="grow" />
        <span className="tiny">
          <span className="faint-text">{settled.length} settled · realized </span>
          <span className={`mono ${totalPnl >= 0 ? 'up-text' : 'down-text'}`}>
            {moneySigned(totalPnl)}
          </span>
        </span>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder="Filter markets…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {rows.length === 0 ? (
        <Empty title="Nothing matches that filter" />
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Market</th>
                <th>Status</th>
                <th className="num">Entry</th>
                <th className="num">Exit</th>
                <th className="num">Shares</th>
                <th className="num">Cost</th>
                <th className="num">Realized P/L</th>
                <th className="num">Return</th>
                <th>Closed</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((trade) => {
                const isClosed = trade.status === 'CLOSED'
                const ret = trade.cost
                  ? (trade.realizedPnl / trade.cost) * 100
                  : 0
                return (
                  <tr key={trade.id}>
                    <td className="market-cell">
                      <span className="market-name" title={trade.question}>
                        {trade.question || trade.tokenId.slice(0, 28)}
                      </span>
                      <span className="market-meta">
                        <OutcomeBadge outcome={trade.outcome} />
                        <span>target @ {price(trade.targetPrice)}</span>
                      </span>
                    </td>
                    <td><StatusBadge status={trade.status} /></td>
                    <td className="num">{price(trade.entryPrice)}</td>
                    <td className="num">
                      {isClosed ? price(trade.currentPrice) : '—'}
                    </td>
                    <td className="num">
                      {trade.size ? shares(trade.size) : '—'}
                    </td>
                    <td className="num">{trade.cost ? money(trade.cost) : '—'}</td>
                    <td className="num">
                      {isClosed
                        ? <Pnl value={trade.realizedPnl} format={moneySigned} />
                        : <span className="faint-text">—</span>}
                    </td>
                    <td className="num">
                      {isClosed && trade.cost
                        ? <Pnl value={ret} format={(v) => pct(v)} />
                        : <span className="faint-text">—</span>}
                    </td>
                    <td className="tiny faint-text nowrap">
                      {dateTime(trade.closedTs ?? trade.openedTs)}
                    </td>
                    <td className="tiny dim-text" style={{
                      maxWidth: 300, whiteSpace: 'normal',
                    }}>
                      {trade.reason || '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
