import "@testing-library/jest-dom/vitest"

import type { AnalysisResponse } from "@risk0/contracts"
import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { NOW, analysisResponse, empiricalProbability, modelProbability } from "../test-fixtures"
import { DataQualityPanel, FreshnessBadge, ProbabilityMethodLabel, modelVersion } from "./data-quality"

afterEach(cleanup)

describe("FreshnessBadge", () => {
  it("shows exact age and source for stale data", () => {
    render(
      <FreshnessBadge
        freshness="stale_usable"
        observedAt="2026-10-10T11:54:53Z"
        source="https://api.morpho.org/markets/0xb8fc"
        now={NOW}
      />,
    )

    expect(screen.getByText("Stale · 5 min 7 s old · https://api.morpho.org/markets/0xb8fc")).toBeInTheDocument()
  })

  it("prefers the server-reported age over the browser clock", () => {
    render(
      <FreshnessBadge freshness="fresh" observedAt="2026-10-10T11:59:58Z" source="rpc" now={NOW} ageSeconds={125} />,
    )

    expect(screen.getByText("Fresh · 2 min 5 s old · rpc")).toBeInTheDocument()
  })

  it("labels fresh and expired data", () => {
    render(
      <>
        <FreshnessBadge freshness="fresh" observedAt="2026-10-10T11:59:58Z" source="rpc" now={NOW} />
        <FreshnessBadge freshness="expired" observedAt="2026-10-09T12:00:00Z" source="rpc" now={NOW} />
      </>,
    )

    expect(screen.getByText("Fresh · 2 s old · rpc")).toBeInTheDocument()
    expect(screen.getByText("Expired · 1 d old · rpc")).toBeInTheDocument()
  })
})

describe("ProbabilityMethodLabel", () => {
  it("labels model-estimated probabilities with their model version", () => {
    render(<ProbabilityMethodLabel estimate={modelProbability} />)

    expect(screen.getByText("Model-estimated · protocol-evidence-v1")).toBeInTheDocument()
  })

  it("labels empirical and unavailable probabilities", () => {
    render(
      <>
        <ProbabilityMethodLabel estimate={empiricalProbability} />
        <ProbabilityMethodLabel estimate={{ ...modelProbability, method: "unavailable" }} />
      </>,
    )

    expect(screen.getByText("Empirical")).toBeInTheDocument()
    expect(screen.getByText("Probability unavailable")).toBeInTheDocument()
  })

  it("never invents a model version", () => {
    expect(modelVersion({ ...modelProbability, explanation: "no version here" })).toBeNull()
    render(<ProbabilityMethodLabel estimate={{ ...modelProbability, explanation: "no version here" }} />)
    expect(screen.getByText("Model-estimated · model version not reported")).toBeInTheDocument()
  })
})

describe("DataQualityPanel", () => {
  it("summarizes stale sources, quote age, probability sources, and warnings", () => {
    const analysis: AnalysisResponse = {
      ...analysisResponse,
      exit_estimate: { ...analysisResponse.exit_estimate, warnings: ["Quote refreshed once."] },
    }
    render(<DataQualityPanel analysis={analysis} now={NOW} receivedAt={NOW - 3000} />)

    const panel = screen.getByRole("region", { name: "Data quality" })
    expect(within(panel).getByText("1 stale source")).toBeInTheDocument()
    expect(
      within(panel).getByText("Stale · 5 min old · https://api.morpho.org/markets/0xb8fc"),
    ).toBeInTheDocument()
    // The quote row uses the server's quote_age_seconds (4 s) plus time elapsed since receipt (3 s).
    expect(within(panel).getByText("Fresh · 7 s old · uniswap-v3 quoter")).toBeInTheDocument()
    expect(within(panel).getByText("No Uniswap exit pool was found for wstETH.")).toBeInTheDocument()
    expect(within(panel).getByText("Quote refreshed once.")).toBeInTheDocument()
    expect(within(panel).getByText(/Model-estimated · protocol-evidence-v1/)).toBeInTheDocument()
  })
})
