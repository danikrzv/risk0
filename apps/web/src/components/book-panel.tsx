"use client"

import type {
  BookAmount,
  BookPocketInput,
  BookPocketResult,
  BookRequest,
  BookResponse,
  BookTotal,
  HydratedCatalogEntry,
  PositionMode,
  RecommendationDecision,
} from "@risk0/contracts"
import { type FormEvent, useId, useMemo, useRef, useState } from "react"

import { runBook as runBookRequest } from "../lib/api"
import {
  compareDecimal,
  formatPercent,
  formatQuantity,
  isPositiveDecimal,
  normalizeDecimal,
  percentToRatio,
  ratioToPercent,
} from "../lib/format"
import { breakEvenPoint, breakEvenThreshold, formatProbability } from "./break-even"
import { canAnalyze } from "./catalog-selector"
import { POSITION_PRESETS } from "./position-form"
import { defaultScenarioDrafts, toScenarioSelections } from "./scenario-panel"

/** Mirrors MAX_BOOK_POCKETS in the contract (a test keeps them equal). */
export const MAX_POCKETS = 20
/** The brief: "a book split across ten pockets of 10% each". */
export const BRIEF_POCKETS = 10
const BRIEF_WEIGHT_PERCENT = "10"
const FALLBACK_AMOUNT = "1000"
const FALLBACK_NUMERAIRE = "USDC"
const SCALE = BigInt(10) ** BigInt(18)
const HUNDRED_PERCENT = BigInt(100) * SCALE

// UNAVAILABLE is a data-quality state; it must never look like a safe result.
const DECISION_TONES: Record<RecommendationDecision, "safe" | "danger" | "unavailable"> = {
  STAY: "safe",
  EXIT: "danger",
  UNAVAILABLE: "unavailable",
}
const DECISION_MARKERS: Record<RecommendationDecision, string> = { STAY: "✓", EXIT: "×", UNAVAILABLE: "?" }

export interface PocketDraft {
  key: number
  entityId: string
  mode: PositionMode
  /** Lender deposit. */
  amount: string
  collateral: string
  debt: string
  ltvPercent: string
  numeraire: string
  /** Share of the book as a percentage, e.g. "10". */
  weightPercent: string
}

type DraftField = "amount" | "collateral" | "debt" | "ltvPercent" | "numeraire" | "weightPercent"
type DraftErrors = Partial<Record<DraftField | "entityId", string>>

type RunState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "error"; message: string }
  | { status: "done"; result: BookResponse }

/** Percent string with up to 18 fraction digits as a scaled BigInt (exact, no floats). */
function scaledPercent(value: string): bigint {
  const [whole, fraction = ""] = normalizeDecimal(value).split(".")
  return BigInt(whole) * SCALE + BigInt(fraction.slice(0, 18).padEnd(18, "0"))
}

function scaledToPercent(value: bigint): string {
  const fraction = (value % SCALE).toString().padStart(18, "0").replace(/0+$/, "")
  return `${value / SCALE}${fraction ? `.${fraction}` : ""}`
}

function totalWeightPercent(drafts: PocketDraft[]): bigint {
  return drafts.reduce((sum, draft) => sum + (isPositiveDecimal(normalizeDecimal(draft.weightPercent)) ? scaledPercent(draft.weightPercent) : BigInt(0)), BigInt(0))
}

function draftFor(entry: HydratedCatalogEntry, key: number, weightPercent: string): PocketDraft {
  const preset = POSITION_PRESETS[entry.entity_id]
  const mode: PositionMode = entry.supported_modes.includes("lender") ? "lender" : "borrower"
  return {
    key,
    entityId: entry.entity_id,
    mode,
    amount: preset?.lender_amount ?? FALLBACK_AMOUNT,
    collateral: preset?.borrower?.collateral_amount ?? "",
    debt: preset?.borrower?.debt_amount ?? "",
    ltvPercent: preset?.borrower ? ratioToPercent(preset.borrower.ltv) : "",
    // Same rule as the position form: the live asset symbol first.
    numeraire: entry.metrics?.asset_symbol ?? preset?.numeraire ?? FALLBACK_NUMERAIRE,
    weightPercent,
  }
}

/**
 * The brief's framing: ten pockets of 10% each, built from the analyzable
 * catalog entries that accept a lender position. Entries with a curated
 * position preset come first (in catalog order), then the other analyzable
 * entries; each pocket is a lender deposit of the preset amount (or 1,000 of
 * the asset) in the entry's asset. With fewer than ten such entries the list
 * repeats from the start so the book still has ten 10% pockets.
 */
export function briefExampleDrafts(entries: HydratedCatalogEntry[], firstKey = 0): PocketDraft[] {
  const candidates = entries.filter((entry) => canAnalyze(entry) && entry.supported_modes.includes("lender"))
  const ordered = [
    ...candidates.filter((entry) => entry.entity_id in POSITION_PRESETS),
    ...candidates.filter((entry) => !(entry.entity_id in POSITION_PRESETS)),
  ]
  if (ordered.length === 0) return []
  return Array.from({ length: BRIEF_POCKETS }, (_, index) => ({
    ...draftFor(ordered[index % ordered.length], firstKey + index, BRIEF_WEIGHT_PERCENT),
    mode: "lender" as const,
  }))
}

function amountError(value: string): string | undefined {
  return isPositiveDecimal(normalizeDecimal(value)) ? undefined : "Enter a positive number"
}

export function validateDraft(
  draft: PocketDraft,
  entry: HydratedCatalogEntry | undefined,
  otherWeightPercent: bigint,
): { errors: DraftErrors; pocket?: BookPocketInput } {
  const errors: DraftErrors = {}
  if (!entry || !canAnalyze(entry)) errors.entityId = "Choose an analyzable catalog entry"
  const numeraire = draft.numeraire.trim()
  if (!numeraire) errors.numeraire = "Enter a numeraire"
  const weight = normalizeDecimal(draft.weightPercent)
  if (!isPositiveDecimal(weight) || compareDecimal(weight, "100") === 1) {
    errors.weightPercent = "Enter a weight above 0% and at most 100%"
  } else if (otherWeightPercent + scaledPercent(weight) > HUNDRED_PERCENT) {
    errors.weightPercent = "Pocket weights must add up to at most 100% of the book"
  }
  let position: BookPocketInput["position"] | undefined
  if (draft.mode === "lender") {
    const amount = amountError(draft.amount)
    if (amount) errors.amount = amount
    else position = { mode: "lender", entity_id: draft.entityId, amount: normalizeDecimal(draft.amount), numeraire }
  } else {
    const collateral = amountError(draft.collateral)
    const debt = amountError(draft.debt)
    if (collateral) errors.collateral = collateral
    if (debt) errors.debt = debt
    const ltvPercent = normalizeDecimal(draft.ltvPercent)
    let ltv: string | undefined
    if (!isPositiveDecimal(ltvPercent)) errors.ltvPercent = "Enter a positive LTV"
    else {
      ltv = percentToRatio(ltvPercent)
      if (entry?.lltv && compareDecimal(ltv, entry.lltv) >= 0) {
        errors.ltvPercent = `LTV must be below the market LLTV of ${formatPercent(entry.lltv)}`
      }
    }
    if (!collateral && !debt && ltv && !errors.ltvPercent) {
      const debtValue = normalizeDecimal(draft.debt)
      position = {
        mode: "borrower",
        entity_id: draft.entityId,
        amount: debtValue,
        collateral_amount: normalizeDecimal(draft.collateral),
        debt_amount: debtValue,
        ltv,
        numeraire,
      }
    }
  }
  if (Object.keys(errors).length || !position) return { errors }
  return { errors, pocket: { pocket_id: `pocket-${draft.key + 1}`, position, weight: percentToRatio(weight) } }
}

function DraftField({
  label,
  value,
  error,
  onChange,
  inputMode = "decimal",
}: {
  label: string
  value: string
  error?: string
  onChange: (value: string) => void
  inputMode?: "decimal" | "text"
}) {
  const id = useId()
  return (
    <div className="field field--inline">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        inputMode={inputMode}
        autoComplete="off"
        value={value}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && (
        <span id={`${id}-error`} className="field__error">
          {error}
        </span>
      )}
    </div>
  )
}

function fractionText(value: string | null): string {
  return value === null ? "unknown" : formatProbability(value)
}

function AmountCell({ amount, numeraire }: { amount: BookAmount | null; numeraire: string }) {
  if (!amount) return <td className="book__unknown">Unknown</td>
  return (
    <td>
      {`${formatQuantity(amount.amount)} ${numeraire}`}
      <small>{`${fractionText(amount.fraction_of_pocket)} of pocket · ${fractionText(amount.fraction_of_book)} of book`}</small>
    </td>
  )
}

export function breakEvenCompact(pocket: BookPocketResult): string {
  const breakEven = pocket.break_even
  if (!breakEven) return "Unavailable"
  if (!breakEven.probability_dependent) return "Hard trigger: not probability-dependent"
  const point = breakEvenPoint(breakEven)
  if (!point) return "No scenario impact"
  const threshold = breakEvenThreshold(point)
  const estimate = point.current_probability === null ? "unavailable" : formatProbability(point.current_probability)
  if (threshold === null) return `Unknown · estimate ${estimate}`
  if (compareDecimal(threshold, "1") >= 0) return `EXIT never cheaper · estimate ${estimate}`
  if (compareDecimal(threshold, "0") <= 0) return `EXIT at any probability · estimate ${estimate}`
  return `EXIT above ${formatProbability(threshold)} · estimate ${estimate}`
}

function TotalRow({ label, total, numeraire }: { label: string; total: BookTotal; numeraire: string | null }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{total.fraction_of_book === null ? "Unknown" : formatProbability(total.fraction_of_book)}</td>
      <td>{total.amount === null || numeraire === null ? "—" : `${formatQuantity(total.amount)} ${numeraire}`}</td>
    </tr>
  )
}

function BookResult({ result, names }: { result: BookResponse; names: Map<string, string> }) {
  const { totals } = result
  return (
    <div className="book__result">
      <p role="status" className="book__summary">
        {`Book analyzed: ${totals.stay_count} STAY, ${totals.exit_count} EXIT, ${totals.unavailable_count} UNAVAILABLE.`}
      </p>
      <div className="book__table-wrap">
        <table className="components book__table">
          <caption>Pockets ranked by expected loss minus exit cost and forgone yield (largest first)</caption>
          <thead>
            <tr>
              <th scope="col">Rank</th>
              <th scope="col">Pocket</th>
              <th scope="col">Weight</th>
              <th scope="col">Decision</th>
              <th scope="col">Expected 30-day loss</th>
              <th scope="col">Exit cost</th>
              <th scope="col">Forgone 30-day yield</th>
              <th scope="col">Break-even vs estimate</th>
            </tr>
          </thead>
          <tbody>
            {result.pockets.map((pocket) => {
              const tone = DECISION_TONES[pocket.decision]
              return (
                <tr key={pocket.pocket_id} data-decision={pocket.decision}>
                  <td>{pocket.rank}</td>
                  <th scope="row">
                    {names.get(pocket.entity_id) ?? pocket.entity_id}
                    <small>{`${pocket.mode} · ${pocket.pocket_id}`}</small>
                    {pocket.decision === "UNAVAILABLE" && pocket.unavailable_reasons.length > 0 && (
                      <small className="book__reason">{pocket.unavailable_reasons.join(" ")}</small>
                    )}
                  </th>
                  <td>{formatPercent(pocket.weight)}</td>
                  <td>
                    <span className={`book-decision book-decision--${tone}`} data-tone={tone}>
                      <span aria-hidden="true">{DECISION_MARKERS[pocket.decision]}</span> {pocket.decision}
                    </span>
                    {pocket.decision === "UNAVAILABLE" && <small>Not a recommendation to stay</small>}
                  </td>
                  <AmountCell amount={pocket.expected_loss} numeraire={pocket.numeraire} />
                  <AmountCell amount={pocket.exit_cost} numeraire={pocket.numeraire} />
                  <AmountCell amount={pocket.forgone_yield ?? null} numeraire={pocket.numeraire} />
                  <td>{breakEvenCompact(pocket)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="book__table-wrap">
      <table className="components book__totals" aria-label="Book totals">
        <caption>Book totals</caption>
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">Share of book</th>
            <th scope="col">Amount</th>
          </tr>
        </thead>
        <tbody>
          <TotalRow label="Expected loss if staying in every pocket" total={totals.expected_loss_staying} numeraire={totals.numeraire} />
          <TotalRow label="Cost of exiting the EXIT pockets" total={totals.exit_cost_exit_pockets} numeraire={totals.numeraire} />
          {totals.forgone_yield_exit_pockets && (
            <TotalRow label="Yield forgone by exiting the EXIT pockets" total={totals.forgone_yield_exit_pockets} numeraire={totals.numeraire} />
          )}
          <TotalRow label="Expected loss of the STAY pockets" total={totals.expected_loss_stay_pockets} numeraire={totals.numeraire} />
          <TotalRow label="Book cost following the recommendations" total={totals.recommended_cost} numeraire={totals.numeraire} />
          <TotalRow label="Expected saving versus staying everywhere" total={totals.net_benefit} numeraire={totals.numeraire} />
        </tbody>
      </table>
      </div>
      <p className="hint">
        {`Allocated ${formatPercent(totals.allocated_weight)} of the book · STAY ${formatPercent(totals.stay_weight)} · EXIT ${formatPercent(totals.exit_weight)} · UNAVAILABLE ${formatPercent(totals.unavailable_weight)}`}
      </p>
      {!totals.complete && (
        <p className="book__incomplete">Totals are incomplete: unknown values are excluded, never counted as zero.</p>
      )}
      {totals.warnings.length > 0 && (
        <ul className="warnings" aria-label="Book warnings">
          {totals.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function BookPanel({
  entries,
  runBook = runBookRequest,
}: {
  entries: HydratedCatalogEntry[]
  /** Injectable for tests; defaults to the same-origin proxy. */
  runBook?: (request: BookRequest) => Promise<BookResponse>
}) {
  const analyzable = useMemo(() => entries.filter(canAnalyze), [entries])
  const names = useMemo(() => new Map(entries.map((entry) => [entry.entity_id, entry.name])), [entries])
  const nextKey = useRef(0)
  const runToken = useRef(0)
  const [draft, setDraft] = useState<PocketDraft | null>(() =>
    analyzable[0] ? draftFor(analyzable[0], 0, BRIEF_WEIGHT_PERCENT) : null,
  )
  const [errors, setErrors] = useState<DraftErrors>({})
  const [pockets, setPockets] = useState<PocketDraft[]>([])
  const [run, setRun] = useState<RunState>({ status: "idle" })
  const [notice, setNotice] = useState<string | null>(null)
  const entryId = useId()
  const modeId = useId()
  const draftEntry = draft ? entries.find((entry) => entry.entity_id === draft.entityId) : undefined
  const allocated = totalWeightPercent(pockets)

  function allocate(count: number): number {
    const first = nextKey.current
    nextKey.current += count
    return first
  }

  function selectEntry(entityId: string) {
    const entry = entries.find((item) => item.entity_id === entityId)
    if (!entry) return
    setDraft((current) => draftFor(entry, current?.key ?? 0, current?.weightPercent ?? BRIEF_WEIGHT_PERCENT))
    setErrors({})
  }

  const update = (field: DraftField) => (value: string) =>
    setDraft((current) => (current ? { ...current, [field]: value } : current))

  function addPocket(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    if (pockets.length >= MAX_POCKETS) {
      setErrors({ entityId: `A book has at most ${MAX_POCKETS} pockets` })
      return
    }
    const result = validateDraft(draft, draftEntry, allocated)
    setErrors(result.errors)
    if (!result.pocket) return
    setPockets((current) => [...current, { ...draft, key: allocate(1) }])
    setNotice(`Pocket added: ${names.get(draft.entityId) ?? draft.entityId}, ${formatPercent(percentToRatio(normalizeDecimal(draft.weightPercent)))} of the book.`)
  }

  function loadBriefExample() {
    const example = briefExampleDrafts(entries, allocate(BRIEF_POCKETS))
    if (example.length === 0) {
      setNotice("No analyzable lender entry is available to build the example.")
      return
    }
    setPockets(example)
    setRun({ status: "idle" })
    setNotice(`Loaded the brief example: ${example.length} pockets of 10% each.`)
  }

  async function analyzeBook() {
    const scenarios = toScenarioSelections(defaultScenarioDrafts()).selections
    const built: BookPocketInput[] = []
    let otherWeight = BigInt(0)
    for (const pocket of pockets) {
      const entry = entries.find((item) => item.entity_id === pocket.entityId)
      const result = validateDraft(pocket, entry, otherWeight)
      if (!result.pocket) {
        setRun({ status: "error", message: `Pocket ${names.get(pocket.entityId) ?? pocket.entityId} is invalid: ${Object.values(result.errors).join(" ")}` })
        return
      }
      otherWeight += scaledPercent(pocket.weightPercent)
      built.push(result.pocket)
    }
    if (built.length === 0 || scenarios.length === 0) return
    const token = ++runToken.current
    setRun({ status: "running" })
    try {
      const result = await runBook({
        pockets: built as [BookPocketInput, ...BookPocketInput[]],
        scenarios: scenarios as [(typeof scenarios)[number], ...typeof scenarios],
      })
      if (token !== runToken.current) return
      setRun({ status: "done", result })
    } catch (error) {
      if (token !== runToken.current) return
      setRun({ status: "error", message: error instanceof Error ? error.message : "The book analysis failed" })
    }
  }

  return (
    <section className="panel book" aria-labelledby="book-heading">
      <h2 id="book-heading">Book</h2>
      <p className="hint">
        Split a book into pockets, each a position with its share of the book. Every pocket runs the full analysis with
        the default scenarios; the table ranks pockets by expected loss minus exit cost, as amounts and as shares of the
        pocket and of the book.
      </p>

      {draft ? (
        <form className="book__add" onSubmit={addPocket} noValidate aria-label="Add a pocket">
          <div className="field field--inline">
            <label htmlFor={entryId}>Catalog entry</label>
            <select
              id={entryId}
              value={draft.entityId}
              aria-invalid={errors.entityId ? true : undefined}
              onChange={(event) => selectEntry(event.target.value)}
            >
              {analyzable.map((entry) => (
                <option key={entry.entity_id} value={entry.entity_id}>
                  {entry.name}
                </option>
              ))}
            </select>
            {errors.entityId && <span className="field__error">{errors.entityId}</span>}
          </div>
          <div className="field field--inline">
            <label htmlFor={modeId}>Mode</label>
            <select
              id={modeId}
              value={draft.mode}
              onChange={(event) => {
                const mode = event.target.value as PositionMode
                setDraft((current) => (current ? { ...current, mode } : current))
                setErrors({})
              }}
            >
              {(draftEntry?.supported_modes ?? ["lender"]).map((mode) => (
                <option key={mode} value={mode}>
                  {mode === "lender" ? "Lender" : "Borrower"}
                </option>
              ))}
            </select>
          </div>
          {draft.mode === "lender" ? (
            <DraftField label="Pocket amount" value={draft.amount} error={errors.amount} onChange={update("amount")} />
          ) : (
            <>
              <DraftField label="Collateral amount" value={draft.collateral} error={errors.collateral} onChange={update("collateral")} />
              <DraftField label="Debt amount" value={draft.debt} error={errors.debt} onChange={update("debt")} />
              <DraftField label="Current LTV (%)" value={draft.ltvPercent} error={errors.ltvPercent} onChange={update("ltvPercent")} />
            </>
          )}
          <DraftField label="Pocket numeraire" value={draft.numeraire} error={errors.numeraire} onChange={update("numeraire")} inputMode="text" />
          <DraftField label="Weight (% of book)" value={draft.weightPercent} error={errors.weightPercent} onChange={update("weightPercent")} />
          <div className="actions">
            <button type="submit" className="button button--secondary">Add pocket</button>
          </div>
        </form>
      ) : (
        <p className="hint">No catalog entry is analyzable right now, so no pocket can be added.</p>
      )}

      <div className="actions">
        <button type="button" className="button button--secondary" onClick={loadBriefExample} disabled={analyzable.length === 0}>
          Load brief example
        </button>
        <button
          type="button"
          className="button button--secondary"
          onClick={() => {
            setPockets([])
            setRun({ status: "idle" })
            setNotice("All pockets removed.")
          }}
          disabled={pockets.length === 0}
        >
          Clear pockets
        </button>
        <button type="button" className="button" onClick={() => void analyzeBook()} disabled={pockets.length === 0 || run.status === "running"}>
          {run.status === "running" ? "Running book analysis…" : "Run book analysis"}
        </button>
      </div>
      <p role="status" className="hint">{notice ?? ""}</p>

      {pockets.length > 0 && (
        <div className="book__table-wrap">
        <table className="components book__pockets">
          <caption>{`Pockets to analyze (${pockets.length}, ${formatPercent(percentToRatio(scaledToPercent(allocated)))} of the book)`}</caption>
          <thead>
            <tr>
              <th scope="col">Pocket</th>
              <th scope="col">Mode</th>
              <th scope="col">Size</th>
              <th scope="col">Weight</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {pockets.map((pocket) => {
              const name = names.get(pocket.entityId) ?? pocket.entityId
              return (
                <tr key={pocket.key}>
                  <th scope="row">{name}</th>
                  <td>{pocket.mode}</td>
                  <td>
                    {pocket.mode === "lender"
                      ? `${pocket.amount} ${pocket.numeraire}`
                      : `${pocket.collateral} collateral · ${pocket.debt} debt · LTV ${pocket.ltvPercent}%`}
                  </td>
                  <td>{`${pocket.weightPercent}%`}</td>
                  <td>
                    <button
                      type="button"
                      className="link-button"
                      aria-label={`Remove pocket ${name} (${pocket.weightPercent}%)`}
                      onClick={() => setPockets((current) => current.filter((item) => item.key !== pocket.key))}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        </div>
      )}

      {run.status === "running" && <p role="status">Running the analysis of every pocket…</p>}
      {run.status === "error" && <p role="alert" className="alert">{run.message}</p>}
      {run.status === "done" && <BookResult result={run.result} names={names} />}
    </section>
  )
}
