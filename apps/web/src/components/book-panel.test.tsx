import "@testing-library/jest-dom/vitest"

import { type BookPocketResult, type BookRequest, type BookResponse, MAX_BOOK_POCKETS } from "@risk0/contracts"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { catalogEntries, hydrated } from "../test-fixtures"
import { BRIEF_POCKETS, BookPanel, MAX_POCKETS, breakEvenCompact, briefExampleDrafts } from "./book-panel"

afterEach(cleanup)

const breakEven = {
  probability_dependent: true,
  dominant_scenario_id: "collateral-shock",
  exit_cost: "110",
  points: [
    {
      scenario_id: "collateral-shock",
      dependency: "asset:1:weth",
      impact: "1000",
      other_expected_loss: "10",
      break_even_probability: "0.11",
      decision_flip_probability: "0.1",
      current_probability: "0.2",
      current_probability_low: "0.1",
      current_probability_high: "0.3",
      reasons: [],
    },
  ],
  explanation: "EXIT becomes optimal if the 30-day probability of collateral-shock exceeds 10%.",
}

function pocket(overrides: Partial<BookPocketResult>): BookPocketResult {
  return {
    pocket_id: "pocket-1",
    rank: 1,
    entity_id: "belem-rwa-eurcv",
    mode: "lender",
    numeraire: "EURCV",
    weight: "0.1",
    decision: "STAY",
    confidence: "high",
    dominant_dependency: null,
    triggers: [],
    pocket_value: "1000",
    expected_loss: { amount: "1", fraction_of_pocket: "0.001", fraction_of_book: "0.0001" },
    exit_cost: { amount: "5", fraction_of_pocket: "0.005", fraction_of_book: "0.0005" },
    forgone_yield: { amount: "3", fraction_of_pocket: "0.003", fraction_of_book: "0.0003" },
    exit_advantage: { amount: "-7", fraction_of_pocket: "-0.007", fraction_of_book: "-0.0007" },
    break_even: null,
    unavailable_reasons: [],
    explanation: "STAY",
    analyzed_at: "2026-10-11T09:00:00Z",
    ...overrides,
  }
}

const bookResponse: BookResponse = {
  schema_version: "1.0.0",
  pockets: [
    pocket({
      pocket_id: "pocket-3",
      entity_id: "wsteth-weth-965",
      numeraire: "WETH",
      decision: "EXIT",
      expected_loss: { amount: "200", fraction_of_pocket: "0.2", fraction_of_book: "0.02" },
      exit_cost: { amount: "110", fraction_of_pocket: "0.11", fraction_of_book: "0.011" },
      forgone_yield: { amount: "4", fraction_of_pocket: "0.004", fraction_of_book: "0.0004" },
      exit_advantage: { amount: "86", fraction_of_pocket: "0.086", fraction_of_book: "0.0086" },
      break_even: breakEven,
    }),
    pocket({ rank: 2 }),
    pocket({
      rank: 3,
      pocket_id: "pocket-2",
      entity_id: "mev-capital-weth",
      numeraire: "WETH",
      decision: "UNAVAILABLE",
      confidence: "unavailable",
      expected_loss: null,
      exit_advantage: null,
      unavailable_reasons: ["Critical data is missing."],
    }),
  ],
  totals: {
    numeraire: null,
    expected_loss_staying: { amount: null, fraction_of_book: "0.0201" },
    exit_cost_exit_pockets: { amount: null, fraction_of_book: "0.011" },
    forgone_yield_exit_pockets: { amount: null, fraction_of_book: "0.0004" },
    expected_loss_stay_pockets: { amount: null, fraction_of_book: "0.0001" },
    recommended_cost: { amount: null, fraction_of_book: "0.0115" },
    net_benefit: { amount: null, fraction_of_book: "0.0086" },
    allocated_weight: "0.3",
    stay_weight: "0.1",
    exit_weight: "0.1",
    unavailable_weight: "0.1",
    stay_count: 1,
    exit_count: 1,
    unavailable_count: 1,
    complete: false,
    warnings: ["1 pocket is UNAVAILABLE and excluded from the totals; the totals are incomplete."],
  },
  generated_at: "2026-10-11T09:00:00Z",
}

const book = () => screen.getByRole("region", { name: "Book" })

describe("briefExampleDrafts", () => {
  it("builds ten 10% lender pockets from analyzable entries, presets first", () => {
    const extra = hydrated({ ...catalogEntries[0], entity_id: "extra-usdc", name: "Extra USDC", metrics: null } as never)
    const offline = hydrated({ ...catalogEntries[0], entity_id: "offline" } as never, { analysis_available: false })
    const drafts = briefExampleDrafts([extra, offline, ...catalogEntries])

    expect(drafts).toHaveLength(BRIEF_POCKETS)
    expect(drafts.every((draft) => draft.weightPercent === "10" && draft.mode === "lender")).toBe(true)
    // Five preset entries first, then the extra one, then the list repeats.
    expect(drafts.map((draft) => draft.entityId)).toEqual([
      "belem-rwa-eurcv",
      "mev-capital-weth",
      "wsteth-weth-965",
      "weth-usdc-860",
      "wbtc-usdc-860",
      "extra-usdc",
      "belem-rwa-eurcv",
      "mev-capital-weth",
      "wsteth-weth-965",
      "weth-usdc-860",
    ])
    expect(drafts[0]).toMatchObject({ amount: "100000", numeraire: "EURCV" })
    expect(drafts[5]).toMatchObject({ amount: "1000", numeraire: "USDC" })
    expect(new Set(drafts.map((draft) => draft.key)).size).toBe(BRIEF_POCKETS)
  })

  it("returns no pockets when nothing is analyzable", () => {
    expect(briefExampleDrafts(catalogEntries.map((entry) => ({ ...entry, analysis_available: false })))).toEqual([])
  })

  it("keeps the pocket limit equal to the contract", () => {
    expect(MAX_POCKETS).toBe(MAX_BOOK_POCKETS)
  })
})

describe("BookPanel", () => {
  it("loads the brief example, runs it, and shows a ranked table with book totals", async () => {
    const runBook = vi.fn(async (_request: BookRequest) => bookResponse)
    render(<BookPanel entries={catalogEntries} runBook={runBook} />)

    fireEvent.click(within(book()).getByRole("button", { name: "Load brief example" }))
    const pockets = within(book()).getByRole("table", { name: /Pockets to analyze \(10, 100.00% of the book\)/ })
    expect(within(pockets).getAllByRole("row")).toHaveLength(11)
    expect(within(book()).getByText("Loaded the brief example: 10 pockets of 10% each.")).toHaveAttribute("role", "status")

    fireEvent.click(within(book()).getByRole("button", { name: "Run book analysis" }))
    expect(await within(book()).findByText("Book analyzed: 1 STAY, 1 EXIT, 1 UNAVAILABLE.")).toBeInTheDocument()

    const request = runBook.mock.calls[0][0]
    expect(request.pockets).toHaveLength(10)
    expect(request.pockets.every((item) => item.weight === "0.1")).toBe(true)
    expect(request.pockets[0].position).toEqual({ mode: "lender", entity_id: "belem-rwa-eurcv", amount: "100000", numeraire: "EURCV" })
    expect(request.scenarios).toHaveLength(5)

    const table = within(book()).getByRole("table", { name: /Pockets ranked by expected loss minus exit cost/ })
    const rows = within(table).getAllByRole("row").slice(1)
    expect(rows).toHaveLength(3)
    const exitRow = rows[0]
    expect(within(exitRow).getByRole("rowheader")).toHaveTextContent("wstETH collateral / WETH debt")
    expect(within(exitRow).getByText("EXIT").closest("[data-tone]")).toHaveAttribute("data-tone", "danger")
    expect(exitRow).toHaveTextContent("200 WETH")
    expect(exitRow).toHaveTextContent("20.00% of pocket · 2.00% of book")
    expect(exitRow).toHaveTextContent("110 WETH")
    expect(exitRow).toHaveTextContent("11.00% of pocket")
    expect(within(table).getByRole("columnheader", { name: "Forgone 30-day yield" })).toBeInTheDocument()
    const forgoneColumn = within(table)
      .getAllByRole("columnheader")
      .findIndex((header) => header.textContent === "Forgone 30-day yield")
    const forgoneCell = within(exitRow).getAllByRole("cell")[forgoneColumn - 1]
    expect(forgoneCell).toHaveTextContent("4 WETH")
    expect(forgoneCell).toHaveTextContent("0.40% of pocket · 0.04% of book")
    expect(within(rows[1]).getAllByRole("cell")[forgoneColumn - 1]).toHaveTextContent("3 EURCV")
    expect(exitRow).toHaveTextContent("EXIT above 10.00% · estimate 20.00%")

    const unavailableRow = rows[2]
    const marker = within(unavailableRow).getByText("UNAVAILABLE").closest("[data-tone]")
    expect(marker).toHaveAttribute("data-tone", "unavailable")
    expect(marker).not.toHaveClass("book-decision--safe")
    expect(unavailableRow).toHaveTextContent("Not a recommendation to stay")
    expect(unavailableRow).toHaveTextContent("Critical data is missing.")
    expect(unavailableRow).toHaveTextContent("Unknown")

    const totals = within(book()).getByRole("table", { name: "Book totals" })
    expect(within(totals).getByRole("row", { name: /Expected loss if staying in every pocket 2.01%/ })).toBeInTheDocument()
    expect(within(totals).getByRole("row", { name: /Yield forgone by exiting the EXIT pockets 0.04%/ })).toBeInTheDocument()
    expect(within(totals).getByRole("row", { name: /Book cost following the recommendations 1.15%/ })).toBeInTheDocument()
    expect(within(totals).getByRole("row", { name: /Expected saving versus staying everywhere 0.86%/ })).toBeInTheDocument()
    expect(within(book()).getByText(/Totals are incomplete/)).toBeInTheDocument()
    expect(within(book()).getByRole("list", { name: "Book warnings" })).toHaveTextContent("1 pocket is UNAVAILABLE")
  })

  it("validates pockets before adding them and keeps weights within the book", () => {
    render(<BookPanel entries={catalogEntries} runBook={vi.fn()} />)
    const form = within(book()).getByRole("form", { name: "Add a pocket" })
    const weight = within(form).getByLabelText("Weight (% of book)")

    fireEvent.change(weight, { target: { value: "0" } })
    fireEvent.click(within(form).getByRole("button", { name: "Add pocket" }))
    expect(weight).toHaveAttribute("aria-invalid", "true")
    expect(within(form).getByText("Enter a weight above 0% and at most 100%")).toBeInTheDocument()

    fireEvent.change(weight, { target: { value: "60" } })
    fireEvent.click(within(form).getByRole("button", { name: "Add pocket" }))
    expect(within(book()).getByRole("table", { name: /Pockets to analyze \(1, 60.00% of the book\)/ })).toBeInTheDocument()

    fireEvent.click(within(form).getByRole("button", { name: "Add pocket" }))
    expect(within(form).getByText("Pocket weights must add up to at most 100% of the book")).toBeInTheDocument()

    fireEvent.click(within(book()).getByRole("button", { name: "Remove pocket Belem RWA EURCV (60%)" }))
    expect(within(book()).queryByRole("table", { name: /Pockets to analyze/ })).not.toBeInTheDocument()
  })

  it("builds a borrower pocket and checks its LTV against the market LLTV", async () => {
    const runBook = vi.fn(async (_request: BookRequest) => bookResponse)
    render(<BookPanel entries={catalogEntries} runBook={runBook} />)
    const form = within(book()).getByRole("form", { name: "Add a pocket" })

    fireEvent.change(within(form).getByLabelText("Catalog entry"), { target: { value: "weth-usdc-860" } })
    fireEvent.change(within(form).getByLabelText("Mode"), { target: { value: "borrower" } })
    fireEvent.change(within(form).getByLabelText("Current LTV (%)"), { target: { value: "90" } })
    fireEvent.click(within(form).getByRole("button", { name: "Add pocket" }))
    expect(within(form).getByText("LTV must be below the market LLTV of 86.00%")).toBeInTheDocument()

    fireEvent.change(within(form).getByLabelText("Current LTV (%)"), { target: { value: "75" } })
    fireEvent.click(within(form).getByRole("button", { name: "Add pocket" }))
    fireEvent.click(within(book()).getByRole("button", { name: "Run book analysis" }))
    await waitFor(() => expect(runBook).toHaveBeenCalledTimes(1))

    expect(runBook.mock.calls[0][0].pockets[0]).toMatchObject({
      weight: "0.1",
      position: { mode: "borrower", entity_id: "weth-usdc-860", amount: "4675", collateral_amount: "2.5", debt_amount: "4675", ltv: "0.75", numeraire: "USDC" },
    })
  })

  it("shows a failed run as an alert", async () => {
    render(<BookPanel entries={catalogEntries} runBook={vi.fn(async () => { throw new Error("Risk service unavailable") })} />)

    fireEvent.click(within(book()).getByRole("button", { name: "Load brief example" }))
    fireEvent.click(within(book()).getByRole("button", { name: "Run book analysis" }))

    expect(await within(book()).findByRole("alert")).toHaveTextContent("Risk service unavailable")
  })
})

describe("breakEvenCompact", () => {
  it("states hard triggers, missing views, and unreachable break-evens plainly", () => {
    expect(breakEvenCompact(pocket({ break_even: null }))).toBe("Unavailable")
    expect(breakEvenCompact(pocket({ break_even: { ...breakEven, probability_dependent: false } }))).toBe(
      "Hard trigger: not probability-dependent",
    )
    const never = { ...breakEven.points[0], decision_flip_probability: "1", break_even_probability: "1" }
    expect(breakEvenCompact(pocket({ break_even: { ...breakEven, points: [never] } }))).toBe("EXIT never cheaper · estimate 20.00%")
    const unknown = { ...breakEven.points[0], decision_flip_probability: null, break_even_probability: null, current_probability: null }
    expect(breakEvenCompact(pocket({ break_even: { ...breakEven, points: [unknown] } }))).toBe("Unknown · estimate unavailable")
  })
})
