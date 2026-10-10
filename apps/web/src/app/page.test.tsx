import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
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
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

function stubApi(analysis: Response | (() => Promise<Response>) = Response.json(analysisResponse)) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/catalog") return Response.json(catalogResponse)
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
