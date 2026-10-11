"use client"

import type { ExposureCreateRequest, PositionMode, RadarEntity } from "@risk0/contracts"
import { FormEvent, useEffect, useMemo, useState } from "react"

/** A target chosen outside the form (e.g. "Evaluate exposure" on a graph node); a new `key` re-applies it. */
export interface ExposureTargetSelection {
  entityId: string
  key: number
}

import { compareDecimal, isPositiveDecimal, normalizeDecimal, percentToRatio } from "../lib/format"

type FieldErrors = Partial<Record<"amount" | "collateral" | "debt" | "ltv" | "numeraire", string>>

const lenderPreset = { amount: "100000", numeraire: "USDC" }
const borrowerPreset = { collateral: "100000", debt: "75000", ltv: "75", numeraire: "USDC" }

// Amounts are exact decimal strings in the contract; never route them through floats.
function positive(value: string): boolean {
  return isPositiveDecimal(value)
}

export function ExposureRail({
  entities,
  onSubmit,
  busy = false,
  selection = null,
  numeraires,
}: {
  entities: RadarEntity[]
  onSubmit: (request: ExposureCreateRequest) => void | Promise<void>
  busy?: boolean
  selection?: ExposureTargetSelection | null
  /** Asset symbol per target whose amounts the analysis prices (catalog entries). */
  numeraires?: ReadonlyMap<string, string>
}) {
  const [mode, setMode] = useState<PositionMode>("lender")
  // The risk service accepts lenders on vaults or markets, but borrowers only on markets.
  const targets = useMemo(
    () =>
      entities.filter(
        (entity) => entity.entity_type === "market" || (mode === "lender" && entity.entity_type === "vault"),
      ),
    [entities, mode],
  )
  const [entityId, setEntityId] = useState(targets[0]?.entity_id ?? "")
  const [amount, setAmount] = useState(lenderPreset.amount)
  const [numeraire, setNumeraire] = useState(lenderPreset.numeraire)
  const [collateral, setCollateral] = useState(borrowerPreset.collateral)
  const [debt, setDebt] = useState(borrowerPreset.debt)
  const [ltv, setLtv] = useState(borrowerPreset.ltv)
  const [errors, setErrors] = useState<FieldErrors>({})

  useEffect(() => {
    if (!targets.some((target) => target.entity_id === entityId)) {
      setEntityId(targets[0]?.entity_id ?? "")
    }
  }, [entityId, targets])

  // Adopt a target chosen on the graph. A vault takes lenders only.
  useEffect(() => {
    if (!selection) return
    const target = entities.find((entity) => entity.entity_id === selection.entityId)
    if (!target || (target.entity_type !== "market" && target.entity_type !== "vault")) return
    if (target.entity_type === "vault") setMode("lender")
    setEntityId(target.entity_id)
    const symbol = numeraires?.get(target.entity_id)
    if (symbol) setNumeraire(symbol)
    // Only a new selection re-applies; entity refreshes must not reset the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection])

  function chooseTarget(nextId: string) {
    setEntityId(nextId)
    const symbol = numeraires?.get(nextId)
    if (symbol) setNumeraire(symbol)
  }

  function reset(nextMode = mode) {
    setNumeraire(numeraires?.get(entityId) ?? (nextMode === "lender" ? lenderPreset.numeraire : borrowerPreset.numeraire))
    setAmount(nextMode === "lender" ? lenderPreset.amount : borrowerPreset.debt)
    setCollateral(borrowerPreset.collateral)
    setDebt(borrowerPreset.debt)
    setLtv(borrowerPreset.ltv)
    setErrors({})
  }

  function chooseMode(nextMode: PositionMode) {
    setMode(nextMode)
    reset(nextMode)
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const nextErrors: FieldErrors = {}
    const amountValue = normalizeDecimal(amount)
    const collateralValue = normalizeDecimal(collateral)
    const debtValue = normalizeDecimal(debt)
    const ltvValue = normalizeDecimal(ltv)
    const numeraireValue = numeraire.trim()
    if (!numeraireValue) nextErrors.numeraire = "Enter a numeraire"
    if (mode === "lender" && !positive(amountValue)) nextErrors.amount = "Enter a positive number"
    if (mode === "borrower" && !positive(collateralValue)) nextErrors.collateral = "Enter a positive number"
    if (mode === "borrower" && !positive(debtValue)) nextErrors.debt = "Enter a positive number"
    if (mode === "borrower" && (!positive(ltvValue) || compareDecimal(ltvValue, "100") >= 0)) {
      nextErrors.ltv = "Enter an LTV greater than 0% and below 100%"
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0 || !entityId) return

    void onSubmit({
      entity_id: entityId,
      mode,
      amount: mode === "lender" ? amountValue : debtValue,
      numeraire: numeraireValue,
      collateral_amount: mode === "borrower" ? collateralValue : null,
      debt_amount: mode === "borrower" ? debtValue : null,
      ltv: mode === "borrower" ? percentToRatio(ltvValue) : null,
    })
  }

  const errorCount = Object.keys(errors).length
  return (
    <section className="panel exposure-rail" aria-labelledby="exposure-heading">
      <span className="eyebrow">MANUAL PORTFOLIO INPUT</span>
      <h2 id="exposure-heading">Exposure</h2>
      <form onSubmit={submit} noValidate>
        <label>Exposure target
          <select id="exposure-target" value={entityId} onChange={(event) => chooseTarget(event.target.value)}>
            {targets.map((entity) => <option key={entity.entity_id} value={entity.entity_id}>{entity.label}</option>)}
          </select>
        </label>
        <fieldset>
          <legend>Position mode</legend>
          <label><input name="exposure-mode" type="radio" checked={mode === "lender"} onChange={() => chooseMode("lender")} /> Lender exposure</label>
          <label><input name="exposure-mode" type="radio" checked={mode === "borrower"} onChange={() => chooseMode("borrower")} /> Borrower exposure</label>
        </fieldset>
        {mode === "lender" ? (
          <div className="field"><label htmlFor="exposure-amount">Exposure amount</label>
            <input id="exposure-amount" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} aria-invalid={errors.amount ? true : undefined} aria-describedby={errors.amount ? "exposure-amount-error" : undefined} />
            {errors.amount ? <span id="exposure-amount-error">{errors.amount}</span> : null}
          </div>
        ) : (
          <>
            <div className="field"><label htmlFor="exposure-collateral">Collateral amount</label>
              <input id="exposure-collateral" inputMode="decimal" aria-invalid={errors.collateral ? true : undefined} value={collateral} onChange={(event) => setCollateral(event.target.value)} aria-describedby={errors.collateral ? "exposure-collateral-error" : undefined} />
              {errors.collateral ? <span id="exposure-collateral-error">{errors.collateral}</span> : null}
            </div>
            <div className="field"><label htmlFor="exposure-debt">Debt amount</label>
              <input id="exposure-debt" inputMode="decimal" aria-invalid={errors.debt ? true : undefined} value={debt} onChange={(event) => setDebt(event.target.value)} aria-describedby={errors.debt ? "exposure-debt-error" : undefined} />
              {errors.debt ? <span id="exposure-debt-error">{errors.debt}</span> : null}
            </div>
            <div className="field"><label htmlFor="exposure-ltv">Borrower LTV (%)</label>
              <input id="exposure-ltv" inputMode="decimal" aria-invalid={errors.ltv ? true : undefined} value={ltv} onChange={(event) => setLtv(event.target.value)} aria-describedby={errors.ltv ? "exposure-ltv-error" : undefined} />
              {errors.ltv ? <span id="exposure-ltv-error">{errors.ltv}</span> : null}
            </div>
          </>
        )}
        <div className="field"><label htmlFor="exposure-numeraire">Exposure numeraire</label>
          <input id="exposure-numeraire" value={numeraire} onChange={(event) => setNumeraire(event.target.value)} aria-invalid={errors.numeraire ? true : undefined} aria-describedby={errors.numeraire ? "exposure-numeraire-error" : undefined} />
          {errors.numeraire ? <span id="exposure-numeraire-error">{errors.numeraire}</span> : null}
        </div>
        {errorCount ? <p role="alert">Fix the {errorCount} highlighted fields</p> : null}
        <div className="exposure-rail__actions">
          <button type="submit" disabled={busy || targets.length === 0}>{busy ? "Evaluating…" : "Evaluate exposure"}</button>
          <button type="button" onClick={() => reset()}>Reset preset</button>
        </div>
      </form>
    </section>
  )
}
