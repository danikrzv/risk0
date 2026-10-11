import "@testing-library/jest-dom/vitest"

import type { RadarEntity } from "@risk0/contracts"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { HealthJustification } from "./health-justification"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const market: RadarEntity = {
  entity_id: "market:1:0xabc",
  entity_type: "market",
  chain_id: 1,
  address_or_key: "0xabc",
  label: "wstETH / WETH 96.5%",
  source: "fixture:radar",
  catalog: true,
  visible: true,
  metadata: { health_status: "WARNING", health_evaluated_at: "2026-10-10T13:00:00Z" },
}

function warningResponse() {
  return {
    schema_version: "1.0.0",
    entity_id: market.entity_id,
    generated_at: "2026-10-10T13:05:00Z",
    health: {
      entity_id: market.entity_id,
      status: "WARNING",
      score: "0.5",
      reasons: ["utilization exceeds warning threshold"],
      missing_fields: [],
      evaluated_at: "2026-10-10T13:00:00Z",
      rule_version: "radar-health-v1",
      derivation: {
        entity_type: "market",
        freshness: "fresh",
        checks: [
          {
            stage: "metrics", rule: "Utilization at or above the warning threshold", metric: "utilization",
            observed: "0.93", note: null, operator: ">=", threshold: "0.90", outcome: "breach",
            leads_to: "WARNING", decisive: true,
          },
        ],
        dependencies: null,
        raw_status: "WARNING",
        raw_reasons: ["utilization exceeds warning threshold"],
        hysteresis: {
          previous_status: "WARNING", raw_status: "WARNING", rule: "already_abnormal", consecutive_abnormal: 0,
          consecutive_normal: 0, counter: null, cycles_required: null, held: false, final_status: "WARNING",
        },
        final_status: "WARNING",
        score: "0.5",
        score_mapping: { HEALTHY: "1", WARNING: "0.5", CRITICAL: "0", UNKNOWN: null },
        rule_version: "radar-health-v1",
      },
    },
  }
}

describe("HealthJustification", () => {
  it("loads and explains the recorded health computation only when opened", async () => {
    const fetchMock = vi.fn(async () => Response.json(warningResponse()))
    vi.stubGlobal("fetch", fetchMock)
    render(<HealthJustification entity={market} status="WARNING" labelFor={(id) => id} />)

    expect(fetchMock).not.toHaveBeenCalled()
    const details = screen.getByText("Why WARNING?").parentElement as HTMLDetailsElement
    details.open = true
    fireEvent(details, new Event("toggle"))

    expect(await screen.findByText(/WARNING because utilization 0\.93 ≥ 0\.90/)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith("/api/graph/health?entity_id=market%3A1%3A0xabc", expect.anything())
  })

  it("explains UNKNOWN without a request when the entry is not on the radar", () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    render(<HealthJustification entity={undefined} status="UNKNOWN" labelFor={(id) => id} />)

    const details = screen.getByText("Why UNKNOWN?").parentElement as HTMLDetailsElement
    details.open = true
    fireEvent(details, new Event("toggle"))

    expect(screen.getByText(/not in the radar topology/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
