import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useRadarEvents } from "./use-radar-events"

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListener): void {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener as (event: MessageEvent) => void)
    this.listeners.set(type, listeners)
  }

  emit(type: string, payload: unknown): void {
    const event = new MessageEvent(type, { data: JSON.stringify(payload) })
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal("EventSource", FakeEventSource)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("useRadarEvents", () => {
  it("opens the same-origin stream and dispatches only targeted callbacks", () => {
    const onGraphUpdated = vi.fn()
    const onIncident = vi.fn()
    const onNotification = vi.fn()
    renderHook(() => useRadarEvents({ onGraphUpdated, onIncident, onNotification }))

    const stream = FakeEventSource.instances[0]
    expect(stream.url).toBe("/api/events")
    act(() => {
      stream.emit("graph.updated", event(1, "graph.updated", "graph"))
      stream.emit("incident.opened", event(2, "incident.opened", "incident-1"))
      stream.emit("incident.updated", event(3, "incident.updated", "incident-1"))
      stream.emit("incident.resolved", event(4, "incident.resolved", "incident-1"))
      stream.emit("notification.created", event(5, "notification.created", "notification-1"))
    })

    expect(onGraphUpdated).toHaveBeenCalledTimes(1)
    expect(onIncident).toHaveBeenCalledTimes(3)
    expect(onNotification).toHaveBeenCalledTimes(1)
  })

  it("deduplicates replayed or out-of-order sequences and ignores malformed payloads", () => {
    const onIncident = vi.fn()
    renderHook(() => useRadarEvents({ onIncident }))
    const stream = FakeEventSource.instances[0]

    act(() => {
      stream.emit("incident.opened", event(8, "incident.opened", "incident-1"))
      stream.emit("incident.updated", event(8, "incident.updated", "incident-1"))
      stream.emit("incident.updated", event(7, "incident.updated", "incident-1"))
      stream.emit("incident.updated", { sequence: "nine" })
    })

    expect(onIncident).toHaveBeenCalledOnce()
  })

  it("reconnects with bounded exponential backoff and cancels pending work on unmount", () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => useRadarEvents({}))
    const first = FakeEventSource.instances[0]

    act(() => first.onerror?.())
    expect(first.close).toHaveBeenCalledOnce()
    expect(FakeEventSource.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(999))
    expect(FakeEventSource.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(1))
    expect(FakeEventSource.instances).toHaveLength(2)

    const second = FakeEventSource.instances[1]
    act(() => second.onerror?.())
    act(() => vi.advanceTimersByTime(1_999))
    expect(FakeEventSource.instances).toHaveLength(2)
    act(() => vi.advanceTimersByTime(1))
    expect(FakeEventSource.instances).toHaveLength(3)

    const third = FakeEventSource.instances[2]
    act(() => third.onerror?.())
    unmount()
    act(() => vi.advanceTimersByTime(60_000))
    expect(FakeEventSource.instances).toHaveLength(3)
    expect(third.close).toHaveBeenCalled()
  })

  it("resets reconnect delay after a successful connection", () => {
    vi.useFakeTimers()
    renderHook(() => useRadarEvents({}))
    act(() => FakeEventSource.instances[0].onerror?.())
    act(() => vi.advanceTimersByTime(1_000))
    act(() => FakeEventSource.instances[1].onerror?.())
    act(() => vi.advanceTimersByTime(2_000))

    const third = FakeEventSource.instances[2]
    act(() => third.onopen?.())
    act(() => third.onerror?.())
    act(() => vi.advanceTimersByTime(999))
    expect(FakeEventSource.instances).toHaveLength(3)
    act(() => vi.advanceTimersByTime(1))
    expect(FakeEventSource.instances).toHaveLength(4)
  })

  it("resets sequence state and refetches resources after a service restart", () => {
    vi.useFakeTimers()
    const onIncident = vi.fn()
    const onReconnect = vi.fn()
    renderHook(() => useRadarEvents({ onIncident, onReconnect }))
    const first = FakeEventSource.instances[0]
    act(() => {
      first.onopen?.()
      first.emit("incident.opened", event(8, "incident.opened", "incident-8"))
      first.onerror?.()
      vi.advanceTimersByTime(1_000)
    })

    const restarted = FakeEventSource.instances[1]
    act(() => {
      restarted.onopen?.()
      restarted.emit("incident.opened", event(1, "incident.opened", "incident-1"))
    })

    expect(onReconnect).toHaveBeenCalledOnce()
    expect(onIncident).toHaveBeenCalledTimes(2)
  })
})

function event(sequence: number, type: string, resourceId: string) {
  return { sequence, type, resource_id: resourceId, summary: {} }
}
