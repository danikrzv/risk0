"use client"

import type { Incident, RadarEntity } from "@risk0/contracts"
import { useMemo } from "react"

import { ShowMore, useListLimit } from "./show-more"

const INCIDENT_PAGE_SIZE = 50

export function IncidentRail({
  incidents,
  entities,
  selectedIncidentId,
  onSelectIncident,
}: {
  incidents: Incident[]
  entities: RadarEntity[]
  selectedIncidentId: string | null
  onSelectIncident: (incidentId: string) => void
}) {
  const active = useMemo(() => incidents.filter((incident) => incident.status === "OPEN"), [incidents])
  const labels = useMemo(() => new Map(entities.map((entity) => [entity.entity_id, entity.label])), [entities])
  const [limit, showMore] = useListLimit(INCIDENT_PAGE_SIZE)
  const shown = active.slice(0, limit)
  return (
    <aside className="panel incident-rail" aria-labelledby="incident-heading">
      <span className="eyebrow">ACTIVE SIGNALS</span>
      <h2 id="incident-heading">Incidents</h2>
      {active.length === 0 ? <p className="incident-rail__empty">No active incidents.</p> : (
        <ul id="active-incident-list" aria-label="Active incidents">
          {shown.map((incident) => {
            const label = labels.get(incident.source_entity_id) ?? incident.source_entity_id
            return (
              <li key={incident.incident_id}>
                <button
                  type="button"
                  aria-label={`${label} ${incident.severity}`}
                  aria-pressed={selectedIncidentId === incident.incident_id}
                  data-severity={incident.severity}
                  onClick={() => onSelectIncident(incident.incident_id)}
                >
                  <span>{incident.severity}</span>
                  <strong>{label}</strong>
                  <small>{incident.reasons.join(" ")}</small>
                </button>
              </li>
            )
          })}
        </ul>
      )}
      <ShowMore
        shown={shown.length}
        total={active.length}
        pageSize={INCIDENT_PAGE_SIZE}
        noun="incidents"
        controls="active-incident-list"
        onShowMore={showMore}
      />
    </aside>
  )
}
