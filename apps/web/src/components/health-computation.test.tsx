import "@testing-library/jest-dom/vitest"

import type {
  EntityHealthResponse,
  HealthCheck,
  HealthDerivation,
  HealthState,
} from "@risk0/contracts"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { HealthComputation } from "./health-computation"

afterEach(cleanup)

const MARKET = "market:1:weth-usdc"
const EVALUATED_AT = "2026-10-10T12:05:00Z"

function check(overrides: Partial<HealthCheck>): HealthCheck {
  return {
    stage: "metrics",
    rule: "rule",
    metric: null,
    observed: null,
    note: null,
    operator: null,
    threshold: null,
    outcome: "within",
    leads_to: null,
    decisive: false,
    ...overrides,
  }
}

const freshness = check({
  stage: "freshness",
  rule: "An expired snapshot cannot be evaluated",
  metric: "freshness",
  note: "fresh",
  leads_to: "UNKNOWN",
})

function marketChecks(decisiveUtilization: boolean, hysteresis: HealthCheck): HealthCheck[] {
  return [
    freshness,
    check({ rule: "Bad debt is recorded", metric: "bad_debt", observed: "0", operator: ">", threshold: "0", leads_to: "CRITICAL" }),
    check({ rule: "Utilization at or above the critical threshold", metric: "utilization", observed: "0.93", operator: ">=", threshold: "0.98", leads_to: "CRITICAL" }),
    check({ rule: "Near-liquidation debt at or above the critical threshold", metric: "near_liquidation_debt_ratio", observed: "0.02", operator: ">=", threshold: "0.30", leads_to: "CRITICAL" }),
    check({ rule: "Required market metrics are missing", metric: "missing_fields", observed: "0", operator: ">", threshold: "0", leads_to: "UNKNOWN" }),
    check({
      rule: "Utilization at or above the warning threshold",
      metric: "utilization",
      observed: "0.93",
      operator: ">=",
      threshold: "0.90",
      outcome: "breach",
      leads_to: "WARNING",
      decisive: decisiveUtilization,
    }),
    check({ rule: "Available liquidity is exhausted", metric: "available_liquidity", observed: "18500000", operator: "<=", threshold: "0", leads_to: "WARNING" }),
    hysteresis,
  ]
}

function derivation(overrides: Partial<HealthDerivation>): HealthDerivation {
  return {
    entity_type: "market",
    freshness: "fresh",
    checks: [],
    dependencies: null,
    raw_status: "WARNING",
    raw_reasons: ["utilization exceeds warning threshold"],
    hysteresis: {
      previous_status: "WARNING",
      raw_status: "WARNING",
      rule: "already_abnormal",
      consecutive_abnormal: 0,
      consecutive_normal: 0,
      counter: null,
      cycles_required: null,
      held: false,
      final_status: "WARNING",
    },
    final_status: "WARNING",
    score: "0.5",
    score_mapping: { HEALTHY: "1", WARNING: "0.5", CRITICAL: "0", UNKNOWN: null },
    rule_version: "radar-health-v1",
    ...overrides,
  }
}

const confirmedWarning = derivation({
  checks: marketChecks(true, check({
    stage: "hysteresis",
    rule: "WARNING needs 2 consecutive abnormal evaluations",
    metric: "consecutive_abnormal",
    observed: "2",
    operator: ">=",
    threshold: "2",
    outcome: "info",
    leads_to: "WARNING",
  })),
  hysteresis: {
    previous_status: "HEALTHY",
    raw_status: "WARNING",
    rule: "warning_confirmation",
    consecutive_abnormal: 1,
    consecutive_normal: 0,
    counter: 2,
    cycles_required: 2,
    held: false,
    final_status: "WARNING",
  },
})

const pendingWarning = derivation({
  checks: marketChecks(true, check({
    stage: "hysteresis",
    rule: "WARNING needs 2 consecutive abnormal evaluations",
    metric: "consecutive_abnormal",
    observed: "1",
    operator: "<",
    threshold: "2",
    outcome: "info",
    leads_to: "HEALTHY",
    decisive: true,
  })),
  hysteresis: {
    previous_status: null,
    raw_status: "WARNING",
    rule: "warning_confirmation",
    consecutive_abnormal: 0,
    consecutive_normal: 0,
    counter: 1,
    cycles_required: 2,
    held: true,
    final_status: "HEALTHY",
  },
  final_status: "HEALTHY",
  score: "1",
})

const weightedVault = derivation({
  entity_type: "vault",
  checks: [
    check({ stage: "dependencies", rule: "CRITICAL markets hold a material allocation", metric: "critical_allocation", observed: "0.02", operator: ">=", threshold: "0.05", leads_to: "CRITICAL" }),
    check({ stage: "dependencies", rule: "UNKNOWN markets hold more than a material allocation", metric: "unknown_allocation", observed: "0", operator: ">", threshold: "0.05", leads_to: "UNKNOWN" }),
    check({
      stage: "dependencies",
      rule: "CRITICAL markets hold a minor allocation",
      metric: "critical_allocation",
      observed: "0.02",
      operator: ">",
      threshold: "0",
      outcome: "breach",
      leads_to: "WARNING",
      decisive: true,
    }),
  ],
  dependencies: {
    dependencies: [
      { entity_id: MARKET, status: "HEALTHY", counted: "weighted", weight: "0.98" },
      { entity_id: "market:1:small", status: "CRITICAL", counted: "weighted", weight: "0.02" },
      { entity_id: "asset:1:usdc", status: "HEALTHY", counted: "full", weight: null },
    ],
    weighted_totals: { HEALTHY: "0.98", WARNING: "0", CRITICAL: "0.02", UNKNOWN: "0" },
    material_allocation: "0.05",
    status: "WARNING",
    reason: "critical market holds a minor allocation",
  },
})

function state(overrides: Partial<HealthState>): HealthState {
  return {
    entity_id: MARKET,
    status: "WARNING",
    score: "0.5",
    reasons: ["utilization exceeds warning threshold"],
    missing_fields: [],
    evaluated_at: EVALUATED_AT,
    rule_version: "radar-health-v1",
    derivation: confirmedWarning,
    ...overrides,
  }
}

function response(health: HealthState | null): EntityHealthResponse {
  return { schema_version: "1.0.0", entity_id: MARKET, health, generated_at: "2026-10-10T12:06:00Z" }
}

const labels: Record<string, string> = { [MARKET]: "WETH / USDC 86%", "market:1:small": "Small market" }
const labelFor = (id: string) => labels[id] ?? id

function renderSection(
  load: (entityId: string) => Promise<EntityHealthResponse>,
  props: Partial<{ evaluatedAt: string | null; displayedStatus: "HEALTHY" | "WARNING" | "CRITICAL" | "UNKNOWN" }> = {},
) {
  return render(
    <HealthComputation
      entityId={MARKET}
      evaluatedAt={props.evaluatedAt ?? EVALUATED_AT}
      displayedStatus={props.displayedStatus ?? "WARNING"}
      labelFor={labelFor}
      load={load}
    />,
  )
}

function section() {
  return screen.getByRole("region", { name: "Health computation" })
}

describe("HealthComputation", () => {
  it("shows the final state, its score, and the sentence naming the decisive rule", async () => {
    const load = vi.fn(async () => response(state({})))
    renderSection(load)

    expect(screen.getByRole("status")).toHaveTextContent("Loading health computation")
    expect(await screen.findByText("score 0.5 = WARNING")).toBeInTheDocument()
    expect(load).toHaveBeenCalledWith(MARKET)
    expect(
      within(section()).getByText(
        "WARNING because utilization 0.93 ≥ 0.90 (warning threshold); below the 0.98 critical threshold.",
      ),
    ).toBeInTheDocument()
    expect(within(section()).getByText("Score mapping: HEALTHY 1 · WARNING 0.5 · CRITICAL 0 · UNKNOWN none")).toBeInTheDocument()
    expect(within(section()).getByText("raw WARNING, confirmed 2/2 cycles → WARNING")).toBeInTheDocument()
    expect(within(section()).getByText("2026-10-10 12:05:00 UTC")).toBeInTheDocument()
    expect(within(section()).getByText("radar-health-v1")).toBeInTheDocument()
  })

  it("lists every check in a captioned table and marks the decisive row in text", async () => {
    renderSection(async () => response(state({})))

    const table = await screen.findByRole("table", { name: /Checks in evaluation order/ })
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Check",
      "Value",
      "Rule",
      "Result",
    ])
    const rows = within(table).getAllByRole("row").slice(1)
    expect(rows).toHaveLength(8)
    const decisive = rows.filter((row) => within(row).queryByText("Decisive"))
    expect(decisive).toHaveLength(1)
    expect(decisive[0]).toHaveAttribute("data-decisive", "true")
    expect(decisive[0]).toHaveClass("health-computation__decisive")
    expect(within(decisive[0]).getByRole("rowheader")).toHaveTextContent("utilization")
    expect(decisive[0]).toHaveTextContent("Utilization at or above the warning threshold")
    expect(decisive[0]).toHaveTextContent("≥ 0.90 → WARNING")
    expect(decisive[0]).toHaveTextContent("Breach → WARNING")
    // Large values are grouped; within checks read "Within".
    const liquidity = rows.find((row) => row.textContent?.includes("available liquidity"))!
    expect(liquidity).toHaveTextContent("18,500,000")
    expect(liquidity).toHaveTextContent("Within")
  })

  it("explains a warning held by hysteresis", async () => {
    renderSection(
      async () => response(state({ status: "HEALTHY", score: "1", derivation: pendingWarning })),
      { displayedStatus: "HEALTHY" },
    )

    expect(await screen.findByText("score 1 = HEALTHY")).toBeInTheDocument()
    expect(
      screen.getByText(
        "HEALTHY: the raw WARNING (utilization 0.93 ≥ 0.90 (warning threshold); below the 0.98 critical threshold) is pending confirmation, 1/2 consecutive cycles.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByText("raw WARNING, pending 1/2 cycles → still HEALTHY")).toBeInTheDocument()
    const held = screen.getAllByRole("row").find((row) => row.textContent?.includes("Held → HEALTHY"))
    expect(held).toHaveAttribute("data-decisive", "true")
  })

  it("shows the dependency aggregation with weights, totals, and materiality", async () => {
    renderSection(async () => response(state({ derivation: weightedVault })))

    const table = await screen.findByRole("table", { name: "Dependency aggregation" })
    const rows = within(table).getAllByRole("row").slice(1)
    expect(rows.map((row) => row.textContent)).toEqual([
      "WETH / USDC 86%● HEALTHY98%",
      "Small market■ CRITICAL2%",
      "asset:1:usdc● HEALTHYfull (worst-of)",
    ])
    expect(
      screen.getByText(/Allocation by status: CRITICAL 2% · UNKNOWN 0% · WARNING 0% · HEALTHY 98% vs 5% material/),
    ).toHaveTextContent("→ WARNING (critical market holds a minor allocation)")
    expect(
      screen.getByText(
        "WARNING because critical allocation 2% > 0% (critical market holds a minor allocation); below the 5% critical threshold.",
      ),
    ).toBeInTheDocument()
  })

  it("says when an evaluation was recorded without a derivation", async () => {
    renderSection(async () => response(state({ derivation: null })))

    expect(
      await screen.findByText(
        "Health computation not recorded for this evaluation (evaluated 2026-10-10 12:05:00 UTC); it appears after the next radar cycle.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByText("score 0.5 = WARNING")).toBeInTheDocument()
    expect(screen.queryByRole("table")).not.toBeInTheDocument()
  })

  it("says when the entity has not been evaluated", async () => {
    renderSection(async () => response(null))

    expect(await screen.findByText(/Not evaluated yet/)).toBeInTheDocument()
  })

  it("notes when an incident overrides the displayed status", async () => {
    renderSection(async () => response(state({})), { displayedStatus: "CRITICAL" })

    expect(await screen.findByText(/The radar currently shows CRITICAL/)).toBeInTheDocument()
  })

  it("reports a failed load and retries on request", async () => {
    const load = vi
      .fn<(entityId: string) => Promise<EntityHealthResponse>>()
      .mockRejectedValueOnce(new Error("Risk service is busy — retrying"))
      .mockResolvedValueOnce(response(state({})))
    renderSection(load)

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Health computation unavailable: Risk service is busy — retrying",
    )
    fireEvent.click(screen.getByRole("button", { name: "Retry" }))
    expect(await screen.findByText("score 0.5 = WARNING")).toBeInTheDocument()
    expect(load).toHaveBeenCalledTimes(2)
  })

  it("refetches when the recorded evaluation changes and keeps the previous one meanwhile", async () => {
    let resolveSecond: (value: EntityHealthResponse) => void = () => {}
    const load = vi
      .fn<(entityId: string) => Promise<EntityHealthResponse>>()
      .mockResolvedValueOnce(response(state({})))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve }))
    const { rerender } = renderSection(load)
    expect(await screen.findByText("score 0.5 = WARNING")).toBeInTheDocument()

    rerender(
      <HealthComputation
        entityId={MARKET}
        evaluatedAt="2026-10-10T13:05:00Z"
        displayedStatus="WARNING"
        labelFor={labelFor}
        load={load}
      />,
    )

    expect(load).toHaveBeenCalledTimes(2)
    expect(screen.getByText("score 0.5 = WARNING")).toBeInTheDocument()
    resolveSecond(response(state({ status: "CRITICAL", score: "0", derivation: null, evaluated_at: "2026-10-10T13:05:00Z" })))
    await waitFor(() => expect(screen.getByText("score 0 = CRITICAL")).toBeInTheDocument())
  })
})
