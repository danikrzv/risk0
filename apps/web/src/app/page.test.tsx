import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
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
})
