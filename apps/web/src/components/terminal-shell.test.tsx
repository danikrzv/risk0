import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { TerminalShell } from "./terminal-shell"

afterEach(cleanup)

describe("TerminalShell", () => {
  it("renders the institutional navigation and keeps page content accessible", () => {
    const onCategoryChange = vi.fn()
    render(
      <TerminalShell status="live" activeCategory="graph" onCategoryChange={onCategoryChange}>
        <section aria-label="Workspace content">Analysis workspace</section>
      </TerminalShell>,
    )

    expect(screen.getByRole("heading", { name: "Risk0" })).toBeInTheDocument()
    const navigation = screen.getByRole("navigation", { name: "Primary" })
    const graph = within(navigation).getByRole("button", { name: "Graph" })
    expect(graph).toHaveAttribute("aria-current", "page")
    expect(within(navigation).getByRole("button", { name: "Markets" })).not.toHaveAttribute("aria-current")
    expect(within(navigation).getByRole("button", { name: "Oracles" })).toBeEnabled()
    expect(within(navigation).getByRole("button", { name: "Incidents" })).toBeEnabled()
    const positionAnalysis = within(navigation).getByRole("button", { name: "Position Analysis" })
    fireEvent.click(positionAnalysis)
    expect(onCategoryChange).toHaveBeenCalledWith("position-analysis")
    expect(screen.getByText("LIVE")).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Workspace content" })).toBeInTheDocument()
  })

  it("labels a degraded connection without presenting it as live", () => {
    render(<TerminalShell status="degraded" activeCategory="markets" onCategoryChange={() => {}}>Content</TerminalShell>)

    expect(screen.getByText("DEGRADED")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
  })

  it("exposes banner, main, and contentinfo landmarks with the disclaimer outside main", () => {
    render(<TerminalShell status="live" activeCategory="graph" onCategoryChange={() => {}}><p>Workspace</p></TerminalShell>)

    expect(screen.getByRole("banner")).toBeInTheDocument()
    expect(within(screen.getByRole("main")).getByText("Workspace")).toBeInTheDocument()
    expect(within(screen.getByRole("contentinfo")).getByText(/NOT INVESTMENT ADVICE/)).toBeInTheDocument()
    expect(screen.getByRole("main")).not.toContainElement(screen.getByRole("navigation", { name: "Primary" }))
    expect(screen.getByRole("main")).not.toContainElement(screen.getByText(/NOT INVESTMENT ADVICE/))
  })

  it("does not claim LIVE while the connection is still being established", () => {
    render(<TerminalShell status="connecting" activeCategory="graph" onCategoryChange={() => {}}>Content</TerminalShell>)

    expect(screen.getByText("CONNECTING")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
  })
})
