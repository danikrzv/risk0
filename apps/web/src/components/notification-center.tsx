"use client"

import type { Notification } from "@risk0/contracts"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { useRadarEvents } from "../hooks/use-radar-events"
import { getNotifications, markAllNotificationsRead, markNotificationRead } from "../lib/api"
import { NotificationBurstToast, NotificationToast } from "./notification-toast"
import { ShowMore, useListLimit } from "./show-more"

const HISTORY_LIMIT = 100
/** History entries rendered at once; the rest are revealed on request. */
const HISTORY_PAGE_SIZE = 20
/** More toasts than this on screen at once are grouped into one summary toast. */
export const MAX_INDIVIDUAL_TOASTS = 3

export function NotificationCenter({
  onIncidentClick,
  toastDurationMs,
}: {
  onIncidentClick?: (incidentId: string) => void
  toastDurationMs?: number
}) {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [hasOlder, setHasOlder] = useState(false)
  const [toastIds, setToastIds] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [markingAll, setMarkingAll] = useState(false)
  const [historyLimit, showMoreHistory] = useListLimit(HISTORY_PAGE_SIZE)

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
      setHasOlder(response.next_cursor !== null)
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
  // Only the newest page is loaded; when all of it is unread, older ones may be too.
  const unreadLabel = hasOlder && unreadCount === notifications.length && unreadCount > 0
    ? `${unreadCount}+`
    : String(unreadCount)
  const toasts = useMemo(() => {
    const byId = new Map(notifications.map((notification) => [notification.notification_id, notification]))
    return toastIds
      .map((id) => byId.get(id))
      .filter((notification): notification is Notification => notification !== undefined)
  }, [notifications, toastIds])

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

  const markAllRead = async () => {
    if (markingAll) return
    setMarkingAll(true)
    try {
      await markAllNotificationsRead()
      const readAt = new Date().toISOString()
      setNotifications((current) => current.map((item) => (item.read_at === null ? { ...item, read_at: readAt } : item)))
      setToastIds([])
      setError(null)
      // Pick up server read timestamps (and anything that arrived meanwhile).
      void refresh()
    } catch {
      setError("Notifications could not be marked as read.")
    } finally {
      setMarkingAll(false)
    }
  }

  const dismissAllToasts = useCallback(() => setToastIds([]), [])
  const openHistory = useCallback(() => {
    setOpen(true)
    setToastIds([])
  }, [])

  const shownHistory = notifications.slice(0, historyLimit)
  return (
    <section aria-label="Notification center">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="notification-history"
        aria-label={`Notifications, ${unreadLabel} unread`}
        onClick={() => setOpen((current) => !current)}
      >
        Notifications{unreadCount > 0 ? ` (${unreadLabel})` : ""}
      </button>

      {error ? <p role="status">{error}</p> : null}

      {open ? (
        <div id="notification-history" aria-label="Notification history">
          <div className="notification-history__header">
            <h2>Notification history</h2>
            <button
              type="button"
              onClick={() => void markAllRead()}
              disabled={markingAll || unreadCount === 0}
            >
              {markingAll ? "Marking…" : "Mark all read"}
            </button>
          </div>
          {notifications.length === 0 ? <p>No notifications.</p> : (
            <ul id="notification-history-list">
              {shownHistory.map((notification) => (
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
          <ShowMore
            shown={shownHistory.length}
            total={notifications.length}
            pageSize={HISTORY_PAGE_SIZE}
            noun="notifications"
            controls="notification-history-list"
            onShowMore={showMoreHistory}
          />
        </div>
      ) : null}

      <div aria-label="Live notifications">
        {toasts.length > MAX_INDIVIDUAL_TOASTS ? (
          <NotificationBurstToast
            notifications={toasts}
            dismissAfterMs={toastDurationMs}
            onDismiss={dismissAllToasts}
            onOpenHistory={openHistory}
          />
        ) : toasts.map((notification) => (
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
