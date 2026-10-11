import "@testing-library/jest-dom/vitest"

import type { AnalysisResponse, Recommendation } from "@risk0/contracts"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { analysisResponse } from "../test-fixtures"
import { RecommendationCard } from "./recommendation-card"

afterEach(cleanup)

function withRecommendation(
  recommendation: Partial<Recommendation>,
  exit: Partial<AnalysisResponse["exit_estimate"]> = {},
): AnalysisResponse {
  return {
    ...analysisResponse,
    recommendation: { ...analysisResponse.recommendation, ...recommendation },
    exit_estimate: { ...analysisResponse.exit_estimate, ...exit },
  }
}

function card() {
  return screen.getByRole("region", { name: "Recommendation" })
}

describe("RecommendationCard", () => {
  it("offers View dependencies only when a graph handoff is available", () => {
    const onViewDependencies = vi.fn()
    const { rerender } = render(
      <RecommendationCard analysis={analysisResponse} numeraire="WETH" onSelectDependency={vi.fn()} />,
    )
    expect(within(card()).queryByRole("button", { name: "View dependencies" })).not.toBeInTheDocument()

    rerender(
      <RecommendationCard
        analysis={analysisResponse}
        numeraire="WETH"
        onSelectDependency={vi.fn()}
        onViewDependencies={onViewDependencies}
      />,
    )
    fireEvent.click(within(card()).getByRole("button", { name: "View dependencies" }))
    expect(onViewDependencies).toHaveBeenCalledTimes(1)
  })

  it("renders EXIT with the numeric comparison, triggers, and dominant dependency", () => {
    const onSelectDependency = vi.fn()
    render(
      <RecommendationCard
        analysis={analysisResponse}
        numeraire="WETH"
        onSelectDependency={onSelectDependency}
      />,
    )

    expect(within(card()).getByRole("heading", { name: "EXIT" })).toBeInTheDocument()
    expect(card()).toHaveAttribute("data-tone", "danger")
    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(within(comparison).getByText("0.0277 WETH")).toBeInTheDocument()
    expect(within(comparison).getByText("0.0131 – 0.0608 WETH")).toBeInTheDocument()
    expect(within(comparison).getByText("0.122 WETH")).toBeInTheDocument()
    expect(within(comparison).getByText("Expected 30-day loss does not exceed the cost of exiting now.")).toBeInTheDocument()
    expect(
      within(comparison).getByText("A hard safety trigger requires exit regardless of the cost comparison."),
    ).toBeInTheDocument()
    expect(within(card()).getByText("liquidation")).toBeInTheDocument()
    expect(within(card()).getByText("Confidence: medium")).toBeInTheDocument()
    expect(within(card()).getByText("A 15% collateral shock liquidates the position.")).toBeInTheDocument()

    fireEvent.click(within(card()).getByRole("button", { name: "wstETH / WETH market" }))
    expect(onSelectDependency).toHaveBeenCalledWith("market:1:wsteth-weth-965")
  })

  it("states when expected loss exceeds exit cost", () => {
    render(
      <RecommendationCard
        analysis={withRecommendation({ triggers: [], expected_loss: "0.5", exit_cost: "0.122" })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(within(comparison).getByText("Expected 30-day loss exceeds the cost of exiting now.")).toBeInTheDocument()
    expect(within(comparison).queryByText(/hard safety trigger/)).not.toBeInTheDocument()
  })

  it("renders STAY with safe styling", () => {
    render(
      <RecommendationCard
        analysis={withRecommendation({ decision: "STAY", triggers: [] })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    expect(within(card()).getByRole("heading", { name: "STAY" })).toBeInTheDocument()
    expect(card()).toHaveAttribute("data-tone", "safe")
    expect(within(card()).getByText("No hard safety trigger fired.")).toBeInTheDocument()
  })

  it("never renders UNAVAILABLE with safe styling", () => {
    render(
      <RecommendationCard
        analysis={withRecommendation({
          decision: "UNAVAILABLE",
          confidence: "unavailable",
          explanation: "Oracle data expired and refresh failed.",
        })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    expect(within(card()).getByRole("heading", { name: "UNAVAILABLE" })).toBeInTheDocument()
    expect(card()).toHaveAttribute("data-tone", "unavailable")
    expect(card().className).not.toMatch(/safe|stay/)
    expect(
      within(card()).getByText("Critical data is missing or too stale for a defensible decision. This is not a recommendation to stay."),
    ).toBeInTheDocument()
  })

  it("says explicitly when exit is recommended but full exit is unavailable", () => {
    render(
      <RecommendationCard
        analysis={withRecommendation({}, { withdrawable: false, warnings: ["Only 40% is withdrawable now."] })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    expect(
      within(card()).getByText("Exit recommended, but full exit is currently unavailable"),
    ).toBeInTheDocument()
    expect(within(card()).getByText("Only 40% is withdrawable now.")).toBeInTheDocument()
  })

  it("shows the exit-cost breakdown and quote metadata", () => {
    render(
      <RecommendationCard analysis={analysisResponse} numeraire="WETH" onSelectDependency={() => {}} />,
    )

    const breakdown = within(card()).getByRole("table", { name: "Exit cost breakdown" })
    expect(within(breakdown).getByText("Gas")).toBeInTheDocument()
    expect(within(breakdown).getByText("0.002 WETH")).toBeInTheDocument()
    expect(within(card()).getByText("Route: wstETH → WETH · quote block 23500000 · 4 s old")).toBeInTheDocument()
  })
})
