"use client"

import { useCallback, useState } from "react"

/** Render limit for long lists: the first `pageSize` items, growing on request. */
export function useListLimit(pageSize: number): [number, () => void] {
  const [limit, setLimit] = useState(pageSize)
  const showMore = useCallback(() => setLimit((current) => current + pageSize), [pageSize])
  return [limit, showMore]
}

/** "Showing N of M" plus a button that reveals the next page of a capped list. */
export function ShowMore({
  shown,
  total,
  pageSize,
  noun,
  controls,
  onShowMore,
}: {
  shown: number
  total: number
  pageSize: number
  /** Plural noun for the items, e.g. "nodes". */
  noun: string
  /** Id of the list this button extends. */
  controls?: string
  onShowMore: () => void
}) {
  if (shown >= total) return null
  const next = Math.min(pageSize, total - shown)
  return (
    <p className="show-more">
      <span>Showing {shown.toLocaleString("en-US")} of {total.toLocaleString("en-US")} {noun}</span>
      <button type="button" className="link-button" aria-controls={controls} onClick={onShowMore}>
        Show {next.toLocaleString("en-US")} more {noun}
      </button>
    </p>
  )
}
