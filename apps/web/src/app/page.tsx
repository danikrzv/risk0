"use client"

import type { AnalysisResponse, ExposureCreateRequest, ExposureImpactResponse, HealthStatus, HydratedCatalogEntry, IncidentGraphResponse, IncidentImpact, PositionInput, RadarEntity, RadarGraphResponse } from "@risk0/contracts"
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"

import { BookPanel } from "../components/book-panel"
import { CatalogSelector, canAnalyze } from "../components/catalog-selector"
import { DataQualityPanel } from "../components/data-quality"
import { ExposureImpactPanel } from "../components/exposure-impact"
import { ExposureEvaluation, type ExposureEvaluationState } from "../components/exposure-evaluation"
import { ExposureRail, type ExposureTargetSelection } from "../components/exposure-rail"
import { HealthJustification } from "../components/health-justification"
import { LossModelComparison } from "../components/loss-model-comparison"
import { PanelResizer } from "../components/panel-resizer"
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

// Below this width the floating panels become near-full-width overlays.
const NARROW_VIEWPORT_QUERY = "(max-width: 48rem)"

// The right-hand drawer's width (px): adjustable, remembered per browser.
const PANEL_WIDTH_KEY = "risk0.sidePanelWidth"
const PANEL_WIDTH_DEFAULT = 352
const PANEL_WIDTH_MIN = 288
const PANEL_WIDTH_MAX = 960
// The drawer never covers more than this share of the canvas.
const PANEL_MAX_CANVAS_SHARE = 0.8

type SidePanel = "exposure" | "incidents" | "details"

function readStoredPanelWidth(): number | null {
  try {
    const value = Number(window.localStorage.getItem(PANEL_WIDTH_KEY))
    return Number.isFinite(value) && value >= PANEL_WIDTH_MIN ? value : null
  } catch {
    return null
  }
}

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

/** After a category handoff, move keyboard focus to the destination heading once it is shown. */
function focusHeading(id: string) {
  requestAnimationFrame(() => document.getElementById(id)?.focus())
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
  // Exposure, incidents, and the selected node's details share one drawer on the
  // canvas's right edge, too short for more than one (a long incident list would
  // squeeze the form away), so one tab is open at a time. All stay mounted while
  // hidden so form values and list state survive.
  const [openPanel, setOpenPanel] = useState<SidePanel | null>("exposure")
  const exposureOpen = openPanel === "exposure"
  const incidentsOpen = openPanel === "incidents"
  const detailsOpen = openPanel === "details"
  const [panelWidth, setPanelWidth] = useState(PANEL_WIDTH_DEFAULT)
  const [narrowViewport, setNarrowViewport] = useState(false)
  // Drawer element the graph portals the selected node's details into.
  const [detailSlot, setDetailSlot] = useState<HTMLDivElement | null>(null)
  // Canvas overlay the panels are portalled into (a state setter, so stable for the memoized graph).
  const [panelSlot, setPanelSlot] = useState<HTMLDivElement | null>(null)
  const [selectedRadarEntityId, setSelectedRadarEntityId] = useState<string | null>(null)
  // Neighborhoods fetched with `focus`, keyed by the expanded entity.
  const [focusGraphs, setFocusGraphs] = useState<ReadonlyMap<string, RadarGraphResponse>>(() => new Map())
  const [loadingNeighboursId, setLoadingNeighboursId] = useState<string | null>(null)
  const [focusError, setFocusError] = useState<string | null>(null)
  // Read at fetch time (not captured in closures) so a just-recorded exposure is used.
  const exposureId = useRef<string | null>(null)
  const [exposureImpacts, setExposureImpacts] = useState<IncidentImpact[]>([])
  const [exposureStatus, setExposureStatus] = useState<{ kind: "ok" | "error"; message: string } | null>(null)
  // The latest exposure's estimated impact across every active incident.
  const [exposureReport, setExposureReport] = useState<ExposureImpactResponse | null>(null)
  const [exposureReportError, setExposureReportError] = useState<string | null>(null)
  const exposureReportToken = useRef(0)
  // The target chosen on the graph ("Evaluate exposure"), re-applied by its key.
  const [exposureSelection, setExposureSelection] = useState<ExposureTargetSelection | null>(null)
  // The 30-day stay/exit analysis of the exposure just submitted.
  const [exposureEvaluation, setExposureEvaluation] = useState<ExposureEvaluationState>({ status: "idle" })
  const exposureEvaluationToken = useRef(0)
  const [exposureBusy, setExposureBusy] = useState(false)
  // Identifies the latest analysis request; a response for an older one is dropped.
  const requestToken = useRef(0)

  // Narrow screens start graph-first: a near-full-width panel would hide the graph.
  useEffect(() => {
    if (typeof window === "undefined") return
    const narrow = window.matchMedia?.(NARROW_VIEWPORT_QUERY).matches ?? false
    setNarrowViewport(narrow)
    if (narrow) setOpenPanel(null)
    const stored = readStoredPanelWidth()
    if (stored !== null) setPanelWidth(stored)
  }, [])

  function togglePanel(panel: SidePanel) {
    setOpenPanel((current) => (current === panel ? null : panel))
  }

  // Selecting a node shows its details in the drawer; clearing it closes them.
  // Setters only, so the memoized graph keeps a stable callback.
  const selectRadarEntity = useCallback((entityId: string | null) => {
    setSelectedRadarEntityId(entityId)
    setOpenPanel((current) => (entityId !== null ? "details" : current === "details" ? null : current))
  }, [])

  // A canvas width of 0 means not laid out (hidden view): fall back to the absolute bound.
  const canvasWidth = panelSlot?.parentElement?.clientWidth || null
  const panelMaximum = canvasWidth === null
    ? PANEL_WIDTH_MAX
    : Math.max(PANEL_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, Math.floor(canvasWidth * PANEL_MAX_CANVAS_SHARE)))
  const shownPanelWidth = Math.min(panelWidth, panelMaximum)

  function commitPanelWidth(width: number) {
    try {
      window.localStorage.setItem(PANEL_WIDTH_KEY, String(width))
    } catch {
      // Storage unavailable (private window): the width still applies for this visit.
    }
  }

  function placeFloatingPanels(panels: ReactNode) {
    return panelSlot ? createPortal(panels, panelSlot) : panels
  }

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

  const loadExposureReport = useCallback(async () => {
    const id = exposureId.current
    if (!id) return
    const token = ++exposureReportToken.current
    try {
      const report = await getExposureImpact(id)
      if (token !== exposureReportToken.current) return
      setExposureReport(report)
      setExposureReportError(null)
    } catch (error) {
      if (token !== exposureReportToken.current) return
      setExposureReportError(`Exposure impact could not be loaded: ${errorMessage(error)}`)
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
      // Incidents opening or resolving change what reaches the exposure.
      void loadExposureReport()
      // Skip if the user has since selected a different incident.
      if (pending !== null && pending === requestedIncidentId.current) void focusIncident(pending)
    }, RADAR_REFRESH_DELAY_MS)
  }, [focusIncident, loadExposureReport, refreshRadar])

  useRadarEvents({
    onGraphUpdated: () => scheduleRadarRefresh(),
    onIncident: (event) => {
      scheduleRadarRefresh(requestedIncidentId.current === event.resource_id ? event.resource_id : null)
    },
    onReconnect: () => scheduleRadarRefresh(requestedIncidentId.current),
  })

  /** Records the exposure for incident propagation and, in parallel, runs its stay/exit analysis. */
  async function evaluateExposure(request: ExposureCreateRequest) {
    void analyzeExposure(request)
    setExposureBusy(true)
    setExposureStatus(null)
    try {
      const exposure = await createExposure(request)
      exposureId.current = exposure.exposure_id
      setExposureImpacts([])
      setExposureReport(null)
      void loadExposureReport()
      // Show the new exposure's impact on the incident already on screen.
      if (requestedIncidentId.current) void focusIncident(requestedIncidentId.current)
      setExposureStatus({ kind: "ok", message: `Exposure recorded: ${exposure.amount} ${exposure.numeraire}` })
    } catch (error) {
      setExposureStatus({ kind: "error", message: errorMessage(error) })
    } finally {
      setExposureBusy(false)
    }
  }

  /** The 30-day analysis of an exposure, with the default scenarios, when its target is a catalog entry. */
  async function analyzeExposure(request: ExposureCreateRequest) {
    const token = ++exposureEvaluationToken.current
    const label = radarLabels.get(request.entity_id) ?? request.entity_id
    const catalogId = catalogByRadarEntity.get(request.entity_id)
    const entry = catalogId && catalog.status === "ready"
      ? catalog.entries.find((item) => item.entity_id === catalogId)
      : undefined
    if (!entry) {
      setExposureEvaluation({
        status: "unavailable",
        label,
        reason: `${label} is not in the curated catalog, so the 30-day stay/exit analysis cannot run; the exposure is still tracked against active incidents.`,
      })
      return
    }
    if (!entry.supported_modes.includes(request.mode)) {
      setExposureEvaluation({
        status: "unavailable",
        label,
        reason: `${entry.name} supports ${entry.supported_modes.join(" and ")} positions only, not a ${request.mode} exposure.`,
      })
      return
    }
    const position: PositionInput = request.mode === "borrower"
      ? {
          mode: "borrower",
          entity_id: entry.entity_id,
          amount: request.amount,
          numeraire: request.numeraire,
          collateral_amount: request.collateral_amount ?? "0",
          debt_amount: request.debt_amount ?? request.amount,
          ltv: request.ltv ?? "0",
        }
      : { mode: "lender", entity_id: entry.entity_id, amount: request.amount, numeraire: request.numeraire }
    const { selections } = toScenarioSelections(defaultScenarioDrafts())
    setExposureEvaluation({ status: "running", label })
    try {
      const result = await runAnalysis({
        position,
        scenarios: selections as [(typeof selections)[number], ...typeof selections],
      })
      if (token !== exposureEvaluationToken.current) return
      setExposureEvaluation({
        status: "done",
        label,
        mode: request.mode,
        catalogId: entry.entity_id,
        result,
        numeraire: request.numeraire,
        receivedAt: Date.now(),
      })
    } catch (error) {
      if (token !== exposureEvaluationToken.current) return
      setExposureEvaluation({ status: "error", label, message: errorMessage(error) })
    }
  }

  /** Hands the exposure's analysis to Position Analysis, where the full result is shown. */
  function viewExposureAnalysis() {
    if (exposureEvaluation.status !== "done") return
    const { catalogId, result, numeraire, receivedAt } = exposureEvaluation
    if (catalogId !== selectedId) selectEntry(catalogId)
    setAnalysis({ status: "done", result, numeraire, receivedAt })
    setActiveCategory("position-analysis")
    focusHeading("position-analysis-heading")
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
  const radarLabelFor = useCallback((entityId: string) => radarLabels.get(entityId) ?? entityId, [radarLabels])

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
  /** Asset symbol per radar target the analysis can price, so an exposure's amount is in the right unit. */
  const exposureNumeraires = useMemo(() => {
    const symbols = new Map<string, string>()
    if (catalog.status !== "ready") return symbols
    const byId = new Map(catalog.entries.map((item) => [item.entity_id, item]))
    for (const [radarId, catalogId] of catalogByRadarEntity) {
      const symbol = byId.get(catalogId)?.metrics?.asset_symbol
      if (symbol) symbols.set(radarId, symbol)
    }
    return symbols
  }, [catalog, catalogByRadarEntity])

  // Setters only, so the memoized radar graph keeps a stable callback.
  const evaluateExposureFor = useCallback((entityId: string) => {
    setExposureSelection((current) => ({ entityId, key: (current?.key ?? 0) + 1 }))
    setOpenPanel("exposure")
    requestAnimationFrame(() => document.getElementById("exposure-target")?.focus())
  }, [])

  const focusRadarEntity = useCallback((entityId: string) => {
    selectRadarEntity(entityId)
    setActiveCategory("graph")
    focusHeading("radar-heading")
  }, [selectRadarEntity])

  // The analyzed entry's radar entity, when the radar has it.
  const analysisRadarEntityId = entry
    ? radarEntitiesByAddress.get(`${entry.chain_id}:${entry.address_or_market_id.toLowerCase()}`)?.entity_id ?? null
    : null
  const viewAnalysisDependencies = useMemo(
    () => (analysisRadarEntityId ? () => focusRadarEntity(analysisRadarEntityId) : undefined),
    [analysisRadarEntityId, focusRadarEntity],
  )

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
            <div className="radar-stage">
              <RadarGraph
                graph={displayedGraph}
                selectedIncident={selectedIncident?.incident ?? null}
                impacts={displayedImpacts}
                selectedEntityId={selectedRadarEntityId}
                onSelectEntity={selectRadarEntity}
                onEvaluateExposure={evaluateExposureFor}
                pinnedEntityIds={displayedPinnedEntityIds}
                expandedEntityIds={expandedEntityIds}
                loadingNeighboursId={loadingNeighboursId}
                onShowNeighbours={showNeighbours}
                overlayRef={setPanelSlot}
                detailContainer={detailSlot}
              />
              {/* Over the canvas when it is drawn; in page flow otherwise (empty radar). */}
              {placeFloatingPanels(<div
                className="floating-panels"
                style={narrowViewport ? undefined : { width: shownPanelWidth }}
              >
                {openPanel !== null && !narrowViewport ? (
                  <PanelResizer
                    width={shownPanelWidth}
                    minimum={PANEL_WIDTH_MIN}
                    maximum={panelMaximum}
                    controls={`${openPanel === "incidents" ? "incident-drawer" : openPanel === "details" ? "entity-detail" : "exposure"}-panel`}
                    onResize={setPanelWidth}
                    onCommit={commitPanelWidth}
                  />
                ) : null}
                <div className="floating-panels__launchers">
                  <button
                    type="button"
                    className="floating-panels__launcher"
                    aria-expanded={exposureOpen}
                    aria-controls="exposure-panel"
                    onClick={() => togglePanel("exposure")}
                  >
                    {exposureOpen ? "Hide exposure" : "Exposure"}
                  </button>
                  <button
                    type="button"
                    className="floating-panels__launcher"
                    aria-expanded={incidentsOpen}
                    aria-controls="incident-drawer-panel"
                    onClick={() => togglePanel("incidents")}
                  >
                    {incidentsOpen ? "Hide incidents" : `Incidents (${radarGraph.active_incidents.length})`}
                  </button>
                  <button
                    type="button"
                    className="floating-panels__launcher"
                    aria-expanded={detailsOpen}
                    aria-controls="entity-detail-panel"
                    onClick={() => togglePanel("details")}
                  >
                    {detailsOpen ? "Hide details" : "Details"}
                  </button>
                </div>
                <div id="exposure-panel" className="floating-panel" hidden={!exposureOpen}>
                  <ExposureRail
                    entities={radarGraph.entities}
                    onSubmit={evaluateExposure}
                    busy={exposureBusy}
                    selection={exposureSelection}
                    numeraires={exposureNumeraires}
                  />
                  <ExposureEvaluation state={exposureEvaluation} onViewFullAnalysis={viewExposureAnalysis} />
                  {exposureStatus?.kind === "ok" ? <p role="status" className="exposure-status">{exposureStatus.message}</p> : null}
                  {exposureStatus?.kind === "error" ? <p role="alert" className="alert">{exposureStatus.message}</p> : null}
                  {exposureReportError ? <p role="alert" className="alert">{exposureReportError}</p> : null}
                  {exposureReport ? (
                    <ExposureImpactPanel
                      report={exposureReport}
                      entities={displayedGraph.entities}
                      incidents={radarGraph.active_incidents}
                      selectedIncidentId={selectedIncident?.incident.incident_id ?? null}
                      onViewIncident={focusIncident}
                    />
                  ) : null}
                </div>
                <aside id="incident-drawer-panel" className="floating-panel" aria-label="Incident drawer" hidden={!incidentsOpen}>
                  <IncidentRail
                    incidents={radarGraph.active_incidents}
                    entities={displayedGraph.entities}
                    selectedIncidentId={selectedIncident?.incident.incident_id ?? null}
                    onSelectIncident={focusIncident}
                  />
                </aside>
                <aside id="entity-detail-panel" className="floating-panel" aria-label="Node details" hidden={!detailsOpen}>
                  <div ref={setDetailSlot} />
                  {selectedRadarEntityId === null ? (
                    <p className="floating-panel__empty">Select a node on the graph to see its details and how its health was computed.</p>
                  ) : null}
                </aside>
              </div>)}
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
          {!radarGraph ? (
            radarError
              ? <LoadErrorMessage error={radarError} />
              : <p role="status" className="category-view__empty">Loading radar topology…</p>
          ) : marketEntities.length === 0 ? <p className="category-view__empty">No markets are currently available.</p> : (
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
          {!radarGraph ? (
            radarError
              ? <LoadErrorMessage error={radarError} />
              : <p role="status" className="category-view__empty">Loading radar topology…</p>
          ) : oracleEntities.length === 0 ? <p className="category-view__empty">No oracles are currently available.</p> : (
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
          {!radarGraph ? (
            radarError
              ? <LoadErrorMessage error={radarError} />
              : <p role="status" className="category-view__empty">Loading radar topology…</p>
          ) : activeIncidents.length === 0 ? <p className="category-view__empty">No active incidents.</p> : (
            <ul className="category-list" aria-label="Active incident categories">
              {activeIncidents.map((incident) => {
                const label = radarLabels.get(incident.source_entity_id) ?? incident.source_entity_id
                return (
                  <li key={incident.incident_id}>
                    <button
                      type="button"
                      data-health-status={incident.severity}
                      onClick={() => {
                        focusRadarEntity(incident.source_entity_id)
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
          <h2 id="position-analysis-heading" tabIndex={-1}>Position analysis</h2>
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
                  <HealthJustification entity={radarEntity} status={status} labelFor={radarLabelFor} />
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
            onViewDependencies={viewAnalysisDependencies}
          />
          <LossModelComparison analysis={analysis.result} numeraire={analysis.numeraire} />
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

      {catalog.status === "ready" && <BookPanel entries={catalog.entries} />}
      </section>
    </TerminalShell>
  )
}
