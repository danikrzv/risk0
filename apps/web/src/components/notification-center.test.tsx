import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { Notification } from "@risk0/contracts"

import type { RadarEvent } from "../hooks/use-radar-events"
import * as api from "../lib/api"
import { NotificationCenter } from "./notification-center"

vi.mock("../hooks/use-radar-events", () => ({
  useRadarEvents: vi.fn(),
}))
vi.mock("../lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/api")>()
  return {
    ...original,
    getNotifications: vi.fn(),
    markAllNotificationsRead: vi.fn(),
    markNotificationRead: vi.fn(),
  }
})

const unreadNotification = {
  notification_id: "notification-1",
  incident_id: "incident-1",
  severity: "WARNING" as const,
  title: "Liquidity warning",
  message: "Available exit liquidity fell below policy.",
  created_at: "2026-10-10T10:00:00Z",
  read_at: null,
}

beforeEach(() => {
  vi.mocked(api.getNotifications).mockResolvedValue({
    schema_version: "1.0.0",
    notifications: [unreadNotification],
    next_cursor: null,
    generated_at: "2026-10-10T10:01:00Z",
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe("NotificationCenter", () => {
  it("surfaces unread startup notifications as transient toasts", async () => {
    vi.useFakeTimers()
    render(<NotificationCenter toastDurationMs={3_000} />)
    await act(async () => Promise.resolve())

    expect(screen.getByRole("status")).toHaveTextContent("Liquidity warning")
    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("loads persistent history, reports unread count, and marks entries read", async () => {
    vi.mocked(api.markNotificationRead).mockResolvedValue({
      ...unreadNotification,
      read_at: "2026-10-10T10:02:00Z",
    })
    render(<NotificationCenter />)

    expect(await screen.findByRole("button", { name: "Notifications, 1 unread" })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Notifications, 1 unread" }))
    expect(within(screen.getByLabelText("Notification history")).getByText("Liquidity warning")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Mark Liquidity warning as read" }))
    await waitFor(() => expect(api.markNotificationRead).toHaveBeenCalledWith("notification-1"))
    expect(screen.getByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument()
    expect(within(screen.getByLabelText("Notification history")).getByText("Liquidity warning")).toBeInTheDocument()
  })

  it("keeps history after a live notification toast auto-dismisses", async () => {
    const { useRadarEvents } = await import("../hooks/use-radar-events")
    let onNotification: ((event: RadarEvent) => void) | undefined
    vi.mocked(useRadarEvents).mockImplementation((options) => {
      onNotification = options.onNotification
    })
    vi.mocked(api.getNotifications)
      .mockResolvedValueOnce({
        schema_version: "1.0.0",
        notifications: [],
        next_cursor: null,
        generated_at: "2026-10-10T10:01:00Z",
      })
      .mockResolvedValueOnce({
        schema_version: "1.0.0",
        notifications: [unreadNotification],
        next_cursor: null,
        generated_at: "2026-10-10T10:02:00Z",
      })
    vi.useFakeTimers()
    render(<NotificationCenter toastDurationMs={3_000} />)
    await act(async () => Promise.resolve())

    await act(async () => {
      onNotification?.({
        sequence: 9,
        type: "notification.created",
        resource_id: "notification-1",
        summary: { incident_id: "incident-1", severity: "WARNING" },
      })
      await Promise.resolve()
    })
    expect(screen.getByRole("status")).toHaveTextContent("Liquidity warning")

    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Notifications, 1 unread" }))
    expect(screen.getByText("Liquidity warning")).toBeInTheDocument()
  })

  it("invokes the incident callback from notification history", async () => {
    const onIncidentClick = vi.fn()
    render(<NotificationCenter onIncidentClick={onIncidentClick} />)

    fireEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }))
    fireEvent.click(within(screen.getByLabelText("Notification history")).getByRole("button", { name: "View incident Liquidity warning" }))
    expect(onIncidentClick).toHaveBeenCalledWith("incident-1")
  })

  it("reloads history after the event stream reconnects", async () => {
    const { useRadarEvents } = await import("../hooks/use-radar-events")
    let onReconnect: (() => void) | undefined
    vi.mocked(useRadarEvents).mockImplementation((options) => {
      onReconnect = options.onReconnect
    })
    render(<NotificationCenter />)
    await screen.findByRole("button", { name: "Notifications, 1 unread" })
    expect(api.getNotifications).toHaveBeenCalledTimes(1)

    await act(async () => onReconnect?.())
    await waitFor(() => expect(api.getNotifications).toHaveBeenCalledTimes(2))
  })

  it("marks a notification read only once even when clicked repeatedly", async () => {
    let resolveRead: ((value: Notification) => void) | undefined
    vi.mocked(api.markNotificationRead).mockImplementation(() => new Promise((resolve) => { resolveRead = resolve as never }))
    render(<NotificationCenter />)

    fireEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }))
    const markRead = screen.getByRole("button", { name: "Mark Liquidity warning as read" })
    fireEvent.click(markRead)
    fireEvent.click(markRead)
    expect(api.markNotificationRead).toHaveBeenCalledOnce()
    await act(async () => resolveRead?.({ ...unreadNotification, read_at: "2026-10-10T10:02:00Z" }))
    expect(screen.getByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument()
  })

  it("ignores an older history response that resolves after a newer one", async () => {
    const { useRadarEvents } = await import("../hooks/use-radar-events")
    let onNotification: ((event: RadarEvent) => void) | undefined
    vi.mocked(useRadarEvents).mockImplementation((options) => {
      onNotification = options.onNotification
    })
    const second = { ...unreadNotification, notification_id: "notification-2", title: "Oracle critical" }
    let releaseStale: (() => void) | undefined
    vi.mocked(api.getNotifications)
      .mockResolvedValueOnce({ schema_version: "1.0.0", notifications: [], next_cursor: null, generated_at: "2026-10-10T10:00:00Z" })
      .mockImplementationOnce(() => new Promise((resolve) => {
        releaseStale = () => resolve({ schema_version: "1.0.0", notifications: [unreadNotification], next_cursor: null, generated_at: "2026-10-10T10:01:00Z" })
      }))
      .mockResolvedValueOnce({ schema_version: "1.0.0", notifications: [second, unreadNotification], next_cursor: null, generated_at: "2026-10-10T10:02:00Z" })
    render(<NotificationCenter />)
    await screen.findByRole("button", { name: "Notifications, 0 unread" })

    await act(async () => {
      onNotification?.({ sequence: 1, type: "notification.created", resource_id: "notification-1", summary: {} })
      onNotification?.({ sequence: 2, type: "notification.created", resource_id: "notification-2", summary: {} })
    })
    await screen.findByRole("button", { name: "Notifications, 2 unread" })
    await act(async () => releaseStale?.())

    expect(screen.getByRole("button", { name: "Notifications, 2 unread" })).toBeInTheDocument()
    expect(screen.getAllByRole("status")).toHaveLength(2)
  })

  it("groups a burst of notifications into one summary toast", async () => {
    const burst = Array.from({ length: 5 }, (_, index) => ({
      ...unreadNotification,
      notification_id: `notification-${index}`,
      title: `Warning ${index}`,
      severity: index === 2 ? ("CRITICAL" as const) : ("WARNING" as const),
      created_at: `2026-10-10T10:0${index}:00Z`,
    }))
    vi.mocked(api.getNotifications).mockResolvedValue({
      schema_version: "1.0.0", notifications: burst, next_cursor: null, generated_at: "2026-10-10T10:06:00Z",
    })
    vi.useFakeTimers()
    render(<NotificationCenter toastDurationMs={3_000} />)
    await act(async () => Promise.resolve())

    const live = screen.getByLabelText("Live notifications")
    expect(live.querySelectorAll("article")).toHaveLength(1)
    // One critical entry makes the summary an alert.
    const summary = within(live).getByRole("alert")
    expect(summary).toHaveTextContent("5 new warnings")
    expect(summary).toHaveTextContent("1 critical")
    expect(summary).toHaveTextContent("Latest: Warning 4")

    fireEvent.click(within(summary).getByRole("button", { name: "View notifications" }))
    expect(screen.getByLabelText("Notification history")).toBeInTheDocument()
    expect(live.querySelector("article")).toBeNull()
  })

  it("auto-dismisses the summary toast and keeps up to three individual toasts", async () => {
    const few = Array.from({ length: 3 }, (_, index) => ({
      ...unreadNotification, notification_id: `notification-${index}`, title: `Warning ${index}`,
    }))
    vi.mocked(api.getNotifications).mockResolvedValue({
      schema_version: "1.0.0", notifications: few, next_cursor: null, generated_at: "2026-10-10T10:06:00Z",
    })
    vi.useFakeTimers()
    render(<NotificationCenter toastDurationMs={3_000} />)
    await act(async () => Promise.resolve())

    expect(screen.getAllByRole("status")).toHaveLength(3)
    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("caps the rendered history and reports more unread than the loaded page", async () => {
    const many = Array.from({ length: 100 }, (_, index) => ({
      ...unreadNotification, notification_id: `notification-${index}`, title: `Warning ${index}`,
    }))
    vi.mocked(api.getNotifications).mockResolvedValue({
      schema_version: "1.0.0", notifications: many, next_cursor: "notification-100", generated_at: "2026-10-10T10:06:00Z",
    })
    render(<NotificationCenter />)

    fireEvent.click(await screen.findByRole("button", { name: "Notifications, 100+ unread" }))
    const history = screen.getByLabelText("Notification history")
    expect(within(history).getAllByRole("listitem")).toHaveLength(20)
    expect(history).toHaveTextContent("Showing 20 of 100 notifications")
    fireEvent.click(within(history).getByRole("button", { name: "Show 20 more notifications" }))
    expect(within(history).getAllByRole("listitem")).toHaveLength(40)
  })

  it("marks every notification read and clears pending toasts", async () => {
    const second = { ...unreadNotification, notification_id: "notification-2", title: "Oracle critical" }
    vi.mocked(api.getNotifications)
      .mockResolvedValueOnce({ schema_version: "1.0.0", notifications: [unreadNotification, second], next_cursor: null, generated_at: "2026-10-10T10:01:00Z" })
      .mockResolvedValue({
        schema_version: "1.0.0",
        notifications: [unreadNotification, second].map((item) => ({ ...item, read_at: "2026-10-10T10:02:00Z" })),
        next_cursor: null,
        generated_at: "2026-10-10T10:02:00Z",
      })
    vi.mocked(api.markAllNotificationsRead).mockResolvedValue({ schema_version: "1.0.0", updated: 2 })
    render(<NotificationCenter />)

    fireEvent.click(await screen.findByRole("button", { name: "Notifications, 2 unread" }))
    expect(screen.getAllByRole("status")).toHaveLength(2)
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }))

    expect(await screen.findByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument()
    expect(api.markAllNotificationsRead).toHaveBeenCalledOnce()
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Mark all read" })).toBeDisabled()
    await waitFor(() => expect(api.getNotifications).toHaveBeenCalledTimes(2))
  })

  it("reports a failed mark-all without losing unread state", async () => {
    vi.mocked(api.markAllNotificationsRead).mockRejectedValue(new api.ApiError("Risk service is busy — retrying", 503))
    render(<NotificationCenter />)

    fireEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }))
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }))

    expect(await screen.findByText("Notifications could not be marked as read.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Notifications, 1 unread" })).toBeInTheDocument()
  })
})
