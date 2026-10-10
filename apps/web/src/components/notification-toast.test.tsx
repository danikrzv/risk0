import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { NotificationToast } from "./notification-toast"

const notification = {
  notification_id: "notification-1",
  incident_id: "incident-1",
  severity: "CRITICAL" as const,
  title: "Oracle deviation",
  message: "The market oracle deviated by 12%.",
  created_at: "2026-10-10T10:00:00Z",
  read_at: null,
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("NotificationToast", () => {
  it("announces a notification and auto-dismisses it", () => {
    vi.useFakeTimers()
    const onDismiss = vi.fn()
    render(<NotificationToast notification={notification} onDismiss={onDismiss} dismissAfterMs={4_000} />)

    expect(screen.getByRole("alert")).toHaveTextContent("Oracle deviation")
    expect(screen.getByRole("alert")).toHaveTextContent("The market oracle deviated by 12%.")

    act(() => vi.advanceTimersByTime(3_999))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(1))
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it("does not restart dismissal when the parent supplies a new callback identity", () => {
    vi.useFakeTimers()
    const dismiss = vi.fn()
    const { rerender } = render(<NotificationToast notification={notification} onDismiss={() => dismiss()} dismissAfterMs={4_000} />)

    for (let second = 1; second <= 3; second += 1) {
      act(() => vi.advanceTimersByTime(1_000))
      rerender(<NotificationToast notification={notification} onDismiss={() => dismiss()} dismissAfterMs={4_000} />)
    }
    act(() => vi.advanceTimersByTime(1_000))
    expect(dismiss).toHaveBeenCalledOnce()
  })

  it("supports keyboard-accessible dismissal and incident navigation", () => {
    const onDismiss = vi.fn()
    const onIncidentClick = vi.fn()
    render(
      <NotificationToast
        notification={notification}
        onDismiss={onDismiss}
        onIncidentClick={onIncidentClick}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "View incident Oracle deviation" }))
    expect(onIncidentClick).toHaveBeenCalledWith("incident-1")

    fireEvent.click(screen.getByRole("button", { name: "Dismiss Oracle deviation notification" }))
    expect(onDismiss).toHaveBeenCalledOnce()
  })
})
