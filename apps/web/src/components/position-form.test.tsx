import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { catalogEntries } from "../test-fixtures"
import { POSITION_PRESETS, PositionForm } from "./position-form"

afterEach(cleanup)

const entry = (id: string) => catalogEntries.find((item) => item.entity_id === id)!

function change(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

describe("PositionForm", () => {
  it("prefills lender presets and submits a lender position without borrower fields", () => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("belem-rwa-eurcv")} onSubmit={onSubmit} />)

    expect(screen.queryByRole("radio", { name: "Borrower" })).not.toBeInTheDocument()
    expect(screen.getByText("Lender only")).toBeInTheDocument()
    expect(screen.getByLabelText("Amount")).toHaveValue("100000")
    expect(screen.getByLabelText("Numeraire")).toHaveValue("EURCV")
    expect(screen.queryByLabelText("Collateral amount")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(onSubmit).toHaveBeenCalledWith({
      mode: "lender",
      entity_id: "belem-rwa-eurcv",
      amount: "100000",
      numeraire: "EURCV",
    })
  })

  it("switches to borrower fields and submits presets with amount equal to debt", () => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("weth-usdc-860")} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    const preset = POSITION_PRESETS["weth-usdc-860"].borrower!

    expect(screen.getByLabelText("Collateral amount")).toHaveValue(preset.collateral_amount)
    expect(screen.getByLabelText("Debt amount")).toHaveValue(preset.debt_amount)
    expect(screen.getByLabelText("Current LTV (%)")).toHaveValue("75")
    expect(screen.queryByLabelText("Amount")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(onSubmit).toHaveBeenCalledWith({
      mode: "borrower",
      entity_id: "weth-usdc-860",
      amount: preset.debt_amount,
      collateral_amount: preset.collateral_amount,
      debt_amount: preset.debt_amount,
      ltv: "0.75",
      numeraire: "USDC",
    })
  })

  it("switches back to lender mode and drops borrower fields", () => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("weth-usdc-860")} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    fireEvent.click(screen.getByRole("radio", { name: "Lender" }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(onSubmit.mock.calls[0][0]).toEqual({
      mode: "lender",
      entity_id: "weth-usdc-860",
      amount: "100000",
      numeraire: "USDC",
    })
  })

  it.each([
    ["0", "Enter a positive number"],
    ["-5", "Enter a positive number"],
    ["NaN", "Enter a positive number"],
    ["Infinity", "Enter a positive number"],
    ["1000000000000000.01", "Enter at most 1,000,000,000,000,000"],
  ])("rejects lender amount %s", (value, message) => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("belem-rwa-eurcv")} onSubmit={onSubmit} />)

    change("Amount", value)
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Amount")).toHaveAccessibleDescription(message)
    expect(screen.getByLabelText("Amount")).toHaveAttribute("aria-invalid", "true")
  })

  it("accepts an LTV just below LLTV and rejects LTV at LLTV", () => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("weth-usdc-860")} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))

    change("Current LTV (%)", "86")
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Current LTV (%)")).toHaveAccessibleDescription(
      "LTV must be below the market LLTV of 86.00%",
    )

    change("Current LTV (%)", "85.9999")
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ ltv: "0.859999" }))
  })

  it("validates borrower collateral and debt", () => {
    const onSubmit = vi.fn()
    render(<PositionForm entry={entry("wbtc-usdc-860")} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))

    change("Collateral amount", "")
    change("Debt amount", "-1")
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Collateral amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(screen.getByLabelText("Debt amount")).toHaveAccessibleDescription("Enter a positive number")
  })

  it("restores presets on request", () => {
    render(<PositionForm entry={entry("belem-rwa-eurcv")} onSubmit={() => {}} />)

    change("Amount", "5")
    fireEvent.click(screen.getByRole("button", { name: "Reset to preset" }))

    expect(screen.getByLabelText("Amount")).toHaveValue("100000")
  })

  it("defaults the numeraire to the live asset symbol", () => {
    const base = entry("mev-capital-weth")
    render(
      <PositionForm entry={{ ...base, metrics: { ...base.metrics!, asset_symbol: "ETH" } }} onSubmit={() => {}} />,
    )

    expect(screen.getByLabelText("Numeraire")).toHaveValue("ETH")
  })

  it("has presets for all five curated entries and borrower presets below LLTV", () => {
    for (const item of catalogEntries) {
      const preset = POSITION_PRESETS[item.entity_id]
      expect(preset).toBeDefined()
      expect(Boolean(preset.borrower)).toBe(item.supported_modes.includes("borrower"))
    }
    expect(POSITION_PRESETS["wsteth-weth-965"].borrower!.ltv).toBe("0.90")
  })

  it("disables submission while analysis is running or unavailable", () => {
    const { rerender } = render(<PositionForm entry={entry("belem-rwa-eurcv")} onSubmit={() => {}} busy />)
    expect(screen.getByRole("button", { name: "Running analysis…" })).toBeDisabled()

    rerender(
      <PositionForm entry={{ ...entry("belem-rwa-eurcv"), analysis_available: false }} onSubmit={() => {}} />,
    )
    expect(screen.getByRole("button", { name: "Run analysis" })).toBeDisabled()
    expect(screen.getByText("Analysis is unavailable for this entry.")).toBeInTheDocument()
  })
})
