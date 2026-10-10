"use client"

import type { Notification } from "@risk0/contracts"
import { useEffect, useRef, useState } from "react"

const DEFAULT_DISMISS_DELAY_MS = 5_000

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
  const dismiss = useRef(onDismiss)
  dismiss.current = onDismiss
  // Auto-dismissal pauses while the user is reading or operating the toast,
  // so its buttons never vanish under the pointer or keyboard focus.
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const paused = hovered || focused
  useEffect(() => {
    if (paused) return
    const timer = setTimeout(() => dismiss.current(), dismissAfterMs)
    return () => clearTimeout(timer)
  }, [dismissAfterMs, notification.notification_id, paused])

  // Only critical incidents interrupt assistive technology; focus never moves.
  const critical = notification.severity === "CRITICAL"
  return (
    <article
      role={critical ? "alert" : "status"}
      aria-atomic="true"
      data-severity={notification.severity}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false)
      }}
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
