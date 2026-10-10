import "@testing-library/jest-dom/vitest"

import type { Incident, RadarEntity } from "@risk0/contracts"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { IncidentRail } from "./incident-rail"

afterEach(cleanup)

const entities: RadarEntity[] = [
  {
    entity_id: "oracle:1:usdc-usd",
    entity_type: "oracle",
    chain_id: 1,
    address_or_key: "usdc-usd",
    label: "USDC / USD Oracle",
    source: "fixture:oracle",
    catalog: false,
    visible: true,
    metadata: {},
  },
]

const incidents: Incident[] = [
  {
    incident_id: "incident:critical",
    source_entity_id: "oracle:1:usdc-usd",
    severity: "CRITICAL",
    status: "OPEN",
    trigger_metrics: { deviation: "0.08" },
    rule_version: "oracle-health-v1",
    first_observed_at: "2026-10-10T12:05:00Z",
    last_observed_at: "2026-10-10T12:10:00Z",
    resolved_at: null,
    reasons: ["Deviation exceeded the critical threshold."],
  },
  {
    incident_id: "incident:resolved",
    source_entity_id: "oracle:1:usdc-usd",
    severity: "WARNING",
    status: "RESOLVED",
    trigger_metrics: {},
    rule_version: "oracle-health-v1",
    first_observed_at: "2026-10-10T10:00:00Z",
    last_observed_at: "2026-10-10T10:05:00Z",
    resolved_at: "2026-10-10T10:10:00Z",
    reasons: ["Recovered."],
  },
]

describe("IncidentRail", () => {
  it("lists only active incidents with source, severity, and reasons", () => {
    render(
      <IncidentRail incidents={incidents} entities={entities} selectedIncidentId={null} onSelectIncident={() => {}} />,
    )

    const list = screen.getByRole("list", { name: "Active incidents" })
    expect(within(list).getByText("USDC / USD Oracle")).toBeInTheDocument()
    expect(within(list).getByText("CRITICAL")).toBeInTheDocument()
    expect(within(list).getByText("Deviation exceeded the critical threshold.")).toBeInTheDocument()
    expect(within(list).queryByText("Recovered.")).not.toBeInTheDocument()
  })

  it("selects an incident and exposes the current selection accessibly", () => {
    const onSelectIncident = vi.fn()
    render(
      <IncidentRail
        incidents={incidents}
        entities={entities}
        selectedIncidentId="incident:critical"
        onSelectIncident={onSelectIncident}
      />,
    )

    const button = screen.getByRole("button", { name: /USDC \/ USD Oracle.*CRITICAL/ })
    expect(button).toHaveAttribute("aria-pressed", "true")
    fireEvent.click(button)
    expect(onSelectIncident).toHaveBeenCalledWith("incident:critical")
  })

  it("renders a calm empty state when there are no active incidents", () => {
    render(
      <IncidentRail
        incidents={incidents.filter((incident) => incident.status === "RESOLVED")}
        entities={entities}
        selectedIncidentId={null}
        onSelectIncident={() => {}}
      />,
    )

    expect(screen.getByText("No active incidents.")).toBeInTheDocument()
  })

  it("caps a large incident list and reveals more on request", () => {
    const many = Array.from({ length: 120 }, (_, index) => ({ ...incidents[0], incident_id: `incident:${index}` }))
    render(<IncidentRail incidents={many} entities={entities} selectedIncidentId={null} onSelectIncident={() => {}} />)

    const list = screen.getByRole("list", { name: "Active incidents" })
    expect(within(list).getAllByRole("button")).toHaveLength(50)
    fireEvent.click(screen.getByRole("button", { name: "Show 50 more incidents" }))
    expect(within(list).getAllByRole("button")).toHaveLength(100)
  })
})
