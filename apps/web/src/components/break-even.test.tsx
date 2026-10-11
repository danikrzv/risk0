import type { BreakEven, BreakEvenPoint } from "@risk0/contracts"
import { describe, expect, it } from "vitest"

import { breakEvenCostText, breakEvenSummary, formatProbability } from "./break-even"

const point: BreakEvenPoint = {
  scenario_id: "collateral-shock",
  dependency: "asset:1:weth",
  impact: "100",
  other_expected_loss: "0",
  break_even_probability: "0.05",
  decision_flip_probability: "0.05",
  current_probability: "0.02",
  current_probability_low: "0.01",
  current_probability_high: "0.03",
  reasons: [],
}

function view(overrides: Partial<BreakEvenPoint> = {}, top: Partial<BreakEven> = {}): BreakEven {
  return {
    probability_dependent: true,
    dominant_scenario_id: "collateral-shock",
    exit_cost: "5",
    points: [{ ...point, ...overrides }],
    explanation: "",
    ...top,
  }
}

describe("formatProbability", () => {
  it("keeps small probabilities readable without trailing noise", () => {
    expect(formatProbability("0")).toBe("0%")
    expect(formatProbability("0.25")).toBe("25.00%")
    expect(formatProbability("0.009")).toBe("0.90%")
    expect(formatProbability("0.000123")).toBe("0.0123%")
    expect(formatProbability("0.000003")).toBe("0.0003%")
    expect(formatProbability("-0.0004")).toBe("-0.04%")
  })
})

describe("breakEvenSummary", () => {
  it("states where EXIT becomes optimal against the current estimate", () => {
    expect(breakEvenSummary(view())).toBe(
      "EXIT becomes optimal above 5.00% probability of Collateral shock; current estimate 2.00%.",
    )
  })

  it("uses the decision flip point when other scenarios already carry expected loss", () => {
    expect(breakEvenSummary(view({ decision_flip_probability: "0.04" }))).toContain("above 4.00% probability")
  })

  it("says when no probability or any probability makes EXIT optimal", () => {
    expect(breakEvenSummary(view({ decision_flip_probability: "1", break_even_probability: "1" }))).toBe(
      "No probability of Collateral shock makes EXIT cheaper than staying; current estimate 2.00%.",
    )
    expect(breakEvenSummary(view({ decision_flip_probability: "0" }))).toContain("EXIT is optimal at any non-zero probability")
  })

  it("never fabricates a threshold or an estimate", () => {
    const summary = breakEvenSummary(
      view({
        break_even_probability: null,
        decision_flip_probability: null,
        current_probability: null,
        reasons: ["the exit cost is unknown (no current exit estimate)"],
      }),
    )
    expect(summary).toBe(
      "Break-even unavailable for Collateral shock: the exit cost is unknown (no current exit estimate).",
    )
    expect(breakEvenSummary(view({ current_probability: null }))).toContain("current estimate unavailable")
  })

  it("explains that a hard trigger does not depend on probability", () => {
    expect(breakEvenSummary(view({}, { probability_dependent: false }))).toBe(
      "A hard safety trigger fired: the decision does not depend on any probability.",
    )
  })

  it("handles books without a break-even view or without impact", () => {
    expect(breakEvenSummary(null)).toBeNull()
    expect(breakEvenSummary(undefined)).toBeNull()
    expect(breakEvenSummary(view({}, { dominant_scenario_id: null, points: [] }))).toContain("no break-even probability")
  })
})

describe("breakEvenCostText", () => {
  it("states the exit cost plus forgone yield the thresholds use", () => {
    expect(breakEvenCostText(view({}, { forgone_yield: "300", cost_of_exiting: "305" }), "USDC")).toBe(
      "Break-even against exit cost 5 USDC + forgone yield 300 USDC = 305 USDC.",
    )
  })

  it("is absent without carry, with an unknown exit cost, or when a hard trigger fired", () => {
    expect(breakEvenCostText(view(), "USDC")).toBeNull()
    expect(breakEvenCostText(view({}, { exit_cost: null, forgone_yield: "300", cost_of_exiting: null }), "USDC")).toBeNull()
    expect(
      breakEvenCostText(
        view({}, { probability_dependent: false, forgone_yield: "300", cost_of_exiting: "305" }),
        "USDC",
      ),
    ).toBeNull()
  })
})
