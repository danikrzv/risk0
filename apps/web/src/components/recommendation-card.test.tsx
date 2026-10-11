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

  it("states the break-even probability next to the current estimate", () => {
    const breakEven = {
      probability_dependent: true,
      dominant_scenario_id: "collateral-shock",
      exit_cost: "0.122",
      points: [
        {
          scenario_id: "collateral-shock",
          dependency: "asset:1:wsteth",
          impact: "1.2",
          other_expected_loss: "0.0027",
          break_even_probability: "0.101666666666666667",
          decision_flip_probability: "0.099416666666666667",
          current_probability: "0.02",
          current_probability_low: "0.01",
          current_probability_high: "0.04",
          reasons: [],
        },
      ],
      explanation: "",
    }
    const { rerender } = render(
      <RecommendationCard
        analysis={withRecommendation({ decision: "STAY", triggers: [], break_even: breakEven })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(comparison).toHaveTextContent(
      "EXIT becomes optimal above 9.94% probability of Collateral shock; current estimate 2.00%.",
    )

    rerender(
      <RecommendationCard
        analysis={withRecommendation({ break_even: { ...breakEven, probability_dependent: false } })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )
    expect(comparison).toHaveTextContent("A hard safety trigger fired: the decision does not depend on any probability.")
  })

  it("compares the expected loss with exit cost plus forgone yield", () => {
    const carry = {
      status: "known" as const,
      apy: "0.045",
      apy_source: "Morpho market supply APY (net when reported)",
      horizon_days: 30,
      notes: ["Assumes exited capital is held idle (no alternative yield)."],
    }
    render(
      <RecommendationCard
        analysis={withRecommendation({
          decision: "STAY",
          triggers: [],
          expected_loss: "66",
          exit_cost: "0.02",
          forgone_yield: "369.863013698630136986",
          cost_of_exiting: "369.883013698630136986",
          carry,
          break_even: {
            probability_dependent: true,
            dominant_scenario_id: "protocol-impairment",
            exit_cost: "0.02",
            forgone_yield: "369.863013698630136986",
            cost_of_exiting: "369.883013698630136986",
            points: [
              {
                scenario_id: "protocol-impairment",
                dependency: "protocol:morpho",
                impact: "30000",
                other_expected_loss: "0",
                break_even_probability: "0.012329433789954338",
                decision_flip_probability: "0.012329433789954338",
                current_probability: "0.0022",
                current_probability_low: "0.001",
                current_probability_high: "0.004",
                reasons: [],
              },
            ],
            explanation: "",
          },
        })}
        numeraire="USDC"
        onSelectDependency={() => {}}
      />,
    )

    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(
      within(comparison).getByText(
        "Expected 30-day loss 66 USDC vs exit cost 0.02 USDC + forgone yield 369.863014 USDC (APY 4.50%)",
      ),
    ).toBeInTheDocument()
    expect(within(comparison).getByText("Forgone 30-day yield")).toBeInTheDocument()
    expect(within(comparison).getByText("Exit cost plus forgone yield")).toBeInTheDocument()
    expect(within(comparison).getByText("369.883014 USDC")).toBeInTheDocument()
    // 66 < 0.02 + 369.86: the forgone yield keeps the position, though 66 > 0.02.
    expect(within(comparison).getByText("Expected 30-day loss does not exceed the cost of exiting now.")).toBeInTheDocument()
    expect(comparison).toHaveTextContent(
      "EXIT becomes optimal above 1.23% probability of Protocol impairment; current estimate 0.22%. Break-even against exit cost 0.02 USDC + forgone yield 369.863014 USDC = 369.883014 USDC.",
    )
    expect(within(comparison).getByText("Assumes exited capital is held idle (no alternative yield).")).toBeInTheDocument()
    expect(within(card()).queryByRole("list", { name: "Yield warnings" })).not.toBeInTheDocument()
  })

  it("warns when the APY is unknown and says when borrower carry is not modelled", () => {
    const unknownNote = "The position APY is unknown: the forgone yield is counted as zero, which biases toward EXIT."
    const { rerender } = render(
      <RecommendationCard
        analysis={withRecommendation({
          forgone_yield: "0",
          cost_of_exiting: "0.122",
          carry: { status: "unknown", apy: null, apy_source: null, horizon_days: 30, notes: [unknownNote] },
        })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )

    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(comparison).toHaveTextContent("+ forgone yield 0 WETH (APY unknown, counted as zero)")
    expect(within(comparison).getByRole("list", { name: "Yield warnings" })).toHaveTextContent(unknownNote)

    rerender(
      <RecommendationCard
        analysis={withRecommendation({
          forgone_yield: "0",
          cost_of_exiting: "0.122",
          carry: {
            status: "not_applicable",
            apy: null,
            apy_source: null,
            horizon_days: 30,
            notes: ["Borrower carry (borrow cost versus collateral yield) is not modelled: the forgone yield is zero."],
          },
        })}
        numeraire="WETH"
        onSelectDependency={() => {}}
      />,
    )
    expect(comparison).toHaveTextContent("+ forgone yield 0 WETH (borrower carry not modelled)")
    expect(comparison).toHaveTextContent("Borrower carry (borrow cost versus collateral yield) is not modelled")
  })

  it("keeps the exit-cost-only comparison for responses without carry", () => {
    render(<RecommendationCard analysis={analysisResponse} numeraire="WETH" onSelectDependency={() => {}} />)

    const comparison = within(card()).getByRole("group", { name: "Stay versus exit" })
    expect(within(comparison).queryByText("Forgone 30-day yield")).not.toBeInTheDocument()
    expect(comparison).not.toHaveTextContent("forgone yield")
  })
})
