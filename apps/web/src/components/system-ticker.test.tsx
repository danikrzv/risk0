import "@testing-library/jest-dom/vitest"

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { SystemTicker } from "./system-ticker"

afterEach(cleanup)

describe("SystemTicker", () => {
  it("shows catalog and analysis metrics without inventing radar incidents", () => {
    render(
      <SystemTicker
        catalogStatus="ready"
        catalogEntries={17}
        supportedEntries={16}
        selectedEntry="RockawayX USDC Yield"
        analysisStatus="done"
      />,
    )

    expect(screen.getByText("CATALOG")).toBeInTheDocument()
    expect(screen.getByText("17")).toBeInTheDocument()
    expect(screen.getByText("SUPPORTED")).toBeInTheDocument()
    expect(screen.getByText("16")).toBeInTheDocument()
    expect(screen.getByText("RockawayX USDC Yield")).toBeInTheDocument()
    expect(screen.getByText("COMPLETE")).toBeInTheDocument()
    expect(screen.queryByText(/incident/i)).not.toBeInTheDocument()
  })

  it("uses explicit unavailable values while catalog data is loading", () => {
    render(
      <SystemTicker
        catalogStatus="loading"
        catalogEntries={0}
        supportedEntries={0}
        selectedEntry={null}
        analysisStatus="idle"
      />,
    )

    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3)
    expect(screen.getByText("IDLE")).toBeInTheDocument()
  })
})
