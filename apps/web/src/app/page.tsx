"use client"

import type { AnalysisResponse, HydratedCatalogEntry, PositionInput } from "@risk0/contracts"
import { useEffect, useRef, useState } from "react"

import { CatalogSelector } from "../components/catalog-selector"
import { DataQualityPanel } from "../components/data-quality"
import { PositionForm } from "../components/position-form"
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
import { getCatalog, runAnalysis } from "../lib/api"

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
        <span className="eyebrow">CURRENT MODULE / POSITION ANALYSIS</span>
        <h2>Model exposure and exit risk</h2>
        <p>
          Select a curated Morpho position, propagate explicit 30-day scenarios, and compare expected loss with the
          current cost of exiting. Radar monitoring will connect here after its data pipelines land.
        </p>
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
    </TerminalShell>
  )
}
