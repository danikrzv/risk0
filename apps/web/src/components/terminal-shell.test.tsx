import "@testing-library/jest-dom/vitest"

import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { TerminalShell } from "./terminal-shell"

afterEach(cleanup)

describe("TerminalShell", () => {
  it("renders the institutional navigation and keeps page content accessible", () => {
    render(
      <TerminalShell status="live">
        <section aria-label="Workspace content">Analysis workspace</section>
      </TerminalShell>,
    )

    expect(screen.getByRole("heading", { name: "Risk0" })).toBeInTheDocument()
    const navigation = screen.getByRole("navigation", { name: "Primary" })
    expect(within(navigation).getByText("Analysis")).toHaveAttribute("aria-current", "page")
    expect(within(navigation).getByText("Graph")).toHaveAttribute("aria-disabled", "true")
    expect(screen.getByText("LIVE")).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Workspace content" })).toBeInTheDocument()
  })

  it("labels a degraded connection without presenting it as live", () => {
    render(<TerminalShell status="degraded">Content</TerminalShell>)

    expect(screen.getByText("DEGRADED")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
  })
})
