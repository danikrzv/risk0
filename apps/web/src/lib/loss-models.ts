import type { AnalysisResponse, MetricProvenance, MetricValue, RiskGraph, RiskNode, ScenarioResult } from "@risk0/contracts"

/** The scenario model's view of a lender's collateral-price loss (it drives STAY/EXIT). */
export interface ScenarioModelView {
  collateralShock: ScenarioResult | null
  /** p × impact for the collateral shock, as a decimal string, or null when unknown. */
  collateralContribution: string | null
  expectedLoss: string
  decision: AnalysisResponse["recommendation"]["decision"]
}

export interface SolvencyMarketRow {
  id: string
  label: string
  scenario: string | null
  borrowersInZone: MetricValue
  q: MetricValue
  qBound: boolean
  r: MetricValue
  qOverHalfR: MetricValue
  pContract: MetricValue
  pReal: MetricValue
  priceDeviation: MetricValue
  priceSources: string | null
  debtIssued: MetricValue
  recoveredValue: MetricValue
  badDebt: MetricValue
  supplierLossFraction: MetricValue
  missing: string | null
  /** The vault's share of this market's supply (edge loss share), when held through a vault. */
  vaultShare: MetricValue
}

export interface SolvencyView {
  model: string | null
  scenario: string | null
  loss: MetricValue
  wPool: MetricValue
  vulnerable: boolean | null
  rPanic: MetricValue
  vault: { label: string; badDebt: MetricValue; badDebtShare: MetricValue } | null
  markets: SolvencyMarketRow[]
  sources: Array<{ metric: string; node: string; provenance: MetricProvenance }>
}

const PREFIX = "solvency_"

function metric(node: RiskNode | undefined, name: string): MetricValue {
  return node?.metrics[`${PREFIX}${name}`] ?? null
}

function text(value: MetricValue): string | null {
  return typeof value === "string" && value.length > 0 ? value : null
}

export function scenarioModelView(analysis: AnalysisResponse): ScenarioModelView {
  const collateralShock = analysis.scenario_results.find((result) => result.scenario_id === "collateral-shock") ?? null
  const central = collateralShock?.probability.central ?? null
  const contribution = collateralShock && central !== null
    ? String(Number(central) * Number(collateralShock.impact))
    : null
  return {
    collateralShock,
    collateralContribution: contribution,
    expectedLoss: analysis.recommendation.expected_loss,
    decision: analysis.recommendation.decision,
  }
}

/** Reads the solvency model's annotations (`solvency_*` metrics, edge loss shares) off the graph. */
export function solvencyView(graph: RiskGraph): SolvencyView | null {
  const nodes = new Map(graph.nodes.map((node) => [node.node_id, node]))
  const position = graph.nodes.find((node) => node.node_type === "position" && `${PREFIX}model` in node.metrics)
  if (!position) return null
  const target = (source: string, relationship: string, type: RiskNode["node_type"]) => graph.edges
    .filter((edge) => edge.source === source && edge.relationship === relationship)
    .map((edge) => nodes.get(edge.target))
    .find((node) => node?.node_type === type)
  const vaultNode = graph.nodes.find((node) => node.node_type === "vault" && `${PREFIX}scenario` in node.metrics)
  const vaultShares = new Map(
    vaultNode
      ? graph.edges
        .filter((edge) => edge.source === vaultNode.node_id && edge.relationship === "allocated_to")
        .map((edge) => [edge.target, edge.loss_share ?? null] as const)
      : [],
  )
  const markets = graph.nodes
    .filter((node) => node.node_type === "market" && `${PREFIX}scenario` in node.metrics)
    .map((market): SolvencyMarketRow => {
      const oracle = target(market.node_id, "uses_oracle", "oracle")
      const pool = target(market.node_id, "exits_through", "pool")
      return {
        id: market.node_id,
        label: market.label,
        scenario: text(metric(market, "scenario")),
        borrowersInZone: metric(market, "borrowers_in_zone"),
        q: metric(market, "q"),
        qBound: metric(market, "q_bound") === true,
        r: metric(pool, "r"),
        qOverHalfR: metric(market, "q_over_half_r"),
        pContract: metric(oracle, "p_contract"),
        pReal: metric(oracle, "p_real"),
        priceDeviation: metric(oracle, "price_deviation"),
        priceSources: text(metric(oracle, "price_sources")),
        debtIssued: metric(market, "debt_issued"),
        recoveredValue: metric(market, "recovered_value"),
        badDebt: metric(market, "bad_debt"),
        supplierLossFraction: metric(market, "supplier_loss_fraction"),
        missing: text(metric(market, "missing")),
        vaultShare: vaultShares.get(market.node_id) ?? null,
      }
    })
    // The markets that drive the loss first.
    .sort((left, right) => Number(right.supplierLossFraction ?? 0) - Number(left.supplierLossFraction ?? 0))
  const sources = graph.nodes.flatMap((node) => Object.entries(node.metric_provenance ?? {})
    .filter(([name]) => name.startsWith(PREFIX))
    .map(([name, provenance]) => ({ metric: name.slice(PREFIX.length), node: node.label, provenance })))
  const vulnerable = metric(position, "vulnerable")
  return {
    model: text(metric(position, "model")),
    scenario: text(metric(position, "scenario")),
    loss: metric(position, "expected_loss"),
    wPool: metric(position, "w_pool"),
    vulnerable: typeof vulnerable === "boolean" ? vulnerable : null,
    rPanic: metric(position, "r_panic"),
    vault: vaultNode
      ? { label: vaultNode.label, badDebt: metric(vaultNode, "bad_debt"), badDebtShare: metric(vaultNode, "bad_debt_share") }
      : null,
    markets,
    sources,
  }
}
