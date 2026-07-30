/** Display formatting. Kept pure and allocation-light — these run per cell. */

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const usdSigned = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2,
  maximumFractionDigits: 2, signDisplay: 'always',
})

const num = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
})

export const money = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : usd.format(v)

export const moneySigned = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : usdSigned.format(v)

export const shares = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : num.format(v)

/** Outcome prices are 0–1 probabilities; three decimals is the CLOB tick. */
export const price = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) || v <= 0
    ? '—'
    : v.toFixed(3)

export const cents = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) || v <= 0
    ? '—'
    : `${(v * 100).toFixed(1)}¢`

export const pct = (v: number | null | undefined, signed = true) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const sign = signed && v > 0 ? '+' : ''
  return `${sign}${v.toFixed(1)}%`
}

export const shortAddress = (a: string | null | undefined) => {
  if (!a) return '—'
  if (!a.startsWith('0x') || a.length < 12) return a
  return `${a.slice(0, 6)}…${a.slice(-4)}`
}

export const clock = (ts: number) => {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('en-GB', { hour12: false })
}

export const dateTime = (ts: number | null | undefined) => {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })} ${
    d.toLocaleTimeString('en-GB', { hour12: false })}`
}

/** Compact countdown for table cells: 2d 04h, 4h 09m, 9m 12s, 12s. */
export function countdown(endTs: number | null | undefined, now: number): string {
  if (!endTs) return '—'
  const remaining = endTs - Math.floor(now / 1000)
  if (remaining <= 0) return 'settling…'
  const d = Math.floor(remaining / 86400)
  const h = Math.floor((remaining % 86400) / 3600)
  const m = Math.floor((remaining % 3600) / 60)
  const s = remaining % 60
  if (d) return `${d}d ${String(h).padStart(2, '0')}h`
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** Full countdown for the hero panel. */
export function countdownFull(
  endTs: number | null | undefined, now: number,
): { text: string; urgent: boolean; expired: boolean } {
  if (!endTs) return { text: '—', urgent: false, expired: false }
  const remaining = endTs - Math.floor(now / 1000)
  if (remaining <= 0) return { text: 'settling…', urgent: true, expired: true }
  const d = Math.floor(remaining / 86400)
  const h = Math.floor((remaining % 86400) / 3600)
  const m = Math.floor((remaining % 3600) / 60)
  const s = remaining % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return {
    text: `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s`,
    urgent: remaining < 3600,
    expired: false,
  }
}

export function duration(seconds: number): string {
  if (!seconds || seconds < 0) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

export const ageLabel = (ts: number, now: number) => {
  if (!ts) return 'never'
  const age = now / 1000 - ts
  if (age < 1) return 'live'
  if (age < 60) return `${age.toFixed(0)}s ago`
  return `${Math.floor(age / 60)}m ago`
}

export const toneOf = (v: number) => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat')
