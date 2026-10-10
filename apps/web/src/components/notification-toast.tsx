"use client"

import type { Notification } from "@risk0/contracts"
import { type FocusEvent, useEffect, useRef, useState } from "react"

const DEFAULT_DISMISS_DELAY_MS = 5_000

/**
 * Calls `onDismiss` after `delayMs`, restarting when `resetKey` changes.
 * Auto-dismissal pauses while the user is reading or operating the toast, so
 * its buttons never vanish under the pointer or keyboard focus.
 */
function useAutoDismiss(onDismiss: () => void, delayMs: number, resetKey: unknown) {
  const dismiss = useRef(onDismiss)
  dismiss.current = onDismiss
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const paused = hovered || focused
  useEffect(() => {
    if (paused) return
    const timer = setTimeout(() => dismiss.current(), delayMs)
    return () => clearTimeout(timer)
  }, [delayMs, resetKey, paused])

  return {
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false),
    onFocus: () => setFocused(true),
    onBlur: (event: FocusEvent<HTMLElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false)
    },
  }
}

export function NotificationToast({
  notification,
  onDismiss,
  onIncidentClick,
  dismissAfterMs = DEFAULT_DISMISS_DELAY_MS,
}: {
  notification: Notification
  onDismiss: () => void
  onIncidentClick?: (incidentId: string) => void
  dismissAfterMs?: number
}) {
  const pauseHandlers = useAutoDismiss(onDismiss, dismissAfterMs, notification.notification_id)

  // Only critical incidents interrupt assistive technology; focus never moves.
  const critical = notification.severity === "CRITICAL"
  return (
    <article
      role={critical ? "alert" : "status"}
      aria-atomic="true"
      data-severity={notification.severity}
      {...pauseHandlers}
    >
      <strong>{notification.title}</strong>
      <p>{notification.message}</p>
      {onIncidentClick ? (
        <button
          type="button"
          aria-label={`View incident ${notification.title}`}
          onClick={() => onIncidentClick(notification.incident_id)}
        >
          View incident
        </button>
      ) : null}
      <button
        type="button"
        aria-label={`Dismiss ${notification.title} notification`}
        onClick={onDismiss}
      >
        Dismiss
      </button>
    </article>
  )
}

/** One toast standing in for a burst of notifications. */
export function NotificationBurstToast({
  notifications,
  onDismiss,
  onOpenHistory,
  dismissAfterMs = DEFAULT_DISMISS_DELAY_MS,
}: {
  notifications: Notification[]
  onDismiss: () => void
  onOpenHistory: () => void
  dismissAfterMs?: number
}) {
  const count = notifications.length
  // A growing burst restarts the timer so the newest arrival is still seen.
  const pauseHandlers = useAutoDismiss(onDismiss, dismissAfterMs, count)
  const critical = notifications.filter((notification) => notification.severity === "CRITICAL").length
  const title = `${count} new warning${count === 1 ? "" : "s"}`
  const latest = notifications.reduce((newest, item) => (
    Date.parse(item.created_at) > Date.parse(newest.created_at) ? item : newest
  ))
  return (
    <article
      role={critical > 0 ? "alert" : "status"}
      aria-atomic="true"
      data-severity={critical > 0 ? "CRITICAL" : "WARNING"}
      {...pauseHandlers}
    >
      <strong>{title}</strong>
      <p>
        {critical > 0 ? `${critical} critical. ` : ""}
        Latest: {latest.title}
      </p>
      <button type="button" onClick={onOpenHistory}>
        View notifications
      </button>
      <button type="button" aria-label={`Dismiss ${title}`} onClick={onDismiss}>
        Dismiss
      </button>
    </article>
  )
}
