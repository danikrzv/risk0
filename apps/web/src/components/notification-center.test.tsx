import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

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

    expect(screen.getByRole("alert")).toHaveTextContent("Liquidity warning")
    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
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
    expect(screen.getByRole("alert")).toHaveTextContent("Liquidity warning")

    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
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
})

