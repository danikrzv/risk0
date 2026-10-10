import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../test-fixtures"
import Home from "./page"

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub)
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} })
})

const radarGraph = {
  schema_version: "1.0.0",
  entities: [
    { entity_id: "vault:1:catalog", entity_type: "vault", chain_id: 1, address_or_key: "catalog", label: "Belem Sentinel USDC", source: "fixture:radar", catalog: true, visible: true, metadata: { health_status: "HEALTHY" } },
    { entity_id: "vault:1:peer", entity_type: "vault", chain_id: 1, address_or_key: "peer", label: "Institutional USDC Peer", source: "fixture:radar", catalog: false, visible: true, metadata: { health_status: "HEALTHY" } },
  ],
  relationships: [], similarities: [], active_incidents: [], generated_at: "2026-10-10T12:00:00Z",
}

function stubApi(analysis: Response | (() => Promise<Response>) = Response.json(analysisResponse)) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/catalog") return Response.json(catalogResponse)
    if (url === "/api/graph") return Response.json(radarGraph)
    if (url.startsWith("/api/notifications")) return Response.json({ schema_version: "1.0.0", notifications: [], next_cursor: null, generated_at: "2026-10-10T12:00:00Z" })
    if (url === "/api/exposures") return Response.json({ exposure_id: "exposure:test", entity_id: "vault:1:catalog", mode: "lender", amount: "100000", numeraire: "USDC", collateral_amount: null, debt_amount: null, ltv: null, created_at: "2026-10-10T12:01:00Z" }, { status: 201 })
    if (url === "/api/analysis") return typeof analysis === "function" ? analysis() : analysis
    throw new Error(`unexpected ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

describe("Home", () => {
  it("renders the Risk0 heading", () => {
    stubApi()
    render(<Home />)
    expect(screen.getByRole("heading", { name: "Risk0" })).toBeInTheDocument()
  })

  it("renders the healthy radar and records a manual exposure", async () => {
    const fetchMock = stubApi()
    render(<Home />)

    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Belem Sentinel USDC HEALTHY/ })).toBeInTheDocument()
    expect(screen.getByText("No active incidents.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/exposures")).toBe(true))
    expect(screen.getByRole("status")).toHaveTextContent("Exposure recorded")
  })

  it("runs the select → inputs → scenarios → result flow", async () => {
    const fetchMock = stubApi()
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    fireEvent.change(screen.getByLabelText("Collateral shock severity (%)"), { target: { value: "15" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    const recommendation = await screen.findByRole("region", { name: "Recommendation" })
    expect(within(recommendation).getByRole("heading", { name: "EXIT" })).toBeInTheDocument()

    const [, init] = fetchMock.mock.calls.find(([url]) => url === "/api/analysis") as unknown as [
      string,
      RequestInit,
    ]
    const body = JSON.parse(String(init.body))
    expect(body.position).toMatchObject({ mode: "borrower", entity_id: "wsteth-weth-965", ltv: "0.9" })
    expect(body.scenarios).toHaveLength(5)

    fireEvent.click(within(recommendation).getByRole("button", { name: "wstETH / WETH market" }))
    const detail = screen.getByRole("region", { name: "Selected dependency" })
    expect(within(detail).getByRole("heading", { name: "wstETH / WETH market" })).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Data quality" })).toBeInTheDocument()
  })

  it("drops an in-flight analysis when the entry changes before it resolves", async () => {
    let resolveAnalysis: (response: Response) => void = () => {}
    stubApi(() => new Promise<Response>((resolve) => (resolveAnalysis = resolve)))
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(screen.getByRole("button", { name: "Running analysis…" })).toBeDisabled()

    fireEvent.click(screen.getByRole("radio", { name: /Belem RWA EURCV/ }))
    resolveAnalysis(Response.json(analysisResponse))
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(screen.queryByRole("region", { name: "Recommendation" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Run analysis" })).toBeEnabled()
  })

  it("resets scenario edits and errors when the entry changes", async () => {
    stubApi()
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.change(screen.getByLabelText("Stablecoin depeg severity (%)"), { target: { value: "200" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveAccessibleDescription(
      "Enter a severity above 0% and at most 100%",
    )

    fireEvent.click(screen.getByRole("radio", { name: /MEV Capital wETH/ }))

    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveValue("10")
    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).not.toHaveAttribute("aria-invalid")
  })

  it("reports scenario errors even when the position is also invalid", async () => {
    const fetchMock = stubApi()
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "abc" } })
    fireEvent.change(screen.getByLabelText("Stablecoin depeg severity (%)"), { target: { value: "200" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveAccessibleDescription(
      "Enter a severity above 0% and at most 100%",
    )
    expect(screen.getByLabelText("Amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analysis")).toHaveLength(0)
  })

  it("shows a sanitized error when the catalog cannot load", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "Risk service unavailable" }, { status: 502 })))
    render(<Home />)

    expect(await screen.findByRole("alert")).toHaveTextContent("Risk service unavailable")
  })

  it("shows analysis errors without a recommendation", async () => {
    stubApi(Response.json({ error: "The risk service rejected this position or scenario selection" }, { status: 422 }))
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The risk service rejected this position or scenario selection",
    )
    expect(screen.queryByRole("region", { name: "Recommendation" })).not.toBeInTheDocument()
  })

  it("blocks analysis when no scenario is selected", async () => {
    const fetchMock = stubApi()
    render(<Home />)

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox)
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(screen.getByText("Select at least one scenario")).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analysis")).toHaveLength(0)
  })

  it("shows CONNECTING rather than LIVE until the catalog and radar have loaded", async () => {
    stubApi()
    render(<Home />)

    expect(screen.getByText("CONNECTING")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
    expect(await screen.findByText("LIVE")).toBeInTheDocument()
  })

  it("reports a radar outage as DEGRADED", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json({ error: "Risk service unavailable" }, { status: 502 }) : base(url)
    )))
    render(<Home />)

    expect(await screen.findByText("DEGRADED")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
  })

  it("announces a failed exposure as an alert, not as a success status", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/exposures" ? Response.json({ error: "The risk service rejected this exposure" }, { status: 422 }) : base(url)
    )))
    render(<Home />)

    fireEvent.click(await screen.findByRole("button", { name: "Add exposure" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("The risk service rejected this exposure")
    expect(screen.queryByText("Exposure recorded", { exact: false })).not.toBeInTheDocument()
  })

  it("coalesces a burst of radar events into a single graph refetch", async () => {
    const sources: { emit: (type: string, payload: unknown) => void }[] = []
    vi.stubGlobal("EventSource", class {
      listeners = new Map<string, (event: MessageEvent) => void>()
      constructor() { sources.push(this) }
      addEventListener(type: string, listener: (event: MessageEvent) => void) { this.listeners.set(type, listener) }
      close() {}
      emit(type: string, payload: unknown) { this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(payload) })) }
    })
    const fetchMock = stubApi()
    const graphCalls = () => fetchMock.mock.calls.filter(([url]) => url === "/api/graph").length
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })
    expect(graphCalls()).toBe(1)

    act(() => {
      for (const source of sources) {
        source.emit("graph.updated", { sequence: 1, type: "graph.updated", resource_id: "graph", summary: {} })
        source.emit("incident.opened", { sequence: 2, type: "incident.opened", resource_id: "incident:a", summary: {} })
        source.emit("graph.updated", { sequence: 3, type: "graph.updated", resource_id: "graph", summary: {} })
        source.emit("incident.updated", { sequence: 4, type: "incident.updated", resource_id: "incident:a", summary: {} })
      }
    })

    await waitFor(() => expect(graphCalls()).toBe(2))
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(graphCalls()).toBe(2)
  })

  it("keeps the most recently selected incident when an older response arrives late", async () => {
    const incidentFor = (id: string, label: string) => ({
      incident_id: id, source_entity_id: "vault:1:catalog", severity: "CRITICAL", status: "OPEN",
      trigger_metrics: {}, rule_version: "v1", first_observed_at: "2026-10-10T12:00:00Z",
      last_observed_at: "2026-10-10T12:00:00Z", resolved_at: null, reasons: [label],
    })
    const graphWithIncidents = {
      ...radarGraph,
      entities: [radarGraph.entities[0], { ...radarGraph.entities[1], entity_id: "vault:1:peer" }],
      active_incidents: [incidentFor("incident:a", "Alpha"), { ...incidentFor("incident:b", "Beta"), source_entity_id: "vault:1:peer" }],
    }
    let releaseA: (() => void) | undefined
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/graph") return Response.json(graphWithIncidents)
      if (url.startsWith("/api/graph?incident_graph_id=")) {
        const id = decodeURIComponent(url.split("=")[1])
        const body = {
          schema_version: "1.0.0", incident: graphWithIncidents.active_incidents.find((item) => item.incident_id === id),
          entities: [], relationships: [], similarities: [], exposures: [], impacts: [], generated_at: "2026-10-10T12:00:00Z",
        }
        if (id === "incident:a") await new Promise<void>((resolve) => { releaseA = resolve })
        return Response.json(body)
      }
      return base(url)
    }))
    render(<Home />)
    const rail = await screen.findByRole("list", { name: "Active incidents" })
    const button = (name: string) => within(rail).getByRole("button", { name })

    fireEvent.click(button("Belem Sentinel USDC CRITICAL"))
    fireEvent.click(button("Institutional USDC Peer CRITICAL"))
    await waitFor(() => expect(button("Institutional USDC Peer CRITICAL")).toHaveAttribute("aria-pressed", "true"))
    await waitFor(() => expect(releaseA).toBeDefined())
    await act(async () => releaseA?.())

    expect(button("Institutional USDC Peer CRITICAL")).toHaveAttribute("aria-pressed", "true")
    expect(button("Belem Sentinel USDC CRITICAL")).toHaveAttribute("aria-pressed", "false")
  })

  it("shows loading skeletons until the catalog and radar arrive", async () => {
    stubApi()
    render(<Home />)

    expect(screen.getByText("Loading curated catalog…")).toBeInTheDocument()
    expect(screen.getByText("Loading radar topology…")).toBeInTheDocument()
    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    await screen.findByRole("radio", { name: /Belem RWA EURCV/ })
    expect(screen.queryByText("Loading curated catalog…")).not.toBeInTheDocument()
    expect(screen.queryByText("Loading radar topology…")).not.toBeInTheDocument()
  })

  it("explains an empty radar instead of drawing an empty graph", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json({ ...radarGraph, entities: [] }) : base(url)
    )))
    render(<Home />)

    expect(await screen.findByText("Radar has no data yet — discovery runs hourly")).toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()
  })

  it("retries the catalog and radar with backoff while the risk service is busy", async () => {
    const base = stubApi()
    let busyCatalog = 1
    let busyGraph = 2
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/catalog" && busyCatalog-- > 0) {
        return Response.json({ error: "Risk service is busy — retrying" }, { status: 503 })
      }
      if (url === "/api/graph" && busyGraph-- > 0) {
        return Response.json({ error: "Risk service is starting or unreachable" }, { status: 503 })
      }
      return base(url)
    })
    vi.stubGlobal("fetch", fetchMock)
    render(<Home />)

    // Retried failures are announced politely and never as a dead-end alert.
    expect(await screen.findByText("Risk service is busy — retrying")).toHaveAttribute("role", "status")
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByText("Loading curated catalog…")).toBeInTheDocument()
    expect(screen.getByText("Loading radar topology…")).toBeInTheDocument()

    expect(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }, { timeout: 4_000 })).toBeInTheDocument()
    // The radar is still retrying once the catalog error is gone.
    expect(
      await screen.findByText("Risk service is starting or unreachable — retrying automatically", {}, { timeout: 4_000 }),
    ).toHaveAttribute("role", "status")
    expect(await screen.findByRole("button", { name: /Belem Sentinel USDC HEALTHY/ }, { timeout: 6_000 })).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/catalog")).toHaveLength(2)
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/graph")).toHaveLength(3)
    expect(screen.queryByText(/retrying/)).not.toBeInTheDocument()
    expect(await screen.findByText("LIVE")).toBeInTheDocument()
  }, 15_000)

  it("does not retry a non-retryable failure", async () => {
    const fetchMock = vi.fn(async () => Response.json({ error: "Risk service unavailable" }, { status: 502 }))
    vi.stubGlobal("fetch", fetchMock)
    render(<Home />)

    expect(await screen.findByRole("alert")).toHaveTextContent("Risk service unavailable")
    await new Promise((resolve) => setTimeout(resolve, 1_500))
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/catalog")).toHaveLength(1)
  })
})
