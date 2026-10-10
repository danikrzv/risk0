"use client"

import { useEffect, useRef } from "react"

import { getRadarEventsUrl } from "../lib/api"

export type RadarEventType =
  | "graph.updated"
  | "incident.opened"
  | "incident.updated"
  | "incident.resolved"
  | "notification.created"

export interface RadarEvent {
  sequence: number
  type: RadarEventType
  resource_id: string
  summary: Record<string, unknown>
}

export interface UseRadarEventsOptions {
  enabled?: boolean
  onGraphUpdated?: (event: RadarEvent) => void
  onIncident?: (event: RadarEvent) => void
  onNotification?: (event: RadarEvent) => void
  onReconnect?: () => void
}

const EVENT_TYPES: RadarEventType[] = [
  "graph.updated",
  "incident.opened",
  "incident.updated",
  "incident.resolved",
  "notification.created",
]
const INITIAL_RECONNECT_DELAY_MS = 1_000
const MAX_RECONNECT_DELAY_MS = 30_000

export function useRadarEvents(options: UseRadarEventsOptions): void {
  const callbacks = useRef(options)
  callbacks.current = options

  useEffect(() => {
    if (options.enabled === false) return

    let active = true
    let source: EventSource | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectDelay = INITIAL_RECONNECT_DELAY_MS
    let lastSequence = 0
    // A new EventSource cannot set Last-Event-ID itself, so the last delivered
    // ID is passed to the same-origin proxy, which forwards it upstream.
    let lastEventId: number | null = null
    let opened = false

    const dispatch = (message: MessageEvent<string>) => {
      const event = parseRadarEvent(message.data)
      if (!event || event.sequence <= lastSequence) return
      lastSequence = event.sequence
      lastEventId = event.sequence

      if (event.type === "graph.updated") callbacks.current.onGraphUpdated?.(event)
      else if (event.type === "notification.created") callbacks.current.onNotification?.(event)
      else callbacks.current.onIncident?.(event)
    }

    const connect = () => {
      if (!active) return
      source = new EventSource(getRadarEventsUrl(lastEventId))
      for (const eventType of EVENT_TYPES) {
        source.addEventListener(eventType, dispatch as EventListener)
      }
      source.onopen = () => {
        reconnectDelay = INITIAL_RECONNECT_DELAY_MS
        if (opened) {
          lastSequence = 0
          callbacks.current.onReconnect?.()
        }
        opened = true
      }
      source.onerror = () => {
        source?.close()
        source = null
        if (!active || reconnectTimer !== null) return
        const delay = reconnectDelay
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null
          connect()
        }, delay)
      }
    }

    connect()
    return () => {
      active = false
      source?.close()
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
    }
  }, [options.enabled])
}

function parseRadarEvent(data: string): RadarEvent | null {
  let value: unknown
  try {
    value = JSON.parse(data)
  } catch {
    return null
  }
  if (!value || typeof value !== "object") return null

  const candidate = value as Partial<RadarEvent>
  if (
    !Number.isSafeInteger(candidate.sequence) ||
    (candidate.sequence ?? 0) < 0 ||
    !EVENT_TYPES.includes(candidate.type as RadarEventType) ||
    typeof candidate.resource_id !== "string" ||
    !candidate.summary ||
    typeof candidate.summary !== "object" ||
    Array.isArray(candidate.summary)
  ) {
    return null
  }
  return candidate as RadarEvent
}
