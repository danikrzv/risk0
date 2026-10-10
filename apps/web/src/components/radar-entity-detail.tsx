"use client"

import type { HealthStatus, RadarEntity, RadarGraphResponse } from "@risk0/contracts"
import { useMemo } from "react"

import { humanize } from "../lib/format"
import { ShowMore, useListLimit } from "./show-more"

// A shared asset can have thousands of dependents; the list grows on request.
const CONNECTION_PAGE_SIZE = 25

function metricText(value: string | boolean | null): string {
  if (value === null) return "—"
  return String(value)
}

/** Inspector for the radar entity selected on the graph or in the node list. */
export function RadarEntityDetail({
  entity,
  status,
  graph,
  entityById,
  onSelectEntity,
  onAnalyze,
  onShowNeighbours,
  neighboursStatus = "idle",
  onClose,
}: {
  entity: RadarEntity
  status: HealthStatus
  graph: RadarGraphResponse
  /** Lookup over `graph.entities`, shared with the graph so it is not rebuilt per selection. */
  entityById: ReadonlyMap<string, RadarEntity>
  onSelectEntity: (entityId: string) => void
  /** Present only when the entity matches an analyzable catalog entry. */
  onAnalyze?: () => void
  /** Fetch this entity's neighbors from the risk service and add them to the radar. */
  onShowNeighbours?: () => void
  neighboursStatus?: "idle" | "loading" | "shown"
  onClose: () => void
}) {
  const connections = useMemo(() => graph.relationships.flatMap((edge) => {
    if (edge.source_entity_id === entity.entity_id) {
      return [{ direction: "→", neighbor: edge.target_entity_id, type: edge.relationship_type }]
    }
    if (edge.target_entity_id === entity.entity_id) {
      return [{ direction: "←", neighbor: edge.source_entity_id, type: edge.relationship_type }]
    }
    return []
  }), [entity.entity_id, graph.relationships])
  const similarities = useMemo(() => graph.similarities.filter(
    (similarity) => similarity.candidate_entity_id === entity.entity_id || similarity.catalog_entity_id === entity.entity_id,
  ), [entity.entity_id, graph.similarities])
  const [connectionLimit, showMoreConnections] = useListLimit(CONNECTION_PAGE_SIZE)
  const label = (id: string) => entityById.get(id)?.label ?? id
  const metadata = Object.entries(entity.metadata).filter(([name]) => name !== "health_status")

  return (
    <section className="radar-detail" aria-label="Selected radar entity" data-health-status={status}>
      <header className="radar-detail__header">
        <div>
          <small>{entity.entity_type.toUpperCase()} · CHAIN {entity.chain_id}</small>
          <h3>{entity.label}</h3>
        </div>
        <button type="button" className="link-button" onClick={onClose} aria-label="Close entity details">
          Close
        </button>
      </header>
      <dl>
        <dt>Health</dt>
        <dd>{status}</dd>
        <dt>Source</dt>
        <dd>{entity.source}</dd>
        <dt>Address</dt>
        <dd><code>{entity.address_or_key}</code></dd>
        <dt>Catalog</dt>
        <dd>{entity.catalog ? "Curated catalog entry" : "Discovered by radar"}</dd>
      </dl>
      {onAnalyze || onShowNeighbours ? (
        <div className="radar-detail__actions">
          {onAnalyze ? (
            <button type="button" className="button radar-detail__analyze" onClick={onAnalyze}>
              Analyze a position here
            </button>
          ) : null}
          {onShowNeighbours ? (
            <button
              type="button"
              className="button button--secondary"
              onClick={onShowNeighbours}
              disabled={neighboursStatus !== "idle"}
            >
              {neighboursStatus === "loading"
                ? "Loading neighbours…"
                : neighboursStatus === "shown" ? "Neighbours shown" : "Show neighbours"}
            </button>
          ) : null}
        </div>
      ) : null}
      {metadata.length > 0 ? (
        <table className="metrics">
          <caption>Metrics</caption>
          <tbody>
            {metadata.map(([name, value]) => (
              <tr key={name}>
                <th scope="row">{humanize(name)}</th>
                <td>{metricText(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {connections.length > 0 ? (
        <div className="radar-detail__section">
          <h4>Connections</h4>
          <ul id="radar-entity-connections" aria-label="Entity connections">
            {connections.slice(0, connectionLimit).map((connection, index) => (
              <li key={`${connection.neighbor}:${connection.type}:${index}`}>
                <span>{connection.direction} {humanize(connection.type)}</span>{" "}
                <button type="button" className="link-button" onClick={() => onSelectEntity(connection.neighbor)}>
                  {label(connection.neighbor)}
                </button>
              </li>
            ))}
          </ul>
          <ShowMore
            shown={Math.min(connectionLimit, connections.length)}
            total={connections.length}
            pageSize={CONNECTION_PAGE_SIZE}
            noun="connections"
            controls="radar-entity-connections"
            onShowMore={showMoreConnections}
          />
        </div>
      ) : null}
      {similarities.length > 0 ? (
        <div className="radar-detail__section">
          <h4>Similar vaults</h4>
          <ul aria-label="Entity similarities">
            {similarities.map((similarity) => {
              const other = similarity.candidate_entity_id === entity.entity_id
                ? similarity.catalog_entity_id
                : similarity.candidate_entity_id
              return (
                <li key={`${similarity.catalog_entity_id}:${similarity.candidate_entity_id}`}>
                  <button type="button" className="link-button" onClick={() => onSelectEntity(other)}>
                    {label(other)}
                  </button>{" "}
                  <span>{(Number(similarity.score) * 100).toFixed(0)}%</span>
                </li>
              )
            })}
          </ul>
        </div>
      ) : null}
    </section>
  )
}
