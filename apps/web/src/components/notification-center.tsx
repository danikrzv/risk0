"use client"

import type { Notification } from "@risk0/contracts"
import { useCallback, useEffect, useMemo, useState } from "react"

import { useRadarEvents } from "../hooks/use-radar-events"
import { getNotifications, markNotificationRead } from "../lib/api"
import { NotificationToast } from "./notification-toast"

const HISTORY_LIMIT = 100

export function NotificationCenter({
  onIncidentClick,
  toastDurationMs,
}: {
  onIncidentClick?: (incidentId: string) => void
  toastDurationMs?: number
}) {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [toastIds, setToastIds] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (toastId?: string, showUnread = false) => {
    try {
      const response = await getNotifications(undefined, { limit: HISTORY_LIMIT })
      setNotifications(response.notifications)
      if (showUnread) {
        const unreadIds = response.notifications.filter((item) => item.read_at === null).map((item) => item.notification_id)
        setToastIds((current) => [...new Set([...current, ...unreadIds])])
      }
      if (toastId && response.notifications.some((item) => item.notification_id === toastId)) {
        setToastIds((current) => current.includes(toastId) ? current : [...current, toastId])
      }
      setError(null)
    } catch {
      setError("Notifications are temporarily unavailable.")
    }
  }, [])

  useEffect(() => {
    void refresh(undefined, true)
  }, [refresh])

  useRadarEvents({
    onNotification: (event) => {
      void refresh(event.resource_id)
    },
    onReconnect: () => {
      void refresh()
    },
  })

  const unreadCount = useMemo(
    () => notifications.filter((notification) => notification.read_at === null).length,
    [notifications],
  )
  const toasts = useMemo(
    () => toastIds
      .map((id) => notifications.find((notification) => notification.notification_id === id))
      .filter((notification): notification is Notification => notification !== undefined),
    [notifications, toastIds],
  )

  const markRead = async (notification: Notification) => {
    try {
      const updated = await markNotificationRead(notification.notification_id)
      setNotifications((current) => current.map((item) => (
        item.notification_id === updated.notification_id ? updated : item
      )))
      setError(null)
    } catch {
      setError("The notification could not be marked as read.")
    }
  }

  return (
    <section aria-label="Notification center">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="notification-history"
        aria-label={`Notifications, ${unreadCount} unread`}
        onClick={() => setOpen((current) => !current)}
      >
        Notifications{unreadCount > 0 ? ` (${unreadCount})` : ""}
      </button>

      {error ? <p role="status">{error}</p> : null}

      {open ? (
        <div id="notification-history" aria-label="Notification history">
          <h2>Notification history</h2>
          {notifications.length === 0 ? <p>No notifications.</p> : (
            <ul>
              {notifications.map((notification) => (
                <li key={notification.notification_id}>
                  <strong>{notification.title}</strong>
                  <p>{notification.message}</p>
                  <time dateTime={notification.created_at}>
                    {new Date(notification.created_at).toLocaleString()}
                  </time>
                  {onIncidentClick ? (
                    <button
                      type="button"
                      aria-label={`View incident ${notification.title}`}
                      onClick={() => onIncidentClick(notification.incident_id)}
                    >
                      View incident
                    </button>
                  ) : null}
                  {notification.read_at === null ? (
                    <button
                      type="button"
                      aria-label={`Mark ${notification.title} as read`}
                      onClick={() => void markRead(notification)}
                    >
                      Mark as read
                    </button>
                  ) : <span>Read</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      <div aria-label="Live notifications">
        {toasts.map((notification) => (
          <NotificationToast
            key={notification.notification_id}
            notification={notification}
            dismissAfterMs={toastDurationMs}
            onIncidentClick={onIncidentClick}
            onDismiss={() => setToastIds((current) => (
              current.filter((id) => id !== notification.notification_id)
            ))}
          />
        ))}
      </div>
    </section>
  )
}
