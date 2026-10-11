"use client"

import type { EntityHealthResponse, HealthDerivation, HealthStatus } from "@risk0/contracts"
import { useCallback, useEffect, useId, useState } from "react"

import { getEntityHealth } from "../lib/api"
import { formatTimestamp, humanize } from "../lib/format"
import {
  HEALTH_MARKER,
  conditionText,
  explainHealth,
  hysteresisText,
  metricLabel,
  observedText,
  resultText,
  scoreMapping,
  scoreSentence,
  shareText,
} from "../lib/health-explanation"

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; response: EntityHealthResponse }

function StatusMark({ status }: { status: HealthStatus }) {
  return (
    <span className="health-mark" data-health-status={status}>
      <span aria-hidden="true">{HEALTH_MARKER[status]} </span>
      {status}
    </span>
  )
}

/**
 * How the selected entity's health was computed: loaded on demand from the
 * risk service (never part of the graph payload) and refetched when the
 * entity or its recorded evaluation changes.
 */
export function HealthComputation({
  entityId,
  evaluatedAt,
  displayedStatus,
  labelFor,
  load = getEntityHealth,
}: {
  entityId: string
  /** The graph's `health_evaluated_at`; a new value means a new recorded evaluation. */
  evaluatedAt: string | null
  /** The status the radar shows, which an incident under review can override. */
  displayedStatus: HealthStatus
  labelFor: (entityId: string) => string
  load?: (entityId: string) => Promise<EntityHealthResponse>
}) {
  const headingId = useId()
  const [state, setState] = useState<LoadState>({ kind: "loading" })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    // A refresh keeps showing the previous evaluation until the new one arrives.
    setState((current) => current.kind === "loaded" && current.response.entity_id === entityId
      ? current
      : { kind: "loading" })
    load(entityId).then(
      (response) => {
        if (!active) return
        if (response && typeof response === "object" && "health" in response) {
          setState({ kind: "loaded", response })
        } else {
          setState({ kind: "error", message: "Risk service returned an invalid response" })
        }
      },
      (error: unknown) => {
        if (active) {
          setState({
            kind: "error",
            message: error instanceof Error ? error.message : "Risk service unavailable",
          })
        }
      },
    )
    return () => {
      active = false
    }
  }, [entityId, evaluatedAt, load, attempt])

  const retry = useCallback(() => setAttempt((value) => value + 1), [])

  return (
    <section
      className="radar-detail__section health-computation"
      aria-labelledby={headingId}
      aria-busy={state.kind === "loading"}
    >
      <h4 id={headingId}>Health computation</h4>
      {state.kind === "loading" ? (
        <p className="health-computation__status" role="status">Loading health computation…</p>
      ) : null}
      {state.kind === "error" ? (
        <div className="health-computation__status" role="alert">
          <p>Health computation unavailable: {state.message}</p>
          <button type="button" className="link-button" onClick={retry}>Retry</button>
        </div>
      ) : null}
      {state.kind === "loaded" ? (
        <HealthComputationBody
          response={state.response}
          displayedStatus={displayedStatus}
          labelFor={labelFor}
        />
      ) : null}
    </section>
  )
}

function HealthComputationBody({
  response,
  displayedStatus,
  labelFor,
}: {
  response: EntityHealthResponse
  displayedStatus: HealthStatus
  labelFor: (entityId: string) => string
}) {
  const health = response.health
  if (health === null) {
    return <p className="health-computation__status">Not evaluated yet: the radar has no recorded health for this entity.</p>
  }
  const evaluated = formatTimestamp(health.evaluated_at)
  const derivation = health.derivation ?? null
  return (
    <>
      <p className="health-computation__result">
        <StatusMark status={health.status} /> <span>{scoreSentence(health.status, health.score)}</span>
      </p>
      {displayedStatus !== health.status ? (
        <p className="health-computation__note">
          The radar currently shows {displayedStatus} (an incident under review or a newer graph state); below is the latest recorded health evaluation.
        </p>
      ) : null}
      {derivation === null ? (
        <p className="health-computation__status">
          Health computation not recorded for this evaluation (evaluated {evaluated}); it appears after the next radar cycle.
        </p>
      ) : (
        <Derivation derivation={derivation} labelFor={labelFor} />
      )}
      <dl className="health-computation__meta">
        <dt>Evaluated at</dt>
        <dd><time dateTime={health.evaluated_at}>{evaluated}</time></dd>
        <dt>Rule version</dt>
        <dd>{health.rule_version}</dd>
      </dl>
    </>
  )
}

function Derivation({
  derivation,
  labelFor,
}: {
  derivation: HealthDerivation
  labelFor: (entityId: string) => string
}) {
  const aggregation = derivation.dependencies
  return (
    <>
      <p className="health-computation__explanation">{explainHealth(derivation)}</p>
      <p className="health-computation__mapping">Score mapping: {scoreMapping()}</p>
      <div className="health-computation__scroll">
        <table className="metrics health-computation__checks">
          <caption>Checks in evaluation order (decisive rule marked)</caption>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Value</th>
              <th scope="col">Rule</th>
              <th scope="col">Result</th>
            </tr>
          </thead>
          <tbody>
            {derivation.checks.map((check, index) => {
              const condition = conditionText(check)
              return (
                <tr
                  key={`${check.stage}:${check.rule}:${index}`}
                  className={check.decisive ? "health-computation__decisive" : undefined}
                  data-decisive={check.decisive || undefined}
                  data-outcome={check.outcome}
                >
                  <th scope="row">
                    {metricLabel(check)}
                    <small>{humanize(check.stage)}</small>
                  </th>
                  <td>{observedText(check)}</td>
                  <td>
                    {check.rule}
                    {condition ? <small>{condition}{check.leads_to ? ` → ${check.leads_to}` : ""}</small> : null}
                  </td>
                  <td>
                    {resultText(check)}
                    {check.decisive ? <strong className="health-computation__badge">Decisive</strong> : null}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {aggregation ? (
        <div className="health-computation__scroll">
          <table className="metrics health-computation__dependencies">
            <caption>Dependency aggregation</caption>
            <thead>
              <tr>
                <th scope="col">Dependency</th>
                <th scope="col">Status</th>
                <th scope="col">Weight</th>
              </tr>
            </thead>
            <tbody>
              {aggregation.dependencies.map((item) => (
                <tr key={item.entity_id}>
                  <th scope="row">{labelFor(item.entity_id)}</th>
                  <td><StatusMark status={item.status} /></td>
                  <td>{item.weight !== null ? shareText(item.weight) : "full (worst-of)"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="health-computation__totals">
            {aggregation.dependencies.some((item) => item.counted === "weighted")
              ? `Allocation by status: ${(["CRITICAL", "UNKNOWN", "WARNING", "HEALTHY"] as const)
                .map((status) => `${status} ${shareText(aggregation.weighted_totals[status])}`)
                .join(" · ")} vs ${shareText(aggregation.material_allocation)} material`
              : "Counted in full: the worst dependency status applies"}
            {" "}→ {aggregation.status} ({aggregation.reason})
          </p>
        </div>
      ) : null}
      <p className="health-computation__hysteresis">
        <span>Hysteresis:</span> {hysteresisText(derivation)}
      </p>
    </>
  )
}
