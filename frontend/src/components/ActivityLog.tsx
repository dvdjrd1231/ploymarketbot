import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLogs } from '../lib/live'
import { api } from '../lib/api'
import { clock } from '../lib/format'
import { Empty } from './common'

const LEVELS = ['ALL', 'SUCCESS', 'INFO', 'WARNING', 'ERROR'] as const
type Level = (typeof LEVELS)[number]

/**
 * Live activity log.
 *
 * Auto-scroll sticks to the bottom only while the user is already there, so
 * reading back through history is not yanked away by the next line.
 */
export function ActivityLog({ onError }: { onError: (m: string) => void }) {
  const logs = useLogs()
  const [level, setLevel] = useState<Level>('ALL')
  const [query, setQuery] = useState('')
  const [pinned, setPinned] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return logs.filter((line) => {
      if (level !== 'ALL' && line.level !== level) return false
      if (!needle) return true
      return line.message.toLowerCase().includes(needle)
    })
  }, [logs, level, query])

  useLayoutEffect(() => {
    if (!pinned) return
    const node = listRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [rows, pinned])

  useEffect(() => {
    const node = listRef.current
    if (!node) return
    const onScroll = () => {
      const atBottom =
        node.scrollHeight - node.scrollTop - node.clientHeight < 40
      setPinned(atBottom)
    }
    node.addEventListener('scroll', onScroll, { passive: true })
    return () => node.removeEventListener('scroll', onScroll)
  }, [])

  const clear = async () => {
    try {
      await api.clearLogs()
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    }
  }

  return (
    <>
      <div className="row" style={{ padding: '0 0 10px' }}>
        <div className="row" style={{ gap: 4 }}>
          {LEVELS.map((l) => (
            <button
              key={l}
              className={`btn small${level === l ? '' : ' ghost'}`}
              onClick={() => setLevel(l)}
            >
              {l === 'ALL' ? 'All' : l.charAt(0) + l.slice(1).toLowerCase()}
            </button>
          ))}
        </div>
        <div className="grow" />
        {!pinned && (
          <button className="link-btn" onClick={() => {
            setPinned(true)
            const node = listRef.current
            if (node) node.scrollTop = node.scrollHeight
          }}>
            ↓ Jump to latest
          </button>
        )}
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder="Search log…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="btn small ghost" onClick={clear}>Clear</button>
      </div>

      {rows.length === 0 ? (
        <Empty
          title={logs.length ? 'Nothing matches that filter' : 'No activity yet'}
          hint={logs.length
            ? undefined
            : 'Every decision the bot makes — copies, skips and the reason for each — is written here as it happens.'}
        />
      ) : (
        <div className="log-list" ref={listRef}>
          {rows.map((line, index) => (
            <div className="log-line" key={`${line.ts}-${index}`}>
              <span className="log-time">{clock(line.ts)}</span>
              <span className={`log-level ${line.level}`}>{line.level}</span>
              <span className="log-message">{line.message}</span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
