import "@testing-library/jest-dom/vitest"

import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { analysisResponse } from "../test-fixtures"
import {
  DEFAULT_SCENARIOS,
  ScenarioPanel,
  ScenarioResults,
  defaultScenarioDrafts,
  toScenarioSelections,
} from "./scenario-panel"

afterEach(cleanup)

describe("ScenarioPanel", () => {
  it("mirrors the version-controlled default scenario set", () => {
    const file = JSON.parse(
      readFileSync(resolve(process.cwd(), "../../data/scenarios/default.json"), "utf8"),
    ) as { scenarios: { scenario_id: string; name: string; severity: string; threshold: string }[] }

    expect(
      DEFAULT_SCENARIOS.map(({ scenario_id, name, severity, threshold }) => ({
        scenario_id,
        name,
        severity,
        threshold,
      })),
    ).toEqual(
      file.scenarios.map(({ scenario_id, name, severity, threshold }) => ({
        scenario_id,
        name,
        severity,
        threshold,
      })),
    )
  })

  it("renders all five scenarios enabled with default severities", () => {
    render(<ScenarioPanel drafts={defaultScenarioDrafts()} onChange={() => {}} />)

    expect(screen.getAllByRole("checkbox")).toHaveLength(5)
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).toBeChecked()
    expect(screen.getByLabelText("Collateral shock severity (%)")).toHaveValue("15")
    expect(screen.getByLabelText("Protocol impairment severity (%)")).toHaveValue("10")
    expect(screen.getByText("wstETH, WETH, or WBTC falls 15% over 24 hours")).toBeInTheDocument()
  })

  it("edits severity and toggles scenarios", () => {
    const onChange = vi.fn()
    render(<ScenarioPanel drafts={defaultScenarioDrafts()} onChange={onChange} />)

    fireEvent.change(screen.getByLabelText("Collateral shock severity (%)"), {
      target: { value: "25" },
    })
    expect(onChange.mock.calls[0][0].find((d: { scenario_id: string }) => d.scenario_id === "collateral-shock").severity).toBe("25")

    fireEvent.click(screen.getByRole("checkbox", { name: "Oracle failure" }))
    expect(onChange.mock.calls[1][0].find((d: { scenario_id: string }) => d.scenario_id === "oracle-failure").enabled).toBe(false)
  })

  it("converts drafts to contract selections and validates severity bounds", () => {
    const drafts = defaultScenarioDrafts()
    expect(toScenarioSelections(drafts)).toEqual({
      selections: [
        { scenario_id: "stablecoin-depeg", severity: "0.1" },
        { scenario_id: "collateral-shock", severity: "0.15" },
        { scenario_id: "oracle-failure", severity: "0.05" },
        { scenario_id: "liquidity-collapse", severity: "0.8" },
        { scenario_id: "protocol-impairment", severity: "0.1" },
      ],
      errors: {},
    })

    const invalid = drafts.map((draft) =>
      draft.scenario_id === "collateral-shock"
        ? { ...draft, severity: "120" }
        : draft.scenario_id === "oracle-failure"
          ? { ...draft, severity: "0" }
          : draft,
    )
    expect(toScenarioSelections(invalid).errors).toEqual({
      "collateral-shock": "Enter a severity above 0% and at most 100%",
      "oracle-failure": "Enter a severity above 0% and at most 100%",
    })

    const none = drafts.map((draft) => ({ ...draft, enabled: false }))
    expect(toScenarioSelections(none).errors).toEqual({ _form: "Select at least one scenario" })
  })

  it("shows validation messages next to the scenario", () => {
    render(
      <ScenarioPanel
        drafts={defaultScenarioDrafts()}
        onChange={() => {}}
        errors={{ "collateral-shock": "Enter a severity above 0% and at most 100%" }}
      />,
    )

    expect(screen.getByLabelText("Collateral shock severity (%)")).toHaveAccessibleDescription(
      "Enter a severity above 0% and at most 100%",
    )
  })
})

describe("ScenarioResults", () => {
  it("shows impact, probability method, triggers, and affected path", () => {
    render(
      <ScenarioResults
        results={analysisResponse.scenario_results}
        graph={analysisResponse.graph}
        numeraire="WETH"
      />,
    )

    const shock = screen.getByRole("article", { name: "Collateral shock" })
    expect(within(shock).getByText("1.25 WETH")).toBeInTheDocument()
    expect(within(shock).getByText("Empirical")).toBeInTheDocument()
    expect(within(shock).getByText("2.00% (1.00%–4.00%)")).toBeInTheDocument()
    expect(within(shock).getByText("liquidation")).toBeInTheDocument()
    expect(within(shock).getByText("wstETH → wstETH / WETH market → Borrower position")).toBeInTheDocument()

    const protocol = screen.getByRole("article", { name: "Protocol impairment" })
    expect(within(protocol).getByText("Model-estimated · protocol-evidence-v1")).toBeInTheDocument()
    const breakdown = within(protocol).getByRole("region", { name: "Model breakdown" })
    expect(breakdown).toHaveTextContent("demonstration heuristic")
    expect(within(shock).queryByRole("region", { name: "Model breakdown" })).not.toBeInTheDocument()
  })
})
