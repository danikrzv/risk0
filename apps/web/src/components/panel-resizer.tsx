"use client"

import { type KeyboardEvent, type PointerEvent, useRef } from "react"

const KEYBOARD_STEP = 24

/**
 * A vertical separator on the left edge of the right-hand drawer: dragging it
 * left widens the drawer, right narrows it. Arrow keys resize by 24 px, Home and
 * End jump to the bounds. `onCommit` fires once a drag or key press settles, so
 * the caller can persist the width.
 */
export function PanelResizer({
  width,
  minimum,
  maximum,
  controls,
  onResize,
  onCommit,
}: {
  width: number
  minimum: number
  maximum: number
  controls: string
  onResize: (width: number) => void
  onCommit: (width: number) => void
}) {
  const drag = useRef<{ startX: number; startWidth: number; latest: number } | null>(null)
  const clamp = (value: number) => Math.round(Math.min(maximum, Math.max(minimum, value)))

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    drag.current = { startX: event.clientX, startWidth: width, latest: width }
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return
    // The drawer is anchored on the right: moving the edge left makes it wider.
    const next = clamp(drag.current.startWidth + drag.current.startX - event.clientX)
    drag.current.latest = next
    onResize(next)
  }

  function onPointerEnd(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    onCommit(drag.current.latest)
    drag.current = null
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const next = {
      ArrowLeft: clamp(width + KEYBOARD_STEP),
      ArrowRight: clamp(width - KEYBOARD_STEP),
      Home: minimum,
      End: maximum,
    }[event.key]
    if (next === undefined) return
    event.preventDefault()
    onResize(next)
    onCommit(next)
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize side panel"
      aria-controls={controls}
      aria-valuemin={minimum}
      aria-valuemax={maximum}
      aria-valuenow={width}
      tabIndex={0}
      className="panel-resizer"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
      onKeyDown={onKeyDown}
    />
  )
}
