"use client"

import "@xyflow/react/dist/style.css"

import type { HealthState, HealthStatus, Incident, IncidentImpact, RadarEntity, RadarGraphResponse } from "@risk0/contracts"
import { Background, Controls, type Edge, MarkerType, type Node, Position, ReactFlow } from "@xyflow/react"
import { useState } from "react"

const HEALTH_STATUSES: HealthStatus[] = ["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"]

function healthByEntity(entities: RadarEntity[], healthStates: HealthState[]): Map<string, HealthStatus> {
  const health = new Map(healthStates.map((state) => [state.entity_id, state.status]))
  for (const entity of entities) {
    const status = entity.metadata.health_status
    if (!health.has(entity.entity_id) && typeof status === "string" && HEALTH_STATUSES.includes(status as HealthStatus)) {
      health.set(entity.entity_id, status as HealthStatus)
    }
  }
  return health
}

export function visibleRadarEntities(
  entities: RadarEntity[],
  healthStates: HealthState[] = [],
  impacts: IncidentImpact[] = [],
): RadarEntity[] {
  const revealed = new Set(impacts.filter((impact) => impact.revealed).flatMap((impact) => impact.path))
  return entities.filter((entity) => {
    if (entity.catalog || entity.visible || entity.entity_type !== "vault") return true
    if (revealed.has(entity.entity_id)) return true
    return false
  })
}

const TYPE_DEPTH: Record<RadarEntity["entity_type"], number> = {
  vault: 0, market: 1, asset: 2, oracle: 2, irm: 2, protocol: 3, chain: 4, position: 0,
}

export function RadarGraph({
  graph,
  healthStates = [],
  selectedIncident,
  impacts = [],
  onSelectEntity,
  selectedEntityId = null,
}: {
  graph: RadarGraphResponse
  healthStates?: HealthState[]
  selectedIncident?: Incident | null
  impacts?: IncidentImpact[]
  onSelectEntity?: (entityId: string) => void
  selectedEntityId?: string | null
}) {
  const [chainFilter, setChainFilter] = useState("all")
  const [typeFilter, setTypeFilter] = useState("all")
  const [healthFilter, setHealthFilter] = useState("all")
  const health = healthByEntity(graph.entities, healthStates)
  const incidentPath = new Set(impacts.flatMap((impact) => impact.path))
  if (selectedIncident) incidentPath.add(selectedIncident.source_entity_id)
  const policyVisibleEntities = visibleRadarEntities(graph.entities, healthStates, impacts)
  const visibleEntities = policyVisibleEntities.filter((entity) => {
    if (incidentPath.has(entity.entity_id)) return true
    const status = health.get(entity.entity_id) ?? "UNKNOWN"
    return (chainFilter === "all" || String(entity.chain_id) === chainFilter)
      && (typeFilter === "all" || entity.entity_type === typeFilter)
      && (healthFilter === "all" || status === healthFilter)
  })
  const visibleIds = new Set(visibleEntities.map((entity) => entity.entity_id))
  const chainIds = [...new Set(policyVisibleEntities.map((entity) => entity.chain_id))].sort((left, right) => left - right)
  const entityTypes = [...new Set(policyVisibleEntities.map((entity) => entity.entity_type))].sort()
  const entityById = new Map(graph.entities.map((entity) => [entity.entity_id, entity]))
  const visibleSimilarities = graph.similarities.filter((similarity) => visibleIds.has(similarity.candidate_entity_id))
  const rows = new Map<number, number>()
  const flowNodes: Node[] = visibleEntities.map((entity) => {
    const depth = TYPE_DEPTH[entity.entity_type]
    const row = rows.get(depth) ?? 0
    rows.set(depth, row + 1)
    const status = selectedIncident?.source_entity_id === entity.entity_id
      ? selectedIncident.severity
      : (health.get(entity.entity_id) ?? "UNKNOWN")
    return {
      id: entity.entity_id,
      position: { x: depth * 230, y: row * 100 },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      data: { label: `${entity.label} · ${status}` },
      selected: selectedEntityId === entity.entity_id,
      className: `radar-flow-node radar-flow-node--${status.toLowerCase()}${incidentPath.has(entity.entity_id) ? " radar-flow-node--incident" : ""}`,
    }
  })
  const flowEdges: Edge[] = graph.relationships
    .filter((edge) => visibleIds.has(edge.source_entity_id) && visibleIds.has(edge.target_entity_id))
    .map((edge, index) => ({
      id: `${edge.source_entity_id}:${edge.relationship_type}:${edge.target_entity_id}:${index}`,
      source: edge.source_entity_id,
      target: edge.target_entity_id,
      label: edge.relationship_type.replaceAll("_", " "),
      className: incidentPath.has(edge.source_entity_id) && incidentPath.has(edge.target_entity_id) ? "radar-flow-edge--incident" : "",
      markerEnd: { type: MarkerType.ArrowClosed },
    }))

  return (
    <section className="panel radar-graph" aria-labelledby="radar-heading">
      <div className="radar-graph__header">
        <div><span className="eyebrow">DEPENDENCY TOPOLOGY</span><h2 id="radar-heading">Morpho radar graph</h2></div>
        <ul className="radar-legend" aria-label="Health status legend">
          {HEALTH_STATUSES.map((status) => <li key={status} data-health-status={status}>{status}</li>)}
        </ul>
      </div>
      <div className="radar-filters" aria-label="Graph filters">
        <label>Chain filter<select value={chainFilter} onChange={(event) => setChainFilter(event.target.value)}><option value="all">All chains</option>{chainIds.map((chainId) => <option key={chainId} value={chainId}>{chainId}</option>)}</select></label>
        <label>Entity type filter<select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">All entity types</option>{entityTypes.map((type) => <option key={type} value={type}>{type}</option>)}</select></label>
        <label>Health filter<select value={healthFilter} onChange={(event) => setHealthFilter(event.target.value)}><option value="all">All health states</option>{HEALTH_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
      </div>
      <div className="radar-graph__canvas">
        <ReactFlow
          key={selectedIncident?.incident_id ?? "radar"}
          nodes={flowNodes}
          edges={flowEdges}
          onNodeClick={(_, node) => onSelectEntity?.(node.id)}
          nodesDraggable={false}
          nodesConnectable={false}
          fitView
          fitViewOptions={{ padding: selectedIncident ? 0.25 : 0.15 }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="radar-graph__accessible">
        <ul className="radar-nodes" aria-label="Radar nodes">
          {visibleEntities.map((entity) => {
            const status = selectedIncident?.source_entity_id === entity.entity_id
              ? selectedIncident.severity
              : (health.get(entity.entity_id) ?? "UNKNOWN")
            const onPath = incidentPath.has(entity.entity_id)
            return (
              <li key={entity.entity_id}>
                <button
                  type="button"
                  className={`radar-node radar-node--${status.toLowerCase()}`}
                  aria-label={`${entity.label} ${status}`}
                  data-health-status={status}
                  data-incident-path={onPath ? "true" : "false"}
                  data-revealed={onPath && !entity.visible ? "true" : "false"}
                  aria-pressed={selectedEntityId === entity.entity_id}
                  onClick={() => onSelectEntity?.(entity.entity_id)}
                >
                  <small>{entity.entity_type.toUpperCase()} · CHAIN {entity.chain_id}</small>
                  <strong>{entity.label}</strong>
                  <span>{status}</span>
                </button>
              </li>
            )
          })}
        </ul>
        <ul className="radar-relationships" aria-label="Radar relationships">
          {graph.relationships.filter((edge) => visibleIds.has(edge.source_entity_id) && visibleIds.has(edge.target_entity_id)).map((edge) => {
            const source = graph.entities.find((entity) => entity.entity_id === edge.source_entity_id)?.label ?? edge.source_entity_id
            const target = graph.entities.find((entity) => entity.entity_id === edge.target_entity_id)?.label ?? edge.target_entity_id
            const onPath = incidentPath.has(edge.source_entity_id) && incidentPath.has(edge.target_entity_id)
            return <li key={`${edge.source_entity_id}:${edge.relationship_type}:${edge.target_entity_id}`} data-incident-path={onPath ? "true" : "false"}>{source} — {edge.relationship_type.replaceAll("_", " ")} → {target}</li>
          })}
        </ul>
        {visibleSimilarities.length > 0 ? <ul className="radar-similarities" aria-label="Similarity explanations">
          {visibleSimilarities.map((similarity) => <li key={`${similarity.catalog_entity_id}:${similarity.candidate_entity_id}`}>
            <strong>{entityById.get(similarity.candidate_entity_id)?.label ?? similarity.candidate_entity_id}: {(Number(similarity.score) * 100).toFixed(0)}%</strong>
            <span>{Object.entries(similarity.components).map(([name, value]) => `${name.replaceAll("_", " ")} ${(Number(value) * 100).toFixed(0)}%`).join(" · ")}</span>
            {similarity.missing_dimensions.length > 0 ? <small>Missing: {similarity.missing_dimensions.map((name) => name.replaceAll("_", " ")).join(", ")}</small> : null}
          </li>)}
        </ul> : null}
      </div>
      {impacts.length > 0 ? (
        <div className="radar-impact-summary" aria-label="Incident impact summary">
          <strong>{impacts.length} impacted entities</strong>
          <span>{impacts.some((impact) => impact.estimated_affected_amount !== null)
            ? `Affected amount ${impacts.reduce((sum, impact) => sum + Number(impact.estimated_affected_amount ?? 0), 0).toLocaleString("en-US")}`
            : "Affected amount unavailable"}</span>
        </div>
      ) : null}
    </section>
  )
}
