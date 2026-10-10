"use client"

import type { Incident, RadarEntity } from "@risk0/contracts"

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
  const active = incidents.filter((incident) => incident.status === "OPEN")
  const labels = new Map(entities.map((entity) => [entity.entity_id, entity.label]))
  return (
    <aside className="panel incident-rail" aria-labelledby="incident-heading">
      <span className="eyebrow">ACTIVE SIGNALS</span>
      <h2 id="incident-heading">Incidents</h2>
      {active.length === 0 ? <p className="incident-rail__empty">No active incidents.</p> : (
        <ul aria-label="Active incidents">
          {active.map((incident) => {
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
    </aside>
  )
}
