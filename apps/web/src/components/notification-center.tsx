"use client"

import type { Notification } from "@risk0/contracts"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

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

  // Only the latest history response is applied; toast requests from
  // superseded fetches are kept and resolved against it.
  const requestToken = useRef(0)
  const pendingToastIds = useRef(new Set<string>())
  const markingIds = useRef(new Set<string>())

  const refresh = useCallback(async (toastId?: string, showUnread = false) => {
    if (toastId) pendingToastIds.current.add(toastId)
    const token = ++requestToken.current
    try {
      const response = await getNotifications(undefined, { limit: HISTORY_LIMIT })
      if (token !== requestToken.current) return
      setNotifications(response.notifications)
      const ids = new Set(response.notifications.map((item) => item.notification_id))
      const newToasts = [...pendingToastIds.current].filter((id) => ids.has(id))
      pendingToastIds.current.clear()
      if (showUnread) {
        newToasts.push(...response.notifications.filter((item) => item.read_at === null).map((item) => item.notification_id))
      }
      if (newToasts.length > 0) setToastIds((current) => [...new Set([...current, ...newToasts])])
      setError(null)
    } catch {
      if (token !== requestToken.current) return
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
    // Ignore repeat clicks while the first request is in flight.
    if (notification.read_at !== null || markingIds.current.has(notification.notification_id)) return
    markingIds.current.add(notification.notification_id)
    try {
      const updated = await markNotificationRead(notification.notification_id)
      setNotifications((current) => current.map((item) => (
        item.notification_id === updated.notification_id ? updated : item
      )))
      setError(null)
    } catch {
      setError("The notification could not be marked as read.")
    } finally {
      markingIds.current.delete(notification.notification_id)
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
