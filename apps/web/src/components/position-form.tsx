"use client"

import type { HydratedCatalogEntry, PositionInput, PositionMode } from "@risk0/contracts"
import { type FormEvent, type ReactNode, useId, useState } from "react"

import { canAnalyze } from "./catalog-selector"
import { compareDecimal, formatPercent, isPositiveDecimal, percentToRatio, ratioToPercent } from "../lib/format"

const MAX_AMOUNT = "1000000000000000"
const MAX_AMOUNT_LABEL = "1,000,000,000,000,000"

interface BorrowerPreset {
  collateral_amount: string
  debt_amount: string
  ltv: string
}

export interface PositionPreset {
  numeraire: string
  lender_amount: string
  borrower?: BorrowerPreset
}

// Illustrative starting points. Borrower LTVs sit below LLTV but close enough
// that the default 15% collateral shock liquidates while milder shocks do not.
// Debt amounts are approximate; the risk service values them at live prices.
export const POSITION_PRESETS: Record<string, PositionPreset> = {
  "belem-rwa-eurcv": { numeraire: "EURCV", lender_amount: "100000" },
  "mev-capital-weth": { numeraire: "WETH", lender_amount: "100" },
  "wsteth-weth-965": {
    numeraire: "WETH",
    lender_amount: "100",
    borrower: { collateral_amount: "10", debt_amount: "10.8", ltv: "0.90" },
  },
  "weth-usdc-860": {
    numeraire: "USDC",
    lender_amount: "100000",
    borrower: { collateral_amount: "2.5", debt_amount: "3750", ltv: "0.75" },
  },
  "wbtc-usdc-860": {
    numeraire: "USDC",
    lender_amount: "100000",
    borrower: { collateral_amount: "0.1", debt_amount: "4500", ltv: "0.75" },
  },
}

const FALLBACK_PRESET: PositionPreset = { numeraire: "USDC", lender_amount: "1000" }

interface Draft {
  mode: PositionMode
  amount: string
  numeraire: string
  collateral: string
  debt: string
  ltvPercent: string
}

type Field = "amount" | "numeraire" | "collateral" | "debt" | "ltvPercent"
type Errors = Partial<Record<Field, string>>

function presetDraft(entry: HydratedCatalogEntry, mode: PositionMode): Draft {
  const preset = POSITION_PRESETS[entry.entity_id] ?? FALLBACK_PRESET
  return {
    mode,
    amount: preset.lender_amount,
    // Prefer the live asset symbol reported by the risk service.
    numeraire: entry.metrics?.asset_symbol ?? preset.numeraire,
    collateral: preset.borrower?.collateral_amount ?? "",
    debt: preset.borrower?.debt_amount ?? "",
    ltvPercent: preset.borrower ? ratioToPercent(preset.borrower.ltv) : "",
  }
}

function amountError(value: string): string | undefined {
  if (!isPositiveDecimal(value)) return "Enter a positive number"
  if (compareDecimal(value, MAX_AMOUNT) === 1) return `Enter at most ${MAX_AMOUNT_LABEL}`
  return undefined
}

function validate(draft: Draft, entry: HydratedCatalogEntry): { errors: Errors; position?: PositionInput } {
  const errors: Errors = {}
  if (!draft.numeraire.trim()) errors.numeraire = "Enter a numeraire"

  if (draft.mode === "lender") {
    const amount = amountError(draft.amount)
    if (amount) errors.amount = amount
    if (Object.keys(errors).length) return { errors }
    return {
      errors,
      position: {
        mode: "lender",
        entity_id: entry.entity_id,
        amount: draft.amount,
        numeraire: draft.numeraire.trim(),
      },
    }
  }

  const collateral = amountError(draft.collateral)
  if (collateral) errors.collateral = collateral
  const debt = amountError(draft.debt)
  if (debt) errors.debt = debt
  let ltv: string | undefined
  if (!isPositiveDecimal(draft.ltvPercent)) {
    errors.ltvPercent = "Enter a positive LTV"
  } else {
    ltv = percentToRatio(draft.ltvPercent)
    if (entry.lltv && compareDecimal(ltv, entry.lltv) >= 0) {
      errors.ltvPercent = `LTV must be below the market LLTV of ${formatPercent(entry.lltv)}`
    }
  }
  if (Object.keys(errors).length || !ltv) return { errors }
  return {
    errors,
    position: {
      mode: "borrower",
      entity_id: entry.entity_id,
      // The contract's borrower example sizes the position by its debt.
      amount: draft.debt,
      collateral_amount: draft.collateral,
      debt_amount: draft.debt,
      ltv,
      numeraire: draft.numeraire.trim(),
    },
  }
}

function TextField({
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
    <div className="field">
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

export function PositionForm({
  entry,
  onSubmit,
  busy = false,
  children,
}: {
  entry: HydratedCatalogEntry
  onSubmit: (position: PositionInput) => void
  busy?: boolean
  /** Rendered between the position fields and the submit actions. */
  children?: ReactNode
}) {
  const [draft, setDraft] = useState<Draft>(() => presetDraft(entry, entry.supported_modes[0] ?? "lender"))
  const [errors, setErrors] = useState<Errors>({})
  const supportsBorrower = entry.supported_modes.includes("borrower")

  const update = (field: Field) => (value: string) => setDraft((current) => ({ ...current, [field]: value }))

  function setMode(mode: PositionMode) {
    setDraft((current) => ({ ...current, mode }))
    setErrors({})
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const result = validate(draft, entry)
    setErrors(result.errors)
    if (result.position) onSubmit(result.position)
  }

  return (
    <form className="panel position" onSubmit={handleSubmit} noValidate>
      <h2>2. Describe the position</h2>
      {supportsBorrower && entry.supported_modes.length > 1 ? (
        <fieldset className="mode">
          <legend>Mode</legend>
          {entry.supported_modes.map((mode) => (
            <label key={mode}>
              <input
                type="radio"
                name="position-mode"
                value={mode}
                checked={draft.mode === mode}
                onChange={() => setMode(mode)}
              />
              {mode === "lender" ? "Lender" : "Borrower"}
            </label>
          ))}
        </fieldset>
      ) : (
        <p className="mode mode--fixed">{entry.supported_modes[0] === "borrower" ? "Borrower only" : "Lender only"}</p>
      )}

      {draft.mode === "lender" ? (
        <TextField label="Amount" value={draft.amount} error={errors.amount} onChange={update("amount")} />
      ) : (
        <>
          <TextField
            label="Collateral amount"
            value={draft.collateral}
            error={errors.collateral}
            onChange={update("collateral")}
          />
          <TextField label="Debt amount" value={draft.debt} error={errors.debt} onChange={update("debt")} />
          <TextField
            label="Current LTV (%)"
            value={draft.ltvPercent}
            error={errors.ltvPercent}
            onChange={update("ltvPercent")}
          />
          {entry.lltv && <p className="hint">Market LLTV {formatPercent(entry.lltv)}</p>}
        </>
      )}
      <TextField
        label="Numeraire"
        value={draft.numeraire}
        error={errors.numeraire}
        onChange={update("numeraire")}
        inputMode="text"
      />

      {children}
      {!canAnalyze(entry) && <p className="field__error">Analysis is unavailable for this entry.</p>}
      <div className="actions">
        <button
          type="button"
          className="button button--secondary"
          onClick={() => {
            setDraft(presetDraft(entry, draft.mode))
            setErrors({})
          }}
        >
          Reset to preset
        </button>
        <button type="submit" className="button" disabled={busy || !canAnalyze(entry)}>
          {busy ? "Running analysis…" : "Run analysis"}
        </button>
      </div>
    </form>
  )
}
