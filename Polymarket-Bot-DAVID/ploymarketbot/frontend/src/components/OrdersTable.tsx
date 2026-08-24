import { useMemo, useState } from 'react'
import { useEngineState, useOrders } from '../lib/live'
import { api } from '../lib/api'
import { dateTime, money, price, shares } from '../lib/format'
import { Empty, OutcomeBadge, StatusBadge } from './common'

/**
 * Order blotter.
 *
 * Pending orders are pinned to the top and their status is driven by the
 * authenticated CLOB user channel, so a fill shows up the moment the exchange
 * matches it rather than on the next poll.
 */
export function OrdersTable({
  onError, onInfo,
}: {
  onError: (message: string) => void
  onInfo: (message: string) => void
}) {
  const orders = useOrders()
  const state = useEngineState()
  const [showAll, setShowAll] = useState(true)
  const [working, setWorking] = useState<string | null>(null)

  const { pending, visible } = useMemo(() => {
    const pendingOrders = orders.filter((o) => o.pending)
    return {
      pending: pendingOrders,
      visible: showAll ? orders : pendingOrders,
    }
  }, [orders, showAll])

  const cancel = async (orderId: string) => {
    setWorking(orderId)
    try {
      await api.cancelOrder(orderId)
      onInfo('Cancel sent to the exchange.')
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setWorking(null)
    }
  }

  if (orders.length === 0) {
    return (
      <Empty
        title="No orders yet"
        hint="Every order the bot submits is recorded here with its live exchange status — pending, filled, cancelled or rejected."
      />
    )
  }

  return (
    <>
      <div className="row" style={{ padding: '0 0 10px' }}>
        <span className="tiny faint-text">
          {pending.length} pending · {orders.length} total
        </span>
        <div className="grow" />
        <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show pending only' : 'Show all orders'}
        </button>
      </div>

      {visible.length === 0 ? (
        <Empty title="No pending orders"
          hint="Everything the bot submitted has reached a final state." />
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th className="num">Limit</th>
                <th className="num">Size</th>
                <th className="num">Filled</th>
                <th className="num">Notional</th>
                <th>Status</th>
                <th>Placed</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((order) => (
                <tr key={order.id}>
                  <td className="market-cell">
                    <span className="market-name" title={order.question}>
                      {order.question || order.tokenId.slice(0, 28)}
                    </span>
                    <span className="market-meta">
                      <OutcomeBadge outcome={order.outcome} />
                      <span className="mono">{order.orderId
                        ? `${order.orderId.slice(0, 10)}…`
                        : 'awaiting id'}</span>
                    </span>
                  </td>
                  <td>
                    <span className={order.side === 'BUY' ? 'up-text' : 'down-text'}
                      style={{ fontWeight: 600 }}>
                      {order.side}
                    </span>
                  </td>
                  <td className="num">{price(order.price)}</td>
                  <td className="num">{shares(order.size)}</td>
                  <td className="num">
                    {order.filledSize > 0 ? shares(order.filledSize) : '—'}
                  </td>
                  <td className="num">{money(order.price * order.size)}</td>
                  <td>
                    <StatusBadge status={order.status} />
                    {order.error && (
                      <div className="tiny down-text" title={order.error}
                        style={{
                          maxWidth: 260, overflow: 'hidden',
                          textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>
                        {order.error}
                      </div>
                    )}
                  </td>
                  <td className="tiny faint-text nowrap">
                    {dateTime(order.createdTs)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {order.pending && order.orderId && (
                      <button
                        className="btn small ghost"
                        disabled={!state.running || working === order.orderId}
                        onClick={() => cancel(order.orderId)}
                      >
                        Cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
