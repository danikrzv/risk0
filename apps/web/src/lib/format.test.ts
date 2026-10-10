import { describe, expect, it } from "vitest"

import {
  compareDecimal,
  formatAge,
  formatAmount,
  formatPercent,
  formatProbabilityRange,
  formatQuantity,
  isPositiveDecimal,
  percentToRatio,
  ratioToPercent,
} from "./format"

describe("decimal formatting", () => {
  it("formats decimal strings with grouping and half-up rounding without floats", () => {
    expect(formatAmount("1234567.891")).toBe("1,234,567.89")
    expect(formatAmount("0.005")).toBe("0.01")
    expect(formatAmount("0.0049")).toBe("0.00")
    expect(formatAmount("-12.345")).toBe("-12.35")
    expect(formatAmount("999999999999999999.995")).toBe("1,000,000,000,000,000,000.00")
    expect(formatAmount("1000000000000000001", 0)).toBe("1,000,000,000,000,000,001")
  })

  it("formats token quantities with up to six decimals and trimmed zeros", () => {
    expect(formatQuantity("0.122")).toBe("0.122")
    expect(formatQuantity("1520.500000")).toBe("1,520.5")
    expect(formatQuantity("0.00000049")).toBe("0")
    expect(formatQuantity("0.0000005")).toBe("0.000001")
    expect(formatQuantity("100000")).toBe("100,000")
  })

  it("formats ratios as percentages", () => {
    expect(formatPercent("0.965")).toBe("96.50%")
    expect(formatPercent("0.0004119", 4)).toBe("0.0412%")
    expect(formatPercent("1")).toBe("100.00%")
  })

  it("formats a probability range with its central value", () => {
    expect(formatProbabilityRange({ low: "0.01", central: "0.02", high: "0.04" })).toBe(
      "2.00% (1.00%–4.00%)",
    )
    expect(formatProbabilityRange({ low: null, central: null, high: null })).toBe(
      "Unavailable",
    )
  })

  it("converts between percent inputs and ratio strings exactly", () => {
    expect(percentToRatio("15")).toBe("0.15")
    expect(percentToRatio("85.9999")).toBe("0.859999")
    expect(percentToRatio("100")).toBe("1")
    expect(percentToRatio("0.5")).toBe("0.005")
    expect(ratioToPercent("0.859999")).toBe("85.9999")
    expect(ratioToPercent("0.1")).toBe("10")
  })

  it("compares decimal strings exactly", () => {
    expect(compareDecimal("0.859999", "0.86")).toBe(-1)
    expect(compareDecimal("0.86", "0.860")).toBe(0)
    expect(compareDecimal("1000000000000000.000001", "1000000000000000")).toBe(1)
  })

  it("accepts only finite positive plain decimals", () => {
    expect(isPositiveDecimal("100000")).toBe(true)
    expect(isPositiveDecimal("0.000001")).toBe(true)
    for (const value of ["0", "0.000", "-1", "NaN", "Infinity", "1e5", "", " 1", "1.2.3"]) {
      expect(isPositiveDecimal(value)).toBe(false)
    }
  })

  it("formats ages exactly", () => {
    expect(formatAge(4)).toBe("4 s")
    expect(formatAge(65)).toBe("1 min 5 s")
    expect(formatAge(3600 + 120)).toBe("1 h 2 min")
    expect(formatAge(2 * 86400 + 3600)).toBe("2 d 1 h")
  })
})
