import { type RefObject, useEffect, useState } from "react"

const STATUS_BAR_SELECTOR = ".terminal-statusbar"

/**
 * Height (px) that lets `ref`'s element fill the viewport from where it starts
 * down to the sticky status bar, or null when disabled. Re-measured on window
 * resize and whenever the page above reflows (header wrapping, alerts); a hidden
 * element (another workspace category) is skipped until it is shown again.
 */
export function useFillViewport(
  ref: RefObject<HTMLElement | null>,
  { enabled = true, minimum = 480, gap = 12 }: { enabled?: boolean; minimum?: number; gap?: number } = {},
): number | null {
  const [height, setHeight] = useState<number | null>(null)

  useEffect(() => {
    const element = ref.current
    if (!enabled || !element || typeof window === "undefined") {
      setHeight(null)
      return
    }
    const measure = () => {
      if (!element.isConnected || element.getClientRects().length === 0) return
      const top = element.getBoundingClientRect().top + window.scrollY
      const statusBar = document.querySelector<HTMLElement>(STATUS_BAR_SELECTOR)
      const reserved = statusBar?.getBoundingClientRect().height ?? 0
      const next = Math.max(minimum, Math.floor(window.innerHeight - top - reserved - gap))
      setHeight((current) => (current === next ? current : next))
    }
    measure()
    window.addEventListener("resize", measure)
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure)
    observer?.observe(document.body)
    return () => {
      window.removeEventListener("resize", measure)
      observer?.disconnect()
    }
  }, [enabled, gap, minimum, ref])

  return height
}
