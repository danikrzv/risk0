import "@testing-library/jest-dom/vitest"

import type { AnalysisResponse, RiskEdge, RiskNode } from "@risk0/contracts"
import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { analysisResponse } from "../test-fixtures"
import { solvencyView } from "../lib/loss-models"
import { LossModelComparison } from "./loss-model-comparison"

afterEach(cleanup)

const MARKET = "market:1:wsteth-weth-965"
const ORACLE = "oracle:1:0xoracle"
const POOL = "pool:1:wsteth-weth-exit"

/** The fixture graph as a lender position carrying the solvency model's annotations. */
function withSolvency(position: Record<string, unknown>, market: Record<string, unknown>): AnalysisResponse {
  const nodes: RiskNode[] = analysisResponse.graph.nodes.map((node) => {
    if (node.node_type === "position") {
      return { ...node, metrics: { ...node.metrics, ...position } } as RiskNode
    }
    if (node.node_id === MARKET) {
      return {
        ...node,
        metrics: { ...node.metrics, ...market },
        metric_provenance: {
          solvency_q: { kind: "data", source: "https://api.morpho.org/graphql", fetched_at: "2026-10-11T06:54:10Z", freshness: "fresh", block_number: null },
        },
      } as RiskNode
    }
    if (node.node_id === ORACLE) {
      return {
        ...node,
        metrics: { ...node.metrics, solvency_p_contract: "1.2460", solvency_p_real: "1.2458", solvency_price_deviation: "0.0001", solvency_price_sources: "morpho_oracle,uniswap_spot" },
      } as RiskNode
    }
    return node
  })
  nodes.push({
    node_id: POOL, node_type: "pool", label: "wstETH -> WETH collateral exit (Uniswap)",
    metrics: { solvency_r: "1618.4", solvency_route: "wstETH -> WETH" },
    provenance: "uniswap", freshness: "fresh", observed_at: "2026-10-11T06:54:10Z",
  } as RiskNode)
  const edges: RiskEdge[] = [
    ...analysisResponse.graph.edges,
    { source: MARKET, target: POOL, relationship: "exits_through", exposure_weight: null, loss_share: "1" } as RiskEdge,
  ]
  return { ...analysisResponse, graph: { ...analysisResponse.graph, nodes, edges } }
}

const bankRun = withSolvency(
  {
    solvency_model: "lender_bad_debt", solvency_scenario: "A_systemic", solvency_w_pool: "0.0215",
    solvency_expected_loss: "28.0", solvency_vulnerable: true, solvency_r_panic: "0.30",
  },
  {
    solvency_scenario: "A_systemic", solvency_q: "10642.38", solvency_q_bound: false, solvency_borrowers_in_zone: "32",
    solvency_q_over_half_r: "13.15", solvency_debt_issued: null, solvency_recovered_value: null,
    solvency_bad_debt: null, solvency_supplier_loss_fraction: "0.70", solvency_missing: null,
  },
)

describe("LossModelComparison", () => {
  it("shows the scenario model and the solvency model side by side", () => {
    render(<LossModelComparison analysis={bankRun} numeraire="WETH" />)

    const section = screen.getByRole("region", { name: "Two loss models" })
    const scenario = within(section).getByRole("region", { name: /Scenario model/ })
    expect(scenario).toHaveTextContent("drives STAY / EXIT")
    expect(scenario).toHaveTextContent("Expected 30-day loss, all scenarios")
    expect(scenario).toHaveTextContent(analysisResponse.recommendation.decision)

    const solvency = within(section).getByRole("region", { name: /Solvency model/ })
    expect(solvency).toHaveTextContent("Loss if liquidated now")
    expect(solvency).toHaveTextContent("28 WETH")
    expect(solvency).toHaveTextContent("A · systemic (bank run) · vulnerable")
    expect(solvency).toHaveTextContent("Bank-run recovery (assumption)30%")

    const row = within(within(solvency).getByRole("table")).getByRole("rowheader", { name: /wstETH/ }).closest("tr")!
    expect(row).toHaveTextContent("10,642.38 (32)")
    expect(row).toHaveTextContent("1,618.4")
    expect(row).toHaveTextContent("13.15")
    expect(row).toHaveTextContent("70.00%")
    // Branch A applies a fixed recovery: no bad-debt amount, labelled rather than a bare dash.
    expect(row).toHaveTextContent("n/a: fixed recovery")
    expect(section).toHaveTextContent("Only the scenario model feeds the STAY / EXIT decision.")
  })

  it("names missing inputs and never shows an unknown loss as zero", () => {
    const missing = withSolvency(
      { solvency_model: "lender_bad_debt", solvency_scenario: "unavailable", solvency_w_pool: null, solvency_expected_loss: null, solvency_vulnerable: null, solvency_r_panic: "0.30" },
      { solvency_scenario: "unavailable", solvency_q: "12", solvency_bad_debt: null, solvency_supplier_loss_fraction: null, solvency_missing: "R,P_real" },
    )
    render(<LossModelComparison analysis={missing} numeraire="WETH" />)

    const solvency = screen.getByRole("region", { name: /Solvency model/ })
    expect(solvency).toHaveTextContent("Loss if liquidated now—")
    expect(solvency).toHaveTextContent("missing: R,P_real")
    expect(solvency).toHaveTextContent("Unavailable")
  })

  it("says the solvency model does not apply to a borrower", () => {
    const borrower = withSolvency({ solvency_model: "not_applicable" }, {})
    render(<LossModelComparison analysis={borrower} numeraire="WETH" />)

    expect(screen.getByText(/Not applicable to a borrower position/)).toBeInTheDocument()
  })

  it("reads markets, their oracle prices and exit depth through the graph's edges", () => {
    const view = solvencyView(bankRun.graph)!
    expect(view.markets).toHaveLength(1)
    expect(view.markets[0]).toMatchObject({ r: "1618.4", pContract: "1.2460", pReal: "1.2458", supplierLossFraction: "0.70" })
    expect(view.sources).toContainEqual(expect.objectContaining({ metric: "q", provenance: expect.objectContaining({ kind: "data" }) }))
  })
})
