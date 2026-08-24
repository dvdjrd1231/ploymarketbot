import React, { memo, useEffect, useRef, useState } from 'react'

/** An empty-state block shown instead of a bare table with no rows. */
export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <svg width="30" height="30" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
        strokeLinejoin="round" opacity="0.5">
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M3 10h18M8 4v16" />
      </svg>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
    </div>
  )
}

export function Pill({
  tone = 'neutral', live = false, children,
}: {
  tone?: 'neutral' | 'up' | 'down' | 'warn' | 'info'
  live?: boolean
  children: React.ReactNode
}) {
  return (
    <span className={`pill ${tone === 'neutral' ? '' : tone}`}>
      <span className={`dot${live ? ' live' : ''}`} />
      {children}
    </span>
  )
}

const STATUS_CLASS: Record<string, string> = {
  OPEN: 'open', CLOSED: 'closed', SKIPPED: 'skipped', FAILED: 'failed',
  PENDING: 'pending', LIVE: 'pending', MATCHED: 'pending', FILLED: 'filled',
  CANCELED: 'skipped',
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${STATUS_CLASS[status] ?? ''}`}>{status}</span>
}

export function OutcomeBadge({ outcome }: { outcome: string }) {
  if (!outcome) return <span className="faint-text">—</span>
  const key = outcome.toLowerCase()
  const tone = key === 'yes' ? 'yes' : key === 'no' ? 'no' : ''
  return <span className={`badge ${tone}`}>{outcome}</span>
}

/**
 * A number cell that flashes green/red when its value moves.
 *
 * The flash is driven by a ref comparison rather than state so a price update
 * repaints one cell instead of re-rendering the row.
 */
export const TickCell = memo(function TickCell({
  value, format, className = '',
}: {
  value: number
  format: (v: number) => string
  className?: string
}) {
  const previous = useRef(value)
  const [flash, setFlash] = useState('')

  useEffect(() => {
    if (value === previous.current) return
    const direction = value > previous.current ? 'flash-up' : 'flash-down'
    previous.current = value
    setFlash(direction)
    const timer = window.setTimeout(() => setFlash(''), 650)
    return () => window.clearTimeout(timer)
  }, [value])

  return <span className={`tick ${flash} ${className}`}>{format(value)}</span>
})

/** Coloured P/L text: green above zero, red below, dim at flat. */
export function Pnl({
  value, format, showZeroDim = true,
}: {
  value: number
  format: (v: number) => string
  showZeroDim?: boolean
}) {
  const cls = value > 0 ? 'up-text'
    : value < 0 ? 'down-text'
      : showZeroDim ? 'dim-text' : ''
  return <span className={cls}>{format(value)}</span>
}

export function Field({
  label, hint, children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  )
}

export function Check({
  checked, onChange, title, hint, disabled = false,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  title: string
  hint?: string
  disabled?: boolean
}) {
  return (
    <label className="check" style={disabled ? { opacity: 0.55 } : undefined}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="check-text">
        <span className="check-title">{title}</span>
        {hint && <span className="check-hint" style={{ display: 'block' }}>{hint}</span>}
      </span>
    </label>
  )
}

export function Notice({
  tone = 'info', children,
}: {
  tone?: 'info' | 'warn' | 'danger' | 'good'
  children: React.ReactNode
}) {
  const glyph = tone === 'danger' ? '!' : tone === 'warn' ? '!' : tone === 'good' ? '✓' : 'i'
  return (
    <div className={`notice ${tone}`}>
      <span className="notice-icon" aria-hidden>{glyph}</span>
      <span>{children}</span>
    </div>
  )
}
