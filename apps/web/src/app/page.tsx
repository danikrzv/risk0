"use client"

import type { AnalysisResponse, ExposureCreateRequest, HydratedCatalogEntry, IncidentGraphResponse, IncidentImpact, PositionInput, RadarGraphResponse } from "@risk0/contracts"
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
import { TerminalShell } from "../components/terminal-shell"
import { useRadarEvents } from "../hooks/use-radar-events"
import { createExposure, getCatalog, getExposureImpact, getIncidentGraph, getRadarGraph, runAnalysis } from "../lib/api"

type CatalogState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: HydratedCatalogEntry[]; receivedAt: number }

type AnalysisState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "error"; message: string }
  | { status: "done"; result: AnalysisResponse; numeraire: string; receivedAt: number }

// Events arrive in bursts (one radar cycle emits graph, incident, and
// notification events); refetches inside this window are coalesced.
const RADAR_REFRESH_DELAY_MS = 250

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong"
}

export default function Home() {
  const [catalog, setCatalog] = useState<CatalogState>({ status: "loading" })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [scenarios, setScenarios] = useState(defaultScenarioDrafts)
  const [scenarioErrors, setScenarioErrors] = useState<ScenarioErrors>({})
  const [analysis, setAnalysis] = useState<AnalysisState>({ status: "idle" })
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [radarGraph, setRadarGraph] = useState<RadarGraphResponse | null>(null)
  const [radarError, setRadarError] = useState<string | null>(null)
  const [selectedIncident, setSelectedIncident] = useState<IncidentGraphResponse | null>(null)
  const [selectedRadarEntityId, setSelectedRadarEntityId] = useState<string | null>(null)
  // Read at fetch time (not captured in closures) so a just-recorded exposure is used.
  const exposureId = useRef<string | null>(null)
  const [exposureImpacts, setExposureImpacts] = useState<IncidentImpact[]>([])
  const [exposureStatus, setExposureStatus] = useState<{ kind: "ok" | "error"; message: string } | null>(null)
  const [exposureBusy, setExposureBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  // Identifies the latest analysis request; a response for an older one is dropped.
  const requestToken = useRef(0)

  useEffect(() => {
    let active = true
    getCatalog()
      .then((response) => {
        if (!active) return
        const receivedAt = Date.now()
        setNow(receivedAt)
        setCatalog({ status: "ready", entries: response.entries, receivedAt })
      })
      .catch((error: unknown) => active && setCatalog({ status: "error", message: errorMessage(error) }))
    return () => {
      active = false
    }
  }, [])

  // Latest radar / incident request; a response for an older one is dropped.
  const radarToken = useRef(0)
  const incidentToken = useRef(0)
  const radarTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingIncidentId = useRef<string | null>(null)
  const requestedIncidentId = useRef<string | null>(null)

  const refreshRadar = useCallback(async () => {
    const token = ++radarToken.current
    try {
      const graph = await getRadarGraph()
      if (token !== radarToken.current) return
      setRadarGraph(graph)
      setRadarError(null)
    } catch (error) {
      if (token !== radarToken.current) return
      setRadarError(errorMessage(error))
    }
  }, [])

  useEffect(() => {
    void refreshRadar()
    return () => {
      if (radarTimer.current !== null) clearTimeout(radarTimer.current)
      radarTimer.current = null
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
      setRadarError(errorMessage(error))
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

  // Keep displayed data ages exact while data is on screen.
  useEffect(() => {
    if (catalog.status !== "ready") return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [catalog.status])

  const entry = catalog.status === "ready" ? catalog.entries.find((item) => item.entity_id === selectedId) : undefined
  const catalogEntries = catalog.status === "ready" ? catalog.entries.length : 0
  const supportedEntries =
    catalog.status === "ready" ? catalog.entries.filter((item) => item.analysis_available).length : 0
  // Stable identity so the memoized radar graph skips the 1 s clock re-renders.
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
    if (catalog.status !== "ready" || !radarGraph) return matches
    for (const entity of radarGraph.entities) {
      const match = catalog.entries.find((item) => (
        canAnalyze(item)
        && item.chain_id === entity.chain_id
        && item.address_or_market_id.toLowerCase() === entity.address_or_key.toLowerCase()
      ))
      if (match) matches.set(entity.entity_id, match.entity_id)
    }
    return matches
  }, [catalog, radarGraph])
  const analyzableEntityIds = useMemo(() => new Set(catalogByRadarEntity.keys()), [catalogByRadarEntity])

  const analyzeRadarEntity = useCallback((radarEntityId: string) => {
    const catalogId = catalogByRadarEntity.get(radarEntityId)
    if (!catalogId) return
    selectEntry(catalogId)
    requestAnimationFrame(() => {
      document.getElementById("position-analysis")?.scrollIntoView({ behavior: "smooth", block: "start" })
    })
  }, [catalogByRadarEntity, selectEntry])

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
      const receivedAt = Date.now()
      setNow(receivedAt)
      setAnalysis({ status: "done", result, numeraire: position.numeraire, receivedAt })
    } catch (error) {
      if (token !== requestToken.current) return
      setAnalysis({ status: "error", message: errorMessage(error) })
    }
  }

  return (
    <TerminalShell
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
      <header className="masthead">
        <span className="eyebrow">CURRENT MODULE / CONTINUOUS RISK RADAR</span>
        <h2>Monitor dependencies, propagation, and portfolio impact</h2>
        <p>
          Compare Morpho vaults, markets, assets, oracles, and chains every five minutes. Hidden similar vaults are
          revealed when an incident propagates through a shared dependency.
        </p>
      </header>

      <div className="terminal-actions"><NotificationCenter onIncidentClick={focusIncident} /></div>
      {radarError && catalog.status !== "error" ? <p role="alert" className="alert">{radarError}</p> : null}
      {radarGraph ? (
        <div className="radar-layout">
          <ExposureRail entities={radarGraph.entities} onSubmit={addExposure} busy={exposureBusy} />
          <RadarGraph
            graph={radarGraph}
            selectedIncident={selectedIncident?.incident ?? null}
            impacts={displayedImpacts}
            selectedEntityId={selectedRadarEntityId}
            onSelectEntity={setSelectedRadarEntityId}
            analyzableEntityIds={analyzableEntityIds}
            onAnalyzeEntity={analyzeRadarEntity}
          />
          <IncidentRail
            incidents={radarGraph.active_incidents}
            entities={radarGraph.entities}
            selectedIncidentId={selectedIncident?.incident.incident_id ?? null}
            onSelectIncident={focusIncident}
          />
        </div>
      ) : radarError ? null : <p>Loading radar topology…</p>}
      {exposureStatus?.kind === "ok" ? <p role="status" className="exposure-status">{exposureStatus.message}</p> : null}
      {exposureStatus?.kind === "error" ? <p role="alert" className="alert">{exposureStatus.message}</p> : null}

      <section id="position-analysis" className="position-analysis-module" aria-labelledby="position-analysis-heading">
        <header className="module-heading">
          <span className="eyebrow">30-DAY DECISION SUPPORT</span>
          <h2 id="position-analysis-heading">Position analysis</h2>
        </header>

      {catalog.status === "loading" && <p>Loading curated catalog…</p>}
      {catalog.status === "error" && (
        <p role="alert" className="alert">
          {catalog.message}
        </p>
      )}
      {catalog.status === "ready" && (
        <CatalogSelector
          entries={catalog.entries}
          selectedId={selectedId}
          onSelect={selectEntry}
          now={now}
          receivedAt={catalog.receivedAt}
        />
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
            now={now}
          />
          <ScenarioResults
            results={analysis.result.scenario_results}
            graph={analysis.result.graph}
            numeraire={analysis.numeraire}
          />
          <DataQualityPanel analysis={analysis.result} now={now} receivedAt={analysis.receivedAt} />
        </div>
      )}
      </section>
    </TerminalShell>
  )
}
