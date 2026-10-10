import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { NOW, catalogEntries } from "../test-fixtures"
import { CatalogSelector } from "./catalog-selector"

afterEach(cleanup)

describe("CatalogSelector", () => {
  it("lists exactly the five curated entries with their provenance labels", () => {
    render(<CatalogSelector entries={catalogEntries} selectedId={null} onSelect={() => {}} now={NOW} />)

    const options = screen.getAllByRole("radio")
    expect(options).toHaveLength(5)
    for (const entry of catalogEntries) {
      const option = screen.getByRole("radio", { name: new RegExp(entry.name.replace(/[/]/g, "\\/")) })
      const card = option.closest("label") as HTMLElement
      expect(within(card).getByText(entry.provenance_label)).toBeInTheDocument()
    }
  })

  it("describes entity type and LLTV", () => {
    render(<CatalogSelector entries={catalogEntries} selectedId={null} onSelect={() => {}} now={NOW} />)

    expect(screen.getByText("Morpho Vault V2")).toBeInTheDocument()
    expect(screen.getByText("Morpho Vault V1")).toBeInTheDocument()
    expect(screen.getByText("Morpho market · 96.50% LLTV")).toBeInTheDocument()
    expect(screen.getAllByText("Morpho market · 86.00% LLTV")).toHaveLength(2)
  })

  it("never presents representative proxies as confirmed Belem holdings", () => {
    render(<CatalogSelector entries={catalogEntries} selectedId={null} onSelect={() => {}} now={NOW} />)

    expect(screen.getAllByText("Representative proxy — not a confirmed Belem holding")).toHaveLength(3)
    expect(screen.getByText("Verified Belem")).toBeInTheDocument()
    expect(screen.getByText("Belem-distributed")).toBeInTheDocument()
  })

  it("selects an entry", () => {
    const onSelect = vi.fn()
    render(<CatalogSelector entries={catalogEntries} selectedId={null} onSelect={onSelect} now={NOW} />)

    fireEvent.click(screen.getByRole("radio", { name: /WETH collateral \/ USDC debt/ }))

    expect(onSelect).toHaveBeenCalledWith("weth-usdc-860")
  })

  it("keeps unsupported entries visible but disables analysis", () => {
    const entries = catalogEntries.map((entry) =>
      entry.entity_id === "mev-capital-weth" ? { ...entry, supported: false } : entry,
    )
    render(<CatalogSelector entries={entries} selectedId={null} onSelect={() => {}} now={NOW} />)

    const option = screen.getByRole("radio", { name: /MEV Capital wETH/ })
    expect(option).toBeDisabled()
    expect(within(option.closest("label") as HTMLElement).getByText("Analysis unavailable")).toBeInTheDocument()
  })

  it("disables entries the risk service marks unavailable and explains why", () => {
    const entries = catalogEntries.map((entry) =>
      entry.entity_id === "weth-usdc-860"
        ? {
            ...entry,
            analysis_available: false,
            availability: "delisted" as const,
            warnings: ["Morpho no longer lists this entity; analysis is disabled."],
          }
        : entry,
    )
    render(<CatalogSelector entries={entries} selectedId={null} onSelect={() => {}} now={NOW} />)

    const card = screen.getByRole("radio", { name: /WETH collateral \/ USDC debt/ }).closest("label") as HTMLElement
    expect(within(card).getByRole("radio")).toBeDisabled()
    expect(within(card).getByText("Analysis unavailable · Delisted by Morpho")).toBeInTheDocument()
    expect(within(card).getByText("Morpho no longer lists this entity; analysis is disabled.")).toBeInTheDocument()
    expect(within(card).getByText("WETH collateral / USDC debt")).toBeInTheDocument()
  })

  it("shows live metrics with freshness and source", () => {
    const entries = catalogEntries.map((entry) =>
      entry.entity_id === "wsteth-weth-965"
        ? { ...entry, freshness: "stale_usable" as const, fetched_at: "2026-10-10T11:58:00Z" }
        : entry,
    )
    render(<CatalogSelector entries={entries} selectedId={null} onSelect={() => {}} now={NOW} />)

    const card = screen.getByRole("radio", { name: /wstETH collateral/ }).closest("label") as HTMLElement
    expect(within(card).getByText("Supplied 310,500 WETH · liquidity 27,900 WETH · utilization 91.01%")).toBeInTheDocument()
    expect(within(card).getByText(/^Stale · 2 min 0 s old · https:\/\/api\.morpho\.org/)).toBeInTheDocument()
  })

  it("does not invent metrics when the provider returned none", () => {
    const entries = catalogEntries.map((entry) =>
      entry.entity_id === "belem-rwa-eurcv"
        ? {
            ...entry,
            analysis_available: false,
            availability: "provider_unavailable" as const,
            metrics: null,
            source: null,
            fetched_at: null,
            freshness: null,
            age_seconds: null,
            block_number: null,
            warnings: ["Morpho data is unavailable and no usable cache exists."],
          }
        : entry,
    )
    render(<CatalogSelector entries={entries} selectedId={null} onSelect={() => {}} now={NOW} />)

    const card = screen.getByRole("radio", { name: /Belem RWA EURCV/ }).closest("label") as HTMLElement
    expect(within(card).getByText("Live metrics unavailable")).toBeInTheDocument()
    expect(within(card).queryByText(/Supplied/)).not.toBeInTheDocument()
    expect(within(card).getByText("Analysis unavailable · Morpho data unavailable")).toBeInTheDocument()
  })
})
