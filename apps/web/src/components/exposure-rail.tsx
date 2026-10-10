"use client"

import type { ExposureCreateRequest, PositionMode, RadarEntity } from "@risk0/contracts"
import { FormEvent, useEffect, useState } from "react"

type FieldErrors = Partial<Record<"amount" | "collateral" | "debt" | "ltv", string>>

const lenderPreset = { amount: "100000", numeraire: "USDC" }
const borrowerPreset = { collateral: "100000", debt: "75000", ltv: "75", numeraire: "USDC" }

function positive(value: string): boolean {
  return Number.isFinite(Number(value)) && Number(value) > 0
}

export function ExposureRail({
  entities,
  onSubmit,
  busy = false,
}: {
  entities: RadarEntity[]
  onSubmit: (request: ExposureCreateRequest) => void | Promise<void>
  busy?: boolean
}) {
  const targets = entities.filter((entity) => entity.entity_type === "vault" || entity.entity_type === "market")
  const [entityId, setEntityId] = useState(targets[0]?.entity_id ?? "")
  const [mode, setMode] = useState<PositionMode>("lender")
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

  function reset(nextMode = mode) {
    setNumeraire(nextMode === "lender" ? lenderPreset.numeraire : borrowerPreset.numeraire)
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
    if (mode === "lender" && !positive(amount)) nextErrors.amount = "Enter a positive number"
    if (mode === "borrower" && !positive(collateral)) nextErrors.collateral = "Enter a positive number"
    if (mode === "borrower" && !positive(debt)) nextErrors.debt = "Enter a positive number"
    if (mode === "borrower" && (!positive(ltv) || Number(ltv) >= 100)) {
      nextErrors.ltv = "Enter an LTV greater than 0% and below 100%"
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0 || !entityId) return

    void onSubmit({
      entity_id: entityId,
      mode,
      amount: mode === "lender" ? amount : debt,
      numeraire,
      collateral_amount: mode === "borrower" ? collateral : null,
      debt_amount: mode === "borrower" ? debt : null,
      ltv: mode === "borrower" ? String(Number(ltv) / 100) : null,
    })
  }

  const errorCount = Object.keys(errors).length
  return (
    <section className="panel exposure-rail" aria-labelledby="exposure-heading">
      <span className="eyebrow">MANUAL PORTFOLIO INPUT</span>
      <h2 id="exposure-heading">Exposure</h2>
      <form onSubmit={submit} noValidate>
        <label>Exposure target
          <select value={entityId} onChange={(event) => setEntityId(event.target.value)}>
            {targets.map((entity) => <option key={entity.entity_id} value={entity.entity_id}>{entity.label}</option>)}
          </select>
        </label>
        <fieldset>
          <legend>Position mode</legend>
          <label><input name="exposure-mode" type="radio" checked={mode === "lender"} onChange={() => chooseMode("lender")} /> Lender exposure</label>
          <label><input name="exposure-mode" type="radio" checked={mode === "borrower"} onChange={() => chooseMode("borrower")} /> Borrower exposure</label>
        </fieldset>
        {mode === "lender" ? (
          <label>Exposure amount
            <input value={amount} onChange={(event) => setAmount(event.target.value)} aria-describedby={errors.amount ? "amount-error" : undefined} />
            {errors.amount ? <span id="amount-error">{errors.amount}</span> : null}
          </label>
        ) : (
          <>
            <div className="field"><label htmlFor="exposure-collateral">Collateral amount</label>
              <input id="exposure-collateral" value={collateral} onChange={(event) => setCollateral(event.target.value)} aria-describedby={errors.collateral ? "collateral-error" : undefined} />
              {errors.collateral ? <span id="collateral-error">{errors.collateral}</span> : null}
            </div>
            <div className="field"><label htmlFor="exposure-debt">Debt amount</label>
              <input id="exposure-debt" value={debt} onChange={(event) => setDebt(event.target.value)} aria-describedby={errors.debt ? "debt-error" : undefined} />
              {errors.debt ? <span id="debt-error">{errors.debt}</span> : null}
            </div>
            <div className="field"><label htmlFor="exposure-ltv">Borrower LTV (%)</label>
              <input id="exposure-ltv" value={ltv} onChange={(event) => setLtv(event.target.value)} aria-describedby={errors.ltv ? "ltv-error" : undefined} />
              {errors.ltv ? <span id="ltv-error">{errors.ltv}</span> : null}
            </div>
          </>
        )}
        <label>Exposure numeraire
          <input value={numeraire} onChange={(event) => setNumeraire(event.target.value)} />
        </label>
        {errorCount ? <p role="alert">Fix the {errorCount} highlighted fields</p> : null}
        <div className="exposure-rail__actions">
          <button type="submit" disabled={busy || targets.length === 0}>{busy ? "Adding…" : "Add exposure"}</button>
          <button type="button" onClick={() => reset()}>Reset preset</button>
        </div>
      </form>
    </section>
  )
}
