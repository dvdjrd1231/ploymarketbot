/**
 * Shared one-second ticker for countdowns.
 *
 * Countdowns are rendered from absolute timestamps the server sends once, so
 * they keep ticking smoothly even between engine updates — and a market's time
 * remaining never depends on the network. One interval drives every countdown
 * in the app rather than one timer per row.
 */

import { useCallback, useSyncExternalStore } from 'react'

let now = Date.now()
const listeners = new Set<() => void>()
let timer: number | undefined

function tick() {
  now = Date.now()
  for (const fn of listeners) fn()
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  if (timer === undefined) {
    // Align to the second boundary so every countdown flips at the same moment.
    const delay = 1000 - (Date.now() % 1000)
    window.setTimeout(() => {
      tick()
      timer = window.setInterval(tick, 1000)
    }, delay)
    timer = -1 as unknown as number
  }
  return () => {
    listeners.delete(cb)
  }
}

/** Current wall-clock time in ms, updated once per second. */
export function useNow(): number {
  return useSyncExternalStore(
    useCallback((cb: () => void) => subscribe(cb), []),
    useCallback(() => now, []),
  )
}
