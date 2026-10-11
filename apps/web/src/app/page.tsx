"use client"

import type { AnalysisResponse, ExposureCreateRequest, HealthStatus, HydratedCatalogEntry, IncidentGraphResponse, IncidentImpact, PositionInput, RadarEntity, RadarGraphResponse } from "@risk0/contracts"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { CatalogSelector, canAnalyze } from "../components/catalog-selector"
import { DataQualityPanel } from "../components/data-quality"
import { ExposureRail } from "../components/exposure-rail"
import { IncidentRail } from "../components/incident-rail"
import { NotificationCenter } from "../components/notification-center"
import { PositionForm } from "../components/position-form"
import { RadarGraph } from "../components/radar-graph"
import { RecommendationCard } from "../components/recommendation-card"
import { RiskGraph } from "../components/risk-graph"
import {
  ScenarioPanel,
  ScenarioResults,
  defaultScenarioDrafts,
  type ScenarioErrors,
  toScenarioSelections,
} from "../components/scenario-panel"
import { SystemTicker } from "../components/system-ticker"
import { TerminalShell, type WorkspaceCategory } from "../components/terminal-shell"
import { useRadarEvents } from "../hooks/use-radar-events"
import { mergeRadarGraphs, type RadarTopology } from "../lib/radar-graph-merge"
import { CatalogSkeleton, RadarSkeleton } from "../components/skeletons"
import {
  createExposure,
  getCatalog,
  getExposureImpact,
  getIncidentGraph,
  getRadarGraph,
  isRetryableError,
  retryDelayMs,
  runAnalysis,
} from "../lib/api"

type CatalogState =
  | { status: "loading" }
  | { status: "error"; message: string; retrying: boolean }
  | { status: "ready"; entries: HydratedCatalogEntry[]; receivedAt: number }

/** A load failure; `retrying` when it is retried automatically (busy or unreachable service). */
interface LoadError {
  message: string
  retrying: boolean
}

type AnalysisState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "error"; message: string }
  | { status: "done"; result: AnalysisResponse; numeraire: string; receivedAt: number }

// Events arrive in bursts (one radar cycle emits graph, incident, and
// notification events); refetches inside this window are coalesced.
const RADAR_REFRESH_DELAY_MS = 250

const HEALTH_MARKERS: Record<HealthStatus, string> = {
  HEALTHY: "✓",
  WARNING: "!",
  CRITICAL: "×",
  UNKNOWN: "?",
}

function entityHealthStatus(entity: RadarEntity | undefined): HealthStatus {
  switch (entity?.metadata.health_status) {
    case "HEALTHY":
    case "WARNING":
    case "CRITICAL":
    case "UNKNOWN":
      return entity.metadata.health_status
    default:
      return "UNKNOWN"
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong"
}

function loadError(error: unknown): LoadError {
  return { message: errorMessage(error), retrying: isRetryableError(error) }
}

function LoadErrorMessage({ error }: { error: LoadError }) {
  // A retried failure is progress information, not an alert.
  return error.retrying ? (
    <p role="status" className="alert alert--retrying">
      {error.message}
      {/retrying/i.test(error.message) ? "" : " — retrying automatically"}
    </p>
  ) : (
    <p role="alert" className="alert">{error.message}</p>
  )
}

export default function Home() {
  const [activeCategory, setActiveCategory] = useState<WorkspaceCategory>("graph")
  const [catalog, setCatalog] = useState<CatalogState>({ status: "loading" })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [scenarios, setScenarios] = useState(defaultScenarioDrafts)
  const [scenarioErrors, setScenarioErrors] = useState<ScenarioErrors>({})
  const [analysis, setAnalysis] = useState<AnalysisState>({ status: "idle" })
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [radarGraph, setRadarGraph] = useState<RadarGraphResponse | null>(null)
  const [radarError, setRadarError] = useState<LoadError | null>(null)
  const [selectedIncident, setSelectedIncident] = useState<IncidentGraphResponse | null>(null)
  const [incidentsOpen, setIncidentsOpen] = useState(true)
  const [selectedRadarEntityId, setSelectedRadarEntityId] = useState<string | null>(null)
  // Neighborhoods fetched with `focus`, keyed by the expanded entity.
  const [focusGraphs, setFocusGraphs] = useState<ReadonlyMap<string, RadarGraphResponse>>(() => new Map())
  const [loadingNeighboursId, setLoadingNeighboursId] = useState<string | null>(null)
  const [focusError, setFocusError] = useState<string | null>(null)
  // Read at fetch time (not captured in closures) so a just-recorded exposure is used.
  const exposureId = useRef<string | null>(null)
  const [exposureImpacts, setExposureImpacts] = useState<IncidentImpact[]>([])
  const [exposureStatus, setExposureStatus] = useState<{ kind: "ok" | "error"; message: string } | null>(null)
  const [exposureBusy, setExposureBusy] = useState(false)
  // Identifies the latest analysis request; a response for an older one is dropped.
  const requestToken = useRef(0)

  // Retried with backoff while the risk service is busy or starting.
  useEffect(() => {
    let active = true
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    const load = (attempt: number) => {
      getCatalog()
        .then((response) => {
          if (!active) return
          setCatalog({ status: "ready", entries: response.entries, receivedAt: Date.now() })
        })
        .catch((error: unknown) => {
          if (!active) return
          const failure = loadError(error)
          setCatalog({ status: "error", ...failure })
          if (failure.retrying) retryTimer = setTimeout(() => load(attempt + 1), retryDelayMs(attempt))
        })
    }
    load(0)
    return () => {
      active = false
      if (retryTimer !== null) clearTimeout(retryTimer)
    }
  }, [])

  // Latest radar / incident request; a response for an older one is dropped.
  const radarToken = useRef(0)
  const incidentToken = useRef(0)
  const radarTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const radarRetryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const radarRetryAttempt = useRef(0)
  const pendingIncidentId = useRef<string | null>(null)
  const requestedIncidentId = useRef<string | null>(null)

  const refreshRadar = useCallback(async () => {
    const token = ++radarToken.current
    if (radarRetryTimer.current !== null) clearTimeout(radarRetryTimer.current)
    radarRetryTimer.current = null
    try {
      const graph = await getRadarGraph()
      if (token !== radarToken.current) return
      radarRetryAttempt.current = 0
      setRadarGraph(graph)
      setRadarError(null)
    } catch (error) {
      if (token !== radarToken.current) return
      const failure = loadError(error)
      setRadarError(failure)
      // Busy or starting service: retry with backoff instead of leaving a dead error.
      if (failure.retrying) {
        const delay = retryDelayMs(radarRetryAttempt.current)
        radarRetryAttempt.current += 1
        radarRetryTimer.current = setTimeout(() => {
          radarRetryTimer.current = null
          void refreshRadar()
        }, delay)
      }
    }
  }, [])

  useEffect(() => {
    void refreshRadar()
    return () => {
      // Invalidate in-flight requests so no retry is scheduled after unmount.
      radarToken.current += 1
      for (const timer of [radarTimer, radarRetryTimer]) {
        if (timer.current !== null) clearTimeout(timer.current)
        timer.current = null
      }
    }
  }, [refreshRadar])

  const focusIncident = useCallback(async (incidentId: string) => {
    const token = ++incidentToken.current
    requestedIncidentId.current = incidentId
    try {
      const incidentGraph = await getIncidentGraph(incidentId)
      if (token !== incidentToken.current) return
      setSelectedIncident(incidentGraph)
      if (exposureId.current) {
        const impact = await getExposureImpact(exposureId.current)
        if (token !== incidentToken.current) return
        setExposureImpacts(impact.impacts.filter((item) => item.incident_id === incidentId))
      } else {
        setExposureImpacts([])
      }
      setRadarError(null)
    } catch (error) {
      if (token !== incidentToken.current) return
      setRadarError({ message: errorMessage(error), retrying: false })
    }
  }, [])

  /** Coalesce event-driven refetches: one graph (and selected incident) fetch per burst. */
  const scheduleRadarRefresh = useCallback((incidentId: string | null = null) => {
    if (incidentId !== null) pendingIncidentId.current = incidentId
    if (radarTimer.current !== null) return
    radarTimer.current = setTimeout(() => {
      radarTimer.current = null
      const pending = pendingIncidentId.current
      pendingIncidentId.current = null
      void refreshRadar()
      // Skip if the user has since selected a different incident.
      if (pending !== null && pending === requestedIncidentId.current) void focusIncident(pending)
    }, RADAR_REFRESH_DELAY_MS)
  }, [focusIncident, refreshRadar])

  useRadarEvents({
    onGraphUpdated: () => scheduleRadarRefresh(),
    onIncident: (event) => {
      scheduleRadarRefresh(requestedIncidentId.current === event.resource_id ? event.resource_id : null)
    },
    onReconnect: () => scheduleRadarRefresh(requestedIncidentId.current),
  })

  async function addExposure(request: ExposureCreateRequest) {
    setExposureBusy(true)
    setExposureStatus(null)
    try {
      const exposure = await createExposure(request)
      exposureId.current = exposure.exposure_id
      setExposureImpacts([])
      // Show the new exposure's impact on the incident already on screen.
      if (requestedIncidentId.current) void focusIncident(requestedIncidentId.current)
      setExposureStatus({ kind: "ok", message: `Exposure recorded: ${exposure.amount} ${exposure.numeraire}` })
    } catch (error) {
      setExposureStatus({ kind: "error", message: errorMessage(error) })
    } finally {
      setExposureBusy(false)
    }
  }

  const entry = catalog.status === "ready" ? catalog.entries.find((item) => item.entity_id === selectedId) : undefined
  const catalogEntries = catalog.status === "ready" ? catalog.entries.length : 0
  const supportedEntries =
    catalog.status === "ready" ? catalog.entries.filter((item) => item.analysis_available).length : 0
  // The pruned radar carries only a few paths per incident: the selected
  // incident's graph and any fetched neighborhoods are merged on top, and the
  // merged object keeps its identity until one of them changes.
  const displayedGraph = useMemo(() => {
    if (!radarGraph) return null
    const overlays: RadarTopology[] = [...focusGraphs.values()]
    if (selectedIncident) overlays.unshift(selectedIncident)
    return mergeRadarGraphs(radarGraph, overlays)
  }, [focusGraphs, radarGraph, selectedIncident])
  const pinnedEntityIds = useMemo(
    () => new Set([...focusGraphs.values()].flatMap((graph) => graph.entities.map((entity) => entity.entity_id))),
    [focusGraphs],
  )
  const displayedPinnedEntityIds = useMemo(() => {
    if (!selectedRadarEntityId) return pinnedEntityIds
    return new Set([...pinnedEntityIds, selectedRadarEntityId])
  }, [pinnedEntityIds, selectedRadarEntityId])
  const expandedEntityIds = useMemo(() => new Set(focusGraphs.keys()), [focusGraphs])
  const marketEntities = useMemo(
    () => radarGraph?.entities.filter((entity) => entity.entity_type === "market") ?? [],
    [radarGraph],
  )
  const oracleEntities = useMemo(
    () => radarGraph?.entities.filter((entity) => entity.entity_type === "oracle") ?? [],
    [radarGraph],
  )
  const activeIncidents = useMemo(
    () => radarGraph?.active_incidents.filter((incident) => incident.status === "OPEN") ?? [],
    [radarGraph],
  )
  const radarEntitiesByAddress = useMemo(() => {
    const entities = new Map<string, RadarEntity>()
    for (const entity of radarGraph?.entities ?? []) {
      entities.set(`${entity.chain_id}:${entity.address_or_key.toLowerCase()}`, entity)
    }
    return entities
  }, [radarGraph])
  const radarLabels = useMemo(
    () => new Map((radarGraph?.entities ?? []).map((entity) => [entity.entity_id, entity.label])),
    [radarGraph],
  )

  const showNeighbours = useCallback(async (entityId: string) => {
    setLoadingNeighboursId(entityId)
    setFocusError(null)
    try {
      const neighborhood = await getRadarGraph({ focus: entityId })
      setFocusGraphs((current) => new Map(current).set(entityId, neighborhood))
    } catch (error) {
      setFocusError(`Neighbours could not be loaded: ${errorMessage(error)}`)
    } finally {
      setLoadingNeighboursId((current) => (current === entityId ? null : current))
    }
  }, [])

  // Stable identity so the memoized radar graph skips unrelated page re-renders.
  const displayedImpacts = useMemo(() => (selectedIncident?.impacts ?? []).map((impact) => (
    exposureImpacts.find((item) => item.target_entity_id === impact.target_entity_id) ?? impact
  )), [exposureImpacts, selectedIncident])

  // Stable (refs and setters only) so the memoized radar graph can hold it.
  const selectEntry = useCallback((entityId: string) => {
    requestToken.current += 1
    setSelectedId(entityId)
    setScenarios(defaultScenarioDrafts())
    setScenarioErrors({})
    setAnalysis({ status: "idle" })
    setSelectedNodeId(null)
  }, [])

  /** Radar entity id → analyzable catalog entry id, matched on chain and address. */
  const catalogByRadarEntity = useMemo(() => {
    const matches = new Map<string, string>()
    if (catalog.status !== "ready" || !displayedGraph) return matches
    const byAddress = new Map<string, string>()
    for (const item of catalog.entries) {
      const key = `${item.chain_id}:${item.address_or_market_id.toLowerCase()}`
      if (canAnalyze(item) && !byAddress.has(key)) byAddress.set(key, item.entity_id)
    }
    for (const entity of displayedGraph.entities) {
      const match = byAddress.get(`${entity.chain_id}:${entity.address_or_key.toLowerCase()}`)
      if (match) matches.set(entity.entity_id, match)
    }
    return matches
  }, [catalog, displayedGraph])
  const analyzableEntityIds = useMemo(() => new Set(catalogByRadarEntity.keys()), [catalogByRadarEntity])

  const analyzeRadarEntity = useCallback((radarEntityId: string) => {
    const catalogId = catalogByRadarEntity.get(radarEntityId)
    if (!catalogId) return
    selectEntry(catalogId)
    setActiveCategory("position-analysis")
    requestAnimationFrame(() => {
      document.getElementById("position-analysis")?.scrollIntoView({ behavior: "smooth", block: "start" })
    })
  }, [catalogByRadarEntity, selectEntry])

  const focusRadarEntity = useCallback((entityId: string) => {
    setSelectedRadarEntityId(entityId)
    setActiveCategory("graph")
  }, [])

  function validateScenarios(): boolean {
    const { errors } = toScenarioSelections(scenarios)
    setScenarioErrors(errors)
    return Object.keys(errors).length === 0
  }

  async function analyze(position: PositionInput) {
    const { selections, errors } = toScenarioSelections(scenarios)
    setScenarioErrors(errors)
    if (Object.keys(errors).length || selections.length === 0) return

    const token = ++requestToken.current
    setAnalysis({ status: "running" })
    setSelectedNodeId(null)
    try {
      const result = await runAnalysis({
        position,
        scenarios: selections as [(typeof selections)[number], ...typeof selections],
      })
      if (token !== requestToken.current) return
      setAnalysis({ status: "done", result, numeraire: position.numeraire, receivedAt: Date.now() })
    } catch (error) {
      if (token !== requestToken.current) return
      setAnalysis({ status: "error", message: errorMessage(error) })
    }
  }

  return (
    <TerminalShell
      activeCategory={activeCategory}
      onCategoryChange={setActiveCategory}
      status={
        catalog.status === "error" || radarError
          ? "degraded"
          : catalog.status === "loading" || !radarGraph
            ? "connecting"
            : "live"
      }
      ticker={
        <SystemTicker
          catalogStatus={catalog.status}
          catalogEntries={catalogEntries}
          supportedEntries={supportedEntries}
          selectedEntry={entry?.name ?? null}
          analysisStatus={analysis.status}
        />
      }
    >
      <div className="workspace-view workspace-view--graph" hidden={activeCategory !== "graph"}>
        <div className="terminal-actions"><NotificationCenter onIncidentClick={focusIncident} /></div>
        {radarError ? <LoadErrorMessage error={radarError} /> : null}
        {focusError ? <p role="alert" className="alert">{focusError}</p> : null}
        {displayedGraph && radarGraph ? (
          <div className="radar-command-surface">
            <div className="exposure-dock">
              <ExposureRail entities={radarGraph.entities} onSubmit={addExposure} busy={exposureBusy} />
              {exposureStatus?.kind === "ok" ? <p role="status" className="exposure-status">{exposureStatus.message}</p> : null}
              {exposureStatus?.kind === "error" ? <p role="alert" className="alert">{exposureStatus.message}</p> : null}
            </div>
            <div className="radar-stage">
              <RadarGraph
                graph={displayedGraph}
                selectedIncident={selectedIncident?.incident ?? null}
                impacts={displayedImpacts}
                selectedEntityId={selectedRadarEntityId}
                onSelectEntity={setSelectedRadarEntityId}
                analyzableEntityIds={analyzableEntityIds}
                onAnalyzeEntity={analyzeRadarEntity}
                pinnedEntityIds={displayedPinnedEntityIds}
                expandedEntityIds={expandedEntityIds}
                loadingNeighboursId={loadingNeighboursId}
                onShowNeighbours={showNeighbours}
              />
              <aside className={`incident-drawer${incidentsOpen ? " incident-drawer--open" : ""}`} aria-label="Incident drawer">
                <button
                  type="button"
                  className="incident-drawer__toggle"
                  aria-expanded={incidentsOpen}
                  aria-controls="incident-drawer-panel"
                  onClick={() => setIncidentsOpen((open) => !open)}
                >
                  {incidentsOpen ? "Hide incidents" : `Incidents (${radarGraph.active_incidents.length})`}
                </button>
                {incidentsOpen ? (
                  <div id="incident-drawer-panel" className="incident-drawer__panel">
                    <IncidentRail
                      incidents={radarGraph.active_incidents}
                      entities={displayedGraph.entities}
                      selectedIncidentId={selectedIncident?.incident.incident_id ?? null}
                      onSelectIncident={focusIncident}
                    />
                  </div>
                ) : null}
              </aside>
            </div>
          </div>
        ) : radarError && !radarError.retrying ? null : <RadarSkeleton />}
      </div>

      {activeCategory === "markets" ? (
        <section className="category-view" aria-labelledby="markets-heading">
          <header className="module-heading">
            <span className="eyebrow">RADAR ENTITIES</span>
            <h2 id="markets-heading">Markets</h2>
          </header>
          {marketEntities.length === 0 ? <p className="category-view__empty">No markets are currently available.</p> : (
            <ul className="category-list" aria-label="Market health">
              {marketEntities.map((entity) => {
                const status = entityHealthStatus(entity)
                const incidentCount = activeIncidents.filter((incident) => incident.source_entity_id === entity.entity_id).length
                return (
                  <li key={entity.entity_id}>
                    <button type="button" data-health-status={status} onClick={() => focusRadarEntity(entity.entity_id)}>
                      <strong>{entity.label}</strong>
                      <span className="category-list__status"><span aria-hidden="true">{HEALTH_MARKERS[status]}</span> {status}</span>
                      <code>{entity.entity_id}</code>
                      <small>Chain {entity.chain_id} · {incidentCount} active incident{incidentCount === 1 ? "" : "s"}</small>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      ) : null}

      {activeCategory === "oracles" ? (
        <section className="category-view" aria-labelledby="oracles-heading">
          <header className="module-heading">
            <span className="eyebrow">RADAR ENTITIES</span>
            <h2 id="oracles-heading">Oracles</h2>
          </header>
          {oracleEntities.length === 0 ? <p className="category-view__empty">No oracles are currently available.</p> : (
            <ul className="category-list" aria-label="Oracle health">
              {oracleEntities.map((entity) => {
                const status = entityHealthStatus(entity)
                const affectedMarkets = radarGraph?.relationships.filter(
                  (relationship) => relationship.target_entity_id === entity.entity_id && relationship.relationship_type === "uses_oracle",
                ).length ?? 0
                const incidentCount = activeIncidents.filter((incident) => incident.source_entity_id === entity.entity_id).length
                return (
                  <li key={entity.entity_id}>
                    <button type="button" data-health-status={status} onClick={() => focusRadarEntity(entity.entity_id)}>
                      <strong>{entity.label}</strong>
                      <span className="category-list__status"><span aria-hidden="true">{HEALTH_MARKERS[status]}</span> {status}</span>
                      <code>{entity.entity_id}</code>
                      <small>Chain {entity.chain_id} · {affectedMarkets} affected market{affectedMarkets === 1 ? "" : "s"} · {incidentCount} active incident{incidentCount === 1 ? "" : "s"}</small>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      ) : null}

      {activeCategory === "incidents" ? (
        <section className="category-view" aria-labelledby="incidents-heading">
          <header className="module-heading">
            <span className="eyebrow">ACTIVE SIGNALS</span>
            <h2 id="incidents-heading">Incidents</h2>
          </header>
          {activeIncidents.length === 0 ? <p className="category-view__empty">No active incidents.</p> : (
            <ul className="category-list" aria-label="Active incident categories">
              {activeIncidents.map((incident) => {
                const label = radarLabels.get(incident.source_entity_id) ?? incident.source_entity_id
                return (
                  <li key={incident.incident_id}>
                    <button
                      type="button"
                      data-health-status={incident.severity}
                      onClick={() => {
                        setSelectedRadarEntityId(incident.source_entity_id)
                        setActiveCategory("graph")
                        void focusIncident(incident.incident_id)
                      }}
                    >
                      <strong>{label}</strong>
                      <span className="category-list__status"><span aria-hidden="true">{HEALTH_MARKERS[incident.severity]}</span> {incident.severity}</span>
                      <code>{incident.incident_id}</code>
                      <small>{incident.reasons.join(" ")}</small>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      ) : null}

      <section id="position-analysis" className="position-analysis-module" aria-labelledby="position-analysis-heading" hidden={activeCategory !== "position-analysis"}>
        <header className="module-heading">
          <span className="eyebrow">30-DAY DECISION SUPPORT</span>
          <h2 id="position-analysis-heading">Position analysis</h2>
        </header>

      {catalog.status === "error" && <LoadErrorMessage error={catalog} />}
      {catalog.status === "loading" || (catalog.status === "error" && catalog.retrying) ? <CatalogSkeleton /> : null}
      {catalog.status === "ready" && (
        <>
          <ul className="category-list category-list--catalog" aria-label="Position analysis health">
            {catalog.entries.map((catalogEntry) => {
              const radarEntity = radarEntitiesByAddress.get(`${catalogEntry.chain_id}:${catalogEntry.address_or_market_id.toLowerCase()}`)
              const status = entityHealthStatus(radarEntity)
              return (
                <li key={catalogEntry.entity_id}>
                  <button
                    type="button"
                    data-health-status={status}
                    aria-pressed={selectedId === catalogEntry.entity_id}
                    disabled={!canAnalyze(catalogEntry)}
                    onClick={() => selectEntry(catalogEntry.entity_id)}
                  >
                    <strong>{catalogEntry.name}</strong>
                    <span className="category-list__status"><span aria-hidden="true">{HEALTH_MARKERS[status]}</span> {status}</span>
                    <code>{catalogEntry.address_or_market_id}</code>
                    <small>Chain {catalogEntry.chain_id}</small>
                  </button>
                </li>
              )
            })}
          </ul>
          <CatalogSelector
            entries={catalog.entries}
            selectedId={selectedId}
            onSelect={selectEntry}
            receivedAt={catalog.receivedAt}
          />
        </>
      )}

      {entry && (
        <PositionForm
          key={entry.entity_id}
          entry={entry}
          onSubmit={analyze}
          busy={analysis.status === "running"}
          beforeSubmit={validateScenarios}
        >
          <ScenarioPanel drafts={scenarios} onChange={setScenarios} errors={scenarioErrors} />
        </PositionForm>
      )}

      {analysis.status === "error" && (
        <p role="alert" className="alert">
          {analysis.message}
        </p>
      )}

      {analysis.status === "done" && (
        <div className="analysis">
          <RecommendationCard
            analysis={analysis.result}
            numeraire={analysis.numeraire}
            onSelectDependency={setSelectedNodeId}
          />
          <RiskGraph
            graph={analysis.result.graph}
            selectedNodeId={selectedNodeId}
            onSelectNode={setSelectedNodeId}
          />
          <ScenarioResults
            results={analysis.result.scenario_results}
            graph={analysis.result.graph}
            numeraire={analysis.numeraire}
          />
          <DataQualityPanel analysis={analysis.result} receivedAt={analysis.receivedAt} />
        </div>
      )}
      </section>
    </TerminalShell>
  )
}
