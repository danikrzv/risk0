import "@testing-library/jest-dom/vitest"

import type { RadarEntity } from "@risk0/contracts"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ExposureRail } from "./exposure-rail"

afterEach(cleanup)

const entities: RadarEntity[] = [
  {
    entity_id: "vault:1:catalog",
    entity_type: "vault",
    chain_id: 1,
    address_or_key: "catalog",
    label: "Catalog Vault",
    source: "fixture:morpho",
    catalog: true,
    visible: true,
    metadata: {},
  },
  {
    entity_id: "market:1:weth-usdc",
    entity_type: "market",
    chain_id: 1,
    address_or_key: "weth-usdc",
    label: "WETH / USDC Market",
    source: "fixture:morpho",
    catalog: false,
    visible: true,
    metadata: {},
  },
]

function change(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

describe("ExposureRail", () => {
  it("offers only markets as borrower targets, which the risk service requires", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower exposure" }))

    const options = Array.from(screen.getByLabelText("Exposure target").querySelectorAll("option"))
    expect(options.map((option) => option.getAttribute("value"))).toEqual(["market:1:weth-usdc"])
    expect(screen.getByLabelText("Exposure target")).toHaveValue("market:1:weth-usdc")
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ entity_id: "market:1:weth-usdc", mode: "borrower" }))
  })

  it("starts with a sensible lender preset and submits a contract request", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    expect(screen.getByLabelText("Exposure target")).toHaveValue("vault:1:catalog")
    expect(screen.getByRole("radio", { name: "Lender exposure" })).toBeChecked()
    expect(screen.getByLabelText("Exposure amount")).toHaveValue("100000")
    expect(screen.getByLabelText("Exposure numeraire")).toHaveValue("USDC")

    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    expect(onSubmit).toHaveBeenCalledWith({
      entity_id: "vault:1:catalog",
      mode: "lender",
      amount: "100000",
      numeraire: "USDC",
      collateral_amount: null,
      debt_amount: null,
      ltv: null,
    })
  })

  it("provides borrower presets and converts percentage LTV to a ratio", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower exposure" }))

    expect(screen.getByLabelText("Collateral amount")).toHaveValue("100000")
    expect(screen.getByLabelText("Debt amount")).toHaveValue("75000")
    expect(screen.getByLabelText("Borrower LTV (%)")).toHaveValue("75")

    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    expect(onSubmit).toHaveBeenCalledWith({
      entity_id: "market:1:weth-usdc",
      mode: "borrower",
      amount: "75000",
      numeraire: "USDC",
      collateral_amount: "100000",
      debt_amount: "75000",
      ltv: "0.75",
    })
  })

  it("rejects non-positive amounts and an LTV outside the open 0-100 range", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower exposure" }))
    change("Collateral amount", "0")
    change("Debt amount", "-1")
    change("Borrower LTV (%)", "100")
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Collateral amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(screen.getByLabelText("Debt amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(screen.getByLabelText("Borrower LTV (%)")).toHaveAccessibleDescription(
      "Enter an LTV greater than 0% and below 100%",
    )
    expect(screen.getByRole("alert")).toHaveTextContent("Fix the 3 highlighted fields")
  })

  it("resets edited values to the current mode preset", () => {
    render(<ExposureRail entities={entities} onSubmit={() => {}} />)

    change("Exposure amount", "12")
    change("Exposure numeraire", "WETH")
    fireEvent.click(screen.getByRole("button", { name: "Reset preset" }))

    expect(screen.getByLabelText("Exposure amount")).toHaveValue("100000")
    expect(screen.getByLabelText("Exposure numeraire")).toHaveValue("USDC")
  })

  it("selects the first target discovered after an initially empty graph", () => {
    const onSubmit = vi.fn()
    const { rerender } = render(<ExposureRail entities={[]} onSubmit={onSubmit} />)
    rerender(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    expect(screen.getByLabelText("Exposure target")).toHaveValue("vault:1:catalog")
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ entity_id: "vault:1:catalog" }))
  })

  it("uses a native radio group for keyboard mode selection", () => {
    render(<ExposureRail entities={entities} onSubmit={() => {}} />)
    expect(screen.getByRole("radio", { name: "Lender exposure" })).toHaveAttribute("name", "exposure-mode")
    expect(screen.getByRole("radio", { name: "Borrower exposure" })).toHaveAttribute("name", "exposure-mode")
  })

  it("submits exact decimal strings and converts LTV without float error", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower exposure" }))
    change("Collateral amount", " 1e5 ")
    change("Debt amount", "0.1")
    change("Borrower LTV (%)", "33.3")
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      amount: "0.1",
      collateral_amount: "100000",
      debt_amount: "0.1",
      ltv: "0.333",
    }))
  })

  it("rejects non-decimal amounts and an empty numeraire", () => {
    const onSubmit = vi.fn()
    render(<ExposureRail entities={entities} onSubmit={onSubmit} />)

    change("Exposure amount", "0x10")
    change("Exposure numeraire", "  ")
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Exposure amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(screen.getByLabelText("Exposure amount")).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByLabelText("Exposure numeraire")).toHaveAccessibleDescription("Enter a numeraire")
  })
})

