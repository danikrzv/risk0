"use client"

import { useSyncExternalStore } from "react"

// One shared 1 s clock for every data-age display. Only components that call
// useNow() re-render on a tick, so the page and its graphs stay still.

const TICK_MS = 1_000

let current = Date.now()
let timer: ReturnType<typeof setInterval> | null = null
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  if (timer === null) {
    current = Date.now()
    timer = setInterval(() => {
      current = Date.now()
      for (const notify of listeners) notify()
    }, TICK_MS)
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
}

function subscribeNever(): () => void {
  return () => {}
}

function snapshot(): number {
  // While no clock runs, refresh lazily so a first render is not stale.
  if (timer === null && Date.now() - current >= TICK_MS) current = Date.now()
  return current
}

/**
 * The current time in ms, updated every second while mounted. A fixed `now`
 * (tests, or a parent that already knows the time) disables the ticking.
 */
export function useNow(fixed?: number): number {
  const ticking = useSyncExternalStore(fixed === undefined ? subscribe : subscribeNever, snapshot, snapshot)
  return fixed ?? ticking
}
