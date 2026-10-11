"use client"

import "@xyflow/react/dist/style.css"

import type {
  HealthState,
  HealthStatus,
  Incident,
  IncidentImpact,
  RadarEntity,
  RadarGraphResponse,
  RadarRelationship,
} from "@risk0/contracts"
import { Background, Controls, type Edge, MarkerType, MiniMap, type Node, type NodeMouseHandler, Position, ReactFlow } from "@xyflow/react"
import { memo, type ReactNode, useCallback, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"

import { useFillViewport } from "../hooks/use-fill-viewport"
import { layeredLayout } from "../lib/layered-layout"
import { rankRadarEntities, relevantDependencyContext } from "../lib/radar-focus"
import { RadarEntityDetail } from "./radar-entity-detail"
import { ShowMore, useListLimit } from "./show-more"

const HEALTH_STATUSES: HealthStatus[] = ["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"]

/** Most entities drawn on the canvas; the rest stay reachable through filters. */
export const MAX_CANVAS_NODES = 400
/** Above this many nodes React Flow only mounts the nodes inside the viewport. */
const VIEWPORT_CULLING_THRESHOLD = 150
const LIST_PAGE_SIZE = 60
const NODE_WIDTH = 150
const NODE_HEIGHT = 44

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

/**
 * Entities the radar shows, before user filters:
 * - catalog entities, and vaults that are visible or revealed by an incident
 *   (the "anchors");
 * - non-vault entities only when they are connected to an anchor: a direct
 *   neighbor in either direction, or a dependency reachable from an anchor
 *   (relationships point dependent → dependency, e.g. vault → market → oracle);
 * - any entity on an incident propagation path, plus `pinnedIds`.
 * Dependencies are followed outward only, never into other vaults, so the
 * result stays bounded by the anchors' own topology however large the graph.
 */
export function visibleRadarEntities(
  graph: Pick<RadarGraphResponse, "entities" | "relationships">,
  impacts: IncidentImpact[] = [],
  pinnedIds: Iterable<string> = [],
): RadarEntity[] {
  const revealed = new Set(impacts.filter((impact) => impact.revealed).flatMap((impact) => impact.path))
  const shown = new Set<string>(impacts.flatMap((impact) => impact.path))
  const byId = new Map(graph.entities.map((entity) => [entity.entity_id, entity]))

  const anchors: string[] = []
  for (const entity of graph.entities) {
    const isVault = entity.entity_type === "vault"
    if (entity.catalog || (isVault && (entity.visible || revealed.has(entity.entity_id)))) {
      anchors.push(entity.entity_id)
      shown.add(entity.entity_id)
    } else if (isVault && !revealed.has(entity.entity_id)) {
      // Hidden vaults appear only when an incident reveals them.
      shown.delete(entity.entity_id)
    }
  }

  for (const id of pinnedIds) shown.add(id)

  const dependencies = new Map<string, string[]>()
  const anchorIds = new Set(anchors)
  for (const edge of graph.relationships) {
    if (edge.relationship_type === "similar_to") continue
    const list = dependencies.get(edge.source_entity_id)
    if (list) list.push(edge.target_entity_id)
    else dependencies.set(edge.source_entity_id, [edge.target_entity_id])
    // Direct dependents of an anchor (e.g. a position on a catalog market).
    if (anchorIds.has(edge.target_entity_id) && byId.get(edge.source_entity_id)?.entity_type !== "vault") {
      shown.add(edge.source_entity_id)
    }
  }
  const queue = [...anchors]
  const expanded = new Set(anchors)
  for (let index = 0; index < queue.length; index += 1) {
    for (const target of dependencies.get(queue[index]) ?? []) {
      const entity = byId.get(target)
      if (!entity || entity.entity_type === "vault" || expanded.has(target)) continue
      expanded.add(target)
      shown.add(target)
      queue.push(target)
    }
  }
  return graph.entities.filter((entity) => shown.has(entity.entity_id))
}

const NO_HEALTH_STATES: HealthState[] = []
const NO_IMPACTS: IncidentImpact[] = []
const NO_IDS: ReadonlySet<string> = new Set()

function pairKey(left: string, right: string): string {
  return left < right ? `${left}\u0000${right}` : `${right}\u0000${left}`
}

function relationshipKey(edge: RadarRelationship, index: number): string {
  return `${edge.source_entity_id}:${edge.relationship_type}:${edge.target_entity_id}:${index}`
}

/** Undirected hops that lie on a propagation path (consecutive path entries). */
function pathEdgeKeys(impacts: IncidentImpact[]): Set<string> {
  const keys = new Set<string>()
  for (const impact of impacts) {
    for (let index = 1; index < impact.path.length; index += 1) {
      keys.add(pairKey(impact.path[index - 1], impact.path[index]))
    }
  }
  return keys
}

// Layer for entities with no visible relationship.
const TYPE_DEPTH: Record<RadarEntity["entity_type"], number> = {
  position: 0, vault: 0, market: 1, asset: 2, oracle: 2, irm: 2, protocol: 3, chain: 4,
}

/** Incident path first, then catalog entries, then vaults: what a reader needs first. */
function priority(entity: RadarEntity, incidentPath: ReadonlySet<string>): number {
  if (incidentPath.has(entity.entity_id)) return 0
  if (entity.catalog) return 1
  return entity.entity_type === "vault" ? 2 : 3
}

// Memoized, with the layout memoized on the drawn graph only: selection and
// hover never recompute positions or make React Flow re-measure.
export const RadarGraph = memo(function RadarGraph({
  graph,
  healthStates = NO_HEALTH_STATES,
  selectedIncident,
  impacts = NO_IMPACTS,
  onSelectEntity,
  selectedEntityId = null,
  onEvaluateExposure,
  pinnedEntityIds = NO_IDS,
  expandedEntityIds = NO_IDS,
  loadingNeighboursId = null,
  onShowNeighbours,
  overlayRef,
  detailContainer = null,
}: {
  graph: RadarGraphResponse
  healthStates?: HealthState[]
  selectedIncident?: Incident | null
  impacts?: IncidentImpact[]
  onSelectEntity?: (entityId: string | null) => void
  selectedEntityId?: string | null
  /** Opens the exposure form on the selected market or vault. */
  onEvaluateExposure?: (entityId: string) => void
  /** Entities always shown, e.g. neighborhoods fetched with `focus`. */
  pinnedEntityIds?: ReadonlySet<string>
  /** Entities whose neighbors were already fetched. */
  expandedEntityIds?: ReadonlySet<string>
  loadingNeighboursId?: string | null
  /** Fetch and show an entity's neighbors. */
  onShowNeighbours?: (entityId: string) => void
  /** Receives an element over the canvas's right edge that callers can portal floating panels into. */
  overlayRef?: (element: HTMLDivElement | null) => void
  /** Where the selected entity's details render (e.g. a side drawer); inline below the canvas when null. */
  detailContainer?: HTMLElement | null
}) {
  const [chainFilter, setChainFilter] = useState("all")
  const [typeFilter, setTypeFilter] = useState("all")
  const [healthFilter, setHealthFilter] = useState("all")
  const [searchQuery, setSearchQuery] = useState("")
  const [showOnlyRelevant, setShowOnlyRelevant] = useState(false)
  const [showRelationshipLabels, setShowRelationshipLabels] = useState(false)
  const [expanded, setExpanded] = useState(false)
  // The canvas fills the viewport by default; the expanded mode has its own size.
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const canvasHeight = useFillViewport(canvasRef, { enabled: !expanded })
  const [nodeLimit, showMoreNodes] = useListLimit(LIST_PAGE_SIZE)
  const [relationshipLimit, showMoreRelationships] = useListLimit(LIST_PAGE_SIZE)
  const health = useMemo(() => healthByEntity(graph.entities, healthStates), [graph.entities, healthStates])
  const incidentSourceId = selectedIncident?.source_entity_id ?? null
  const incidentPath = useMemo(() => {
    const path = new Set(impacts.flatMap((impact) => impact.path))
    if (incidentSourceId) path.add(incidentSourceId)
    return path
  }, [impacts, incidentSourceId])
  const incidentEdges = useMemo(() => pathEdgeKeys(impacts), [impacts])
  const policyVisibleEntities = useMemo(
    () => visibleRadarEntities(graph, impacts, incidentSourceId ? [...pinnedEntityIds, incidentSourceId] : pinnedEntityIds),
    [graph, impacts, incidentSourceId, pinnedEntityIds],
  )
  const passesFilters = useCallback((entity: RadarEntity) => {
    const status = health.get(entity.entity_id) ?? "UNKNOWN"
    return (chainFilter === "all" || String(entity.chain_id) === chainFilter)
      && (typeFilter === "all" || entity.entity_type === typeFilter)
      && (healthFilter === "all" || status === healthFilter)
  }, [chainFilter, health, healthFilter, typeFilter])
  // An entity selected from a category list or an analysis must be drawn even
  // when the graph's own filters would hide it. Null unless they do, so plain
  // selection changes leave the drawn set (and the layout) untouched.
  const filteredOutSelectionId = useMemo(() => {
    const selected = selectedEntityId ? policyVisibleEntities.find((entity) => entity.entity_id === selectedEntityId) : undefined
    return selected && !passesFilters(selected) ? selected.entity_id : null
  }, [passesFilters, policyVisibleEntities, selectedEntityId])
  const visibleEntities = useMemo(() => policyVisibleEntities
    .filter((entity) => (
      incidentPath.has(entity.entity_id) || entity.entity_id === filteredOutSelectionId || passesFilters(entity)
    ))
    .map((entity, index) => ({ entity, index }))
    .sort((left, right) => (
      priority(left.entity, incidentPath) - priority(right.entity, incidentPath) || left.index - right.index
    ))
    .map(({ entity }) => entity), [filteredOutSelectionId, incidentPath, passesFilters, policyVisibleEntities])
  const chainIds = useMemo(
    () => [...new Set(policyVisibleEntities.map((entity) => entity.chain_id))].sort((left, right) => left - right),
    [policyVisibleEntities],
  )
  const entityTypes = useMemo(
    () => [...new Set(policyVisibleEntities.map((entity) => entity.entity_type))].sort(),
    [policyVisibleEntities],
  )
  const entityById = useMemo(() => new Map(graph.entities.map((entity) => [entity.entity_id, entity])), [graph.entities])
  const selectedEntity = selectedEntityId ? entityById.get(selectedEntityId) ?? null : null
  const relevantIds = useMemo(
    () => relevantDependencyContext(graph, selectedEntityId, impacts),
    [graph, impacts, selectedEntityId],
  )
  const hasFocus = selectedEntity !== null && relevantIds.size > 0
  const renderedEntities = useMemo(
    () => showOnlyRelevant && hasFocus ? visibleEntities.filter((entity) => relevantIds.has(entity.entity_id)) : visibleEntities,
    [hasFocus, relevantIds, showOnlyRelevant, visibleEntities],
  )
  const visibleIds = useMemo(() => new Set(renderedEntities.map((entity) => entity.entity_id)), [renderedEntities])
  // Search reaches every loaded entity except hidden vaults no incident has
  // revealed; a selected result is pinned by the page, so it is then drawn.
  const searchableEntities = useMemo(() => {
    const policyVisibleIds = new Set(policyVisibleEntities.map((entity) => entity.entity_id))
    return graph.entities.filter((entity) => (
      entity.entity_type !== "vault" || entity.catalog || entity.visible || policyVisibleIds.has(entity.entity_id)
    ))
  }, [graph.entities, policyVisibleEntities])
  const searchResults = useMemo(
    () => (searchQuery.trim() ? rankRadarEntities(searchableEntities, searchQuery).slice(0, 8) : []),
    [searchQuery, searchableEntities],
  )
  const visibleSimilarities = useMemo(
    () => graph.similarities.filter((similarity) => visibleIds.has(similarity.candidate_entity_id)),
    [graph.similarities, visibleIds],
  )
  const visibleRelationships = useMemo(
    () => graph.relationships.filter((edge) => visibleIds.has(edge.source_entity_id) && visibleIds.has(edge.target_entity_id)),
    [graph.relationships, visibleIds],
  )

  // The canvas draws at most MAX_CANVAS_NODES entities, highest priority first.
  const promotedEntityId = renderedEntities.length > MAX_CANVAS_NODES
    && selectedEntityId
    && renderedEntities.slice(MAX_CANVAS_NODES).some((entity) => entity.entity_id === selectedEntityId)
    ? selectedEntityId
    : null
  const canvasEntities = useMemo(() => {
    const entities = renderedEntities.slice(0, MAX_CANVAS_NODES)
    if (!promotedEntityId) return entities
    const promoted = entityById.get(promotedEntityId)
    return promoted ? [...entities.slice(0, -1), promoted] : entities
  }, [entityById, promotedEntityId, renderedEntities])
  const canvasRelationships = useMemo(() => {
    if (canvasEntities.length === renderedEntities.length) return visibleRelationships
    const ids = new Set(canvasEntities.map((entity) => entity.entity_id))
    return visibleRelationships.filter((edge) => ids.has(edge.source_entity_id) && ids.has(edge.target_entity_id))
  }, [canvasEntities, renderedEntities.length, visibleRelationships])
  const positions = useMemo(() => layeredLayout(
    canvasEntities.map((entity) => entity.entity_id),
    canvasRelationships.map((edge) => ({ source: edge.source_entity_id, target: edge.target_entity_id })),
    { rankHint: (id) => TYPE_DEPTH[entityById.get(id)?.entity_type ?? "market"] },
  ), [canvasEntities, canvasRelationships, entityById])

  const statusOf = useCallback((entity: RadarEntity): HealthStatus => (
    selectedIncident?.source_entity_id === entity.entity_id
      ? selectedIncident.severity
      : (health.get(entity.entity_id) ?? "UNKNOWN")
  ), [health, selectedIncident])
  const flowNodes: Node[] = useMemo(() => canvasEntities.map((entity) => {
    const status = statusOf(entity)
    return {
      id: entity.entity_id,
      position: positions.get(entity.entity_id) ?? { x: 0, y: 0 },
      initialWidth: NODE_WIDTH,
      initialHeight: NODE_HEIGHT,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      data: { label: `${entity.label} · ${status}` },
      selected: selectedEntityId === entity.entity_id,
      className: `radar-flow-node radar-flow-node--${status.toLowerCase()}${incidentPath.has(entity.entity_id) ? " radar-flow-node--incident" : ""}${hasFocus ? relevantIds.has(entity.entity_id) ? " radar-flow-node--focused" : " radar-flow-node--faded" : ""}`,
    }
  }), [canvasEntities, hasFocus, incidentPath, positions, relevantIds, selectedEntityId, statusOf])
  const flowEdges: Edge[] = useMemo(() => canvasRelationships.map((edge, index) => {
    const incidentEdge = incidentEdges.has(pairKey(edge.source_entity_id, edge.target_entity_id))
    const focusedEdge = hasFocus && relevantIds.has(edge.source_entity_id) && relevantIds.has(edge.target_entity_id)
    const showLabel = showRelationshipLabels || focusedEdge || (selectedIncident && incidentEdge)
    return {
      id: relationshipKey(edge, index),
      source: edge.source_entity_id,
      target: edge.target_entity_id,
      label: showLabel ? edge.relationship_type.replaceAll("_", " ") : undefined,
      className: `${incidentEdge ? "radar-flow-edge--incident" : ""}${hasFocus ? focusedEdge ? " radar-flow-edge--focused" : " radar-flow-edge--faded" : ""}`.trim(),
      markerEnd: { type: MarkerType.ArrowClosed },
    }
  }), [canvasRelationships, hasFocus, incidentEdges, relevantIds, selectedIncident, showRelationshipLabels])
  const fitViewOptions = useMemo(() => ({ padding: selectedIncident ? 0.25 : 0.15 }), [selectedIncident])
  const handleNodeClick: NodeMouseHandler = useCallback((_, node) => onSelectEntity?.(node.id), [onSelectEntity])
  const clearFocus = useCallback(() => {
    setShowOnlyRelevant(false)
    onSelectEntity?.(null)
  }, [onSelectEntity])
  const handlePaneClick = useCallback(() => clearFocus(), [clearFocus])
  const placeDetail = (detail: ReactNode) => (detailContainer ? createPortal(detail, detailContainer) : detail)
  const selectSearchResult = useCallback((entityId: string) => {
    setSearchQuery("")
    setChainFilter("all")
    setTypeFilter("all")
    setHealthFilter("all")
    onSelectEntity?.(entityId)
  }, [onSelectEntity])

  const header = (
    <div className="radar-graph__header">
      <div><span className="eyebrow">DEPENDENCY TOPOLOGY</span><h2 id="radar-heading" tabIndex={-1}>Morpho radar graph</h2></div>
      <div className="radar-graph__header-actions">
        <ul className="radar-legend" aria-label="Health status legend">
          {HEALTH_STATUSES.map((status) => <li key={status} data-health-status={status}>{status}</li>)}
        </ul>
        <div className="radar-graph__focus-controls">
          <div className="radar-search">
            <label>Search graph<input type="search" value={searchQuery} placeholder="Name, ID, address, or type" onChange={(event) => setSearchQuery(event.target.value)} /></label>
            {searchQuery.trim() ? searchResults.length > 0 ? (
              <ul className="radar-search__results" aria-label="Graph search results">
                {searchResults.map((entity) => (
                  <li key={entity.entity_id}>
                    <button type="button" onClick={() => selectSearchResult(entity.entity_id)}>
                      <strong>{entity.label}</strong><small>{entity.entity_id}</small>
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="radar-search__empty" role="status">No graph entities match.</p> : null}
          </div>
          <button type="button" aria-pressed={showOnlyRelevant} disabled={!hasFocus} onClick={() => setShowOnlyRelevant((value) => !value)}>Show only relevant</button>
          <button type="button" disabled={!hasFocus} onClick={clearFocus}>Clear focus</button>
          <button type="button" aria-pressed={showRelationshipLabels} onClick={() => setShowRelationshipLabels((value) => !value)}>Show relationship labels</button>
        </div>
        {graph.entities.length > 0 ? (
          <button type="button" aria-pressed={expanded} onClick={() => setExpanded((value) => !value)}>
            {expanded ? "Exit expanded graph" : "Expand graph"}
          </button>
        ) : null}
      </div>
    </div>
  )

  if (graph.entities.length === 0) {
    return (
      <section className="panel radar-graph" aria-labelledby="radar-heading">
        {header}
        <div className="radar-graph__empty">
          <strong>Radar has no data yet — discovery runs hourly</strong>
          <span>Vaults, markets, and their dependencies appear here after the first discovery cycle completes.</span>
        </div>
      </section>
    )
  }

  const shownNodes = renderedEntities.slice(0, nodeLimit)
  const shownRelationships = visibleRelationships.slice(0, relationshipLimit)
  return (
    <section className={`panel radar-graph${expanded ? " radar-graph--expanded" : ""}`} aria-labelledby="radar-heading">
      {header}
      <div className="radar-filters" aria-label="Graph filters">
        <label>Chain filter<select value={chainFilter} onChange={(event) => setChainFilter(event.target.value)}><option value="all">All chains</option>{chainIds.map((chainId) => <option key={chainId} value={chainId}>{chainId}</option>)}</select></label>
        <label>Entity type filter<select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">All entity types</option>{entityTypes.map((type) => <option key={type} value={type}>{type}</option>)}</select></label>
        <label>Health filter<select value={healthFilter} onChange={(event) => setHealthFilter(event.target.value)}><option value="all">All health states</option>{HEALTH_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
      </div>
      {canvasEntities.length < renderedEntities.length ? (
        <p className="radar-graph__note">
          The canvas shows the first {canvasEntities.length.toLocaleString("en-US")} of {renderedEntities.length.toLocaleString("en-US")} entities
          (incident path and catalog first). Narrow the filters to see the rest.
        </p>
      ) : null}
      <div
        className="radar-graph__canvas"
        ref={canvasRef}
        style={canvasHeight !== null ? { height: canvasHeight } : undefined}
      >
        <ReactFlow
          key={selectedIncident?.incident_id ?? "radar"}
          nodes={flowNodes}
          edges={flowEdges}
          onNodeClick={handleNodeClick}
          onPaneClick={handlePaneClick}
          nodesDraggable={false}
          nodesConnectable={false}
          onlyRenderVisibleElements={flowNodes.length > VIEWPORT_CULLING_THRESHOLD}
          minZoom={0.05}
          fitView
          fitViewOptions={fitViewOptions}
        >
          <Background />
          {/* Left side: the right edge belongs to the floating panels. */}
          <MiniMap position="bottom-left" pannable zoomable />
          <Controls position="top-left" showInteractive={false} />
        </ReactFlow>
        {overlayRef ? <div className="radar-graph__overlay" ref={overlayRef} /> : null}
      </div>
      {selectedEntity && onSelectEntity ? placeDetail(
        <RadarEntityDetail
          key={selectedEntity.entity_id}
          entity={selectedEntity}
          status={statusOf(selectedEntity)}
          graph={graph}
          entityById={entityById}
          onSelectEntity={onSelectEntity}
          onEvaluateExposure={onEvaluateExposure && (selectedEntity.entity_type === "market" || selectedEntity.entity_type === "vault")
            ? () => onEvaluateExposure(selectedEntity.entity_id)
            : undefined}
          onShowNeighbours={onShowNeighbours ? () => onShowNeighbours(selectedEntity.entity_id) : undefined}
          neighboursStatus={loadingNeighboursId === selectedEntity.entity_id
            ? "loading"
            : expandedEntityIds.has(selectedEntity.entity_id) ? "shown" : "idle"}
          onClose={clearFocus}
        />
      ) : null}
      <details className="radar-graph__accessible">
        <summary>Explore topology as lists ({renderedEntities.length} entities, {visibleRelationships.length} relationships)</summary>
        <ul className="radar-nodes" id="radar-node-list" aria-label="Radar nodes">
          {shownNodes.map((entity) => {
            const status = statusOf(entity)
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
        <ShowMore
          shown={shownNodes.length}
          total={renderedEntities.length}
          pageSize={LIST_PAGE_SIZE}
          noun="nodes"
          controls="radar-node-list"
          onShowMore={showMoreNodes}
        />
        <ul className="radar-relationships" id="radar-relationship-list" aria-label="Radar relationships">
          {shownRelationships.map((edge, index) => {
            const source = entityById.get(edge.source_entity_id)?.label ?? edge.source_entity_id
            const target = entityById.get(edge.target_entity_id)?.label ?? edge.target_entity_id
            const onPath = incidentEdges.has(pairKey(edge.source_entity_id, edge.target_entity_id))
            const focused = hasFocus && relevantIds.has(edge.source_entity_id) && relevantIds.has(edge.target_entity_id)
            const labelVisible = showRelationshipLabels || focused || Boolean(selectedIncident && onPath)
            return <li key={relationshipKey(edge, index)} data-focus={hasFocus ? focused ? "focused" : "faded" : "none"} data-incident-path={onPath ? "true" : "false"} data-label-visible={labelVisible ? "true" : "false"}>{source} — {edge.relationship_type.replaceAll("_", " ")} → {target}</li>
          })}
        </ul>
        <ShowMore
          shown={shownRelationships.length}
          total={visibleRelationships.length}
          pageSize={LIST_PAGE_SIZE}
          noun="relationships"
          controls="radar-relationship-list"
          onShowMore={showMoreRelationships}
        />
        {visibleSimilarities.length > 0 ? <ul className="radar-similarities" aria-label="Similarity explanations">
          {visibleSimilarities.map((similarity) => <li key={`${similarity.catalog_entity_id}:${similarity.candidate_entity_id}`}>
            <strong>{entityById.get(similarity.candidate_entity_id)?.label ?? similarity.candidate_entity_id}: {(Number(similarity.score) * 100).toFixed(0)}%</strong>
            <span>{Object.entries(similarity.components).map(([name, value]) => `${name.replaceAll("_", " ")} ${(Number(value) * 100).toFixed(0)}%`).join(" · ")}</span>
            {similarity.missing_dimensions.length > 0 ? <small>Missing: {similarity.missing_dimensions.map((name) => name.replaceAll("_", " ")).join(", ")}</small> : null}
          </li>)}
        </ul> : null}
      </details>
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
})
