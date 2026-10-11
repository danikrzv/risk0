"use client"

import type { AnalysisResponse, PositionMode } from "@risk0/contracts"

import { formatQuantity } from "../lib/format"

export type ExposureEvaluationState =
  | { status: "idle" }
  | { status: "running"; label: string }
  /** The exposure is recorded, but the 30-day analysis cannot run for this target. */
  | { status: "unavailable"; label: string; reason: string }
  | { status: "error"; label: string; message: string }
  | {
      status: "done"
      label: string
      mode: PositionMode
      catalogId: string
      result: AnalysisResponse
      numeraire: string
      receivedAt: number
    }

// UNAVAILABLE is a data-quality state; it must never look like a safe result.
const TONES = { STAY: "safe", EXIT: "danger", UNAVAILABLE: "unavailable" } as const
const MARKERS = { STAY: "✓", EXIT: "×", UNAVAILABLE: "?" } as const

/**
 * The stay-or-exit answer for the exposure just evaluated: expected 30-day loss
 * if staying against the cost of exiting now, the decision, and why.
 */
export function ExposureEvaluation({
  state,
  onViewFullAnalysis,
}: {
  state: ExposureEvaluationState
  onViewFullAnalysis: () => void
}) {
  if (state.status === "idle") return null
  if (state.status === "running") {
    return <p role="status" className="exposure-evaluation__pending">Evaluating stay or exit for {state.label}…</p>
  }
  if (state.status === "unavailable") {
    return <p role="status" className="exposure-evaluation__pending">Stay/exit evaluation unavailable: {state.reason}</p>
  }
  if (state.status === "error") {
    return <p role="alert" className="alert">Stay/exit evaluation failed: {state.message}</p>
  }

  const { recommendation, exit_estimate: exit } = state.result
  const unit = state.numeraire
  const amount = (value: string) => `${formatQuantity(value)} ${unit}`
  const tone = TONES[recommendation.decision]
  const forgone = recommendation.forgone_yield ?? null
  const costOfExiting = recommendation.cost_of_exiting ?? recommendation.exit_cost
  const hasRange = recommendation.expected_loss_low != null && recommendation.expected_loss_high != null

  return (
    <section className="exposure-evaluation" data-tone={tone} aria-labelledby="exposure-evaluation-heading">
      <h3 id="exposure-evaluation-heading">Stay or exit · {state.label}</h3>
      <p className="exposure-evaluation__decision">
        <span aria-hidden="true">{MARKERS[recommendation.decision]}</span> {recommendation.decision}
        <small> · {state.mode} · confidence {recommendation.confidence}</small>
      </p>
      {recommendation.decision === "UNAVAILABLE" ? (
        <p className="exposure-evaluation__notice">
          Critical data is missing or too stale for a defensible decision. This is not a recommendation to stay.
        </p>
      ) : null}
      {recommendation.decision === "EXIT" && !exit.withdrawable ? (
        <p className="exposure-evaluation__notice">Exit recommended, but full exit is currently unavailable.</p>
      ) : null}
      <dl>
        <dt>Expected 30-day loss if staying</dt>
        <dd>
          <strong>{amount(recommendation.expected_loss)}</strong>
          {hasRange ? ` (${formatQuantity(recommendation.expected_loss_low!)} – ${amount(recommendation.expected_loss_high!)})` : ""}
        </dd>
        <dt>Cost of exiting now</dt>
        <dd>
          <strong>{amount(costOfExiting)}</strong>
          {forgone !== null
            ? ` = exit cost ${amount(recommendation.exit_cost)} + forgone 30-day yield ${amount(forgone)}`
            : " (exit cost: slippage, fees, gas, price impact)"}
        </dd>
      </dl>
      <p className="exposure-evaluation__why">{recommendation.explanation}</p>
      {recommendation.triggers.length > 0 ? (
        <ul className="triggers" aria-label="Hard safety triggers">
          {recommendation.triggers.map((trigger) => <li key={trigger}>{trigger}</li>)}
        </ul>
      ) : null}
      <button type="button" className="button button--secondary" onClick={onViewFullAnalysis}>
        View full analysis
      </button>
    </section>
  )
}
