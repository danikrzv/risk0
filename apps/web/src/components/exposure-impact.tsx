"use client"

import type { ExposureImpactResponse, Incident, RadarEntity } from "@risk0/contracts"
import { useMemo } from "react"

import { formatPercent, formatQuantity } from "../lib/format"

/** `position:<chain>:<exposure id>`; the exposure id may itself contain colons. */
function isExposureNode(entityId: string, exposureId: string): boolean {
  const match = /^position:\d+:(.+)$/.exec(entityId)
  return match?.[1] === exposureId
}

/**
 * What the risk service estimated for the latest manual exposure: one row per
 * active incident that reaches it, with the affected amount (exposure × the
 * allocation weights on the propagation path) or the reason it is unknown.
 */
export function ExposureImpactPanel({
  report,
  entities,
  incidents,
  selectedIncidentId,
  onViewIncident,
}: {
  report: ExposureImpactResponse
  entities: RadarEntity[]
  incidents: Incident[]
  selectedIncidentId: string | null
  onViewIncident: (incidentId: string) => void
}) {
  const { exposure, impacts } = report
  const labels = useMemo(() => new Map(entities.map((entity) => [entity.entity_id, entity.label])), [entities])
  const incidentsById = useMemo(() => new Map(incidents.map((incident) => [incident.incident_id, incident])), [incidents])
  const label = (entityId: string) => (
    isExposureNode(entityId, exposure.exposure_id) ? "Your exposure" : labels.get(entityId) ?? entityId
  )
  const known = impacts.filter((impact) => impact.estimated_affected_amount !== null)
  const unknownCount = impacts.length - known.length
  // Display only: the service returns each amount as an exact decimal string.
  const knownTotal = known.reduce((sum, impact) => sum + Number(impact.estimated_affected_amount), 0)
  const held = labels.get(exposure.entity_id) ?? exposure.entity_id
  const size = exposure.mode === "borrower"
    ? `${formatQuantity(exposure.debt_amount ?? "0")} ${exposure.numeraire} debt`
    : `${formatQuantity(exposure.amount)} ${exposure.numeraire}`

  return (
    <section className="exposure-impact" aria-labelledby="exposure-impact-heading">
      <h3 id="exposure-impact-heading">Exposure impact</h3>
      <p className="exposure-impact__subject">
        {size} {exposure.mode} exposure in <strong>{held}</strong>
      </p>
      {impacts.length === 0 ? (
        <p className="exposure-impact__none">
          No active incident reaches this exposure.
        </p>
      ) : (
        <>
          <p className="exposure-impact__total">
            {known.length > 0
              ? `Estimated affected: ${knownTotal.toLocaleString("en-US", { maximumFractionDigits: 2 })} ${exposure.numeraire}`
              : "Estimated affected amount unavailable"}
            {` across ${impacts.length} incident${impacts.length === 1 ? "" : "s"}`}
            {unknownCount > 0 && known.length > 0 ? ` (${unknownCount} with an unknown amount, not counted as zero)` : ""}
          </p>
          <ul className="exposure-impact__list" aria-label="Incidents reaching this exposure">
            {impacts.map((impact) => {
              const incident = incidentsById.get(impact.incident_id)
              const severity = incident?.severity ?? "UNKNOWN"
              const source = incident ? label(incident.source_entity_id) : impact.incident_id
              return (
                <li key={`${impact.incident_id}:${impact.target_entity_id}`} data-severity={severity}>
                  <div className="exposure-impact__row">
                    <strong>{source}</strong>
                    <span className="exposure-impact__severity">{severity}</span>
                  </div>
                  <p className="exposure-impact__amount">
                    {impact.estimated_affected_amount !== null
                      ? `Affected: ${formatQuantity(impact.estimated_affected_amount)} ${exposure.numeraire}`
                      : "Affected amount unavailable"}
                  </p>
                  <p className="exposure-impact__path">
                    Path: {impact.path.map(label).join(" → ")}
                  </p>
                  <small>
                    Confidence {formatPercent(impact.confidence, 0)} · {impact.distance} hop{impact.distance === 1 ? "" : "s"}
                    {impact.revealed ? " · revealed by the incident" : ""}
                  </small>
                  {impact.reasons.length > 0 ? (
                    <small className="exposure-impact__reasons">{impact.reasons.join("; ")}</small>
                  ) : null}
                  <button
                    type="button"
                    className="link-button"
                    aria-pressed={selectedIncidentId === impact.incident_id}
                    onClick={() => onViewIncident(impact.incident_id)}
                  >
                    View incident {source}
                  </button>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </section>
  )
}
