"use client"

import type { AnalysisResponse, ExposureCreateRequest, HydratedCatalogEntry, IncidentGraphResponse, IncidentImpact, PositionInput, RadarGraphResponse } from "@risk0/contracts"
import { useCallback, useEffect, useRef, useState } from "react"

import { CatalogSelector } from "../components/catalog-selector"
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
  const [exposureId, setExposureId] = useState<string | null>(null)
  const [exposureImpacts, setExposureImpacts] = useState<IncidentImpact[]>([])
  const [exposureStatus, setExposureStatus] = useState<string | null>(null)
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

  const refreshRadar = useCallback(async () => {
    try {
      setRadarGraph(await getRadarGraph())
      setRadarError(null)
    } catch (error) {
      setRadarError(errorMessage(error))
    }
  }, [])

  useEffect(() => {
    void refreshRadar()
  }, [refreshRadar])

  useRadarEvents({
    onGraphUpdated: () => void refreshRadar(),
    onIncident: (event) => {
      void refreshRadar()
      if (selectedIncident?.incident.incident_id === event.resource_id) void focusIncident(event.resource_id)
    },
    onReconnect: () => {
      void refreshRadar()
      if (selectedIncident) void focusIncident(selectedIncident.incident.incident_id)
    },
  })

  const focusIncident = useCallback(async (incidentId: string) => {
    try {
      setSelectedIncident(await getIncidentGraph(incidentId))
      if (exposureId) {
        const impact = await getExposureImpact(exposureId)
        setExposureImpacts(impact.impacts.filter((item) => item.incident_id === incidentId))
      } else {
        setExposureImpacts([])
      }
      setRadarError(null)
    } catch (error) {
      setRadarError(errorMessage(error))
    }
  }, [])

  async function addExposure(request: ExposureCreateRequest) {
    setExposureBusy(true)
    setExposureStatus(null)
    try {
      const exposure = await createExposure(request)
      setExposureId(exposure.exposure_id)
      setExposureImpacts([])
      setExposureStatus(`Exposure recorded: ${exposure.amount} ${exposure.numeraire}`)
    } catch (error) {
      setExposureStatus(errorMessage(error))
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

  function selectEntry(entityId: string) {
    requestToken.current += 1
    setSelectedId(entityId)
    setScenarios(defaultScenarioDrafts())
    setScenarioErrors({})
    setAnalysis({ status: "idle" })
    setSelectedNodeId(null)
  }

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
      status={catalog.status === "error" ? "degraded" : "live"}
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
            impacts={exposureImpacts.length > 0 ? exposureImpacts : (selectedIncident?.impacts ?? [])}
            selectedEntityId={selectedRadarEntityId}
            onSelectEntity={setSelectedRadarEntityId}
          />
          <IncidentRail
            incidents={radarGraph.active_incidents}
            entities={radarGraph.entities}
            selectedIncidentId={selectedIncident?.incident.incident_id ?? null}
            onSelectIncident={focusIncident}
          />
        </div>
      ) : radarError ? null : <p>Loading radar topology…</p>}
      {exposureStatus ? <p role="status" className="exposure-status">{exposureStatus}</p> : null}

      <section className="position-analysis-module" aria-labelledby="position-analysis-heading">
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
