"use client"

import type { Notification } from "@risk0/contracts"
import { useEffect, useRef } from "react"

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
  useEffect(() => {
    const timer = setTimeout(() => dismiss.current(), dismissAfterMs)
    return () => clearTimeout(timer)
  }, [dismissAfterMs, notification.notification_id])

  return (
    <article role="alert" aria-live="assertive" aria-atomic="true" data-severity={notification.severity}>
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
