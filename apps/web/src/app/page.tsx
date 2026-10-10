"use client"

import type { AnalysisResponse, HydratedCatalogEntry, PositionInput } from "@risk0/contracts"
import { useEffect, useState } from "react"

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
import { getCatalog, runAnalysis } from "../lib/api"

type CatalogState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: HydratedCatalogEntry[] }

type AnalysisState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "error"; message: string }
  | { status: "done"; result: AnalysisResponse; numeraire: string }

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

  useEffect(() => {
    let active = true
    getCatalog()
      .then((response) => {
        if (!active) return
        setNow(Date.now())
        setCatalog({ status: "ready", entries: response.entries })
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

  function selectEntry(entityId: string) {
    setSelectedId(entityId)
    setAnalysis({ status: "idle" })
    setSelectedNodeId(null)
  }

  async function analyze(position: PositionInput) {
    const { selections, errors } = toScenarioSelections(scenarios)
    setScenarioErrors(errors)
    if (Object.keys(errors).length || selections.length === 0) return

    setAnalysis({ status: "running" })
    setSelectedNodeId(null)
    try {
      const result = await runAnalysis({
        position,
        scenarios: selections as [(typeof selections)[number], ...typeof selections],
      })
      setNow(Date.now())
      setAnalysis({ status: "done", result, numeraire: position.numeraire })
    } catch (error) {
      setAnalysis({ status: "error", message: errorMessage(error) })
    }
  }

  return (
    <main>
      <header className="masthead">
        <h1>Risk0</h1>
        <p>
          Model a hypothetical Ethereum Morpho position, explore its dependencies, and compare the 30-day expected
          loss of staying with the cost of exiting now. Read-only analysis; not investment advice.
        </p>
      </header>

      {catalog.status === "loading" && <p>Loading curated catalog…</p>}
      {catalog.status === "error" && (
        <p role="alert" className="alert">
          {catalog.message}
        </p>
      )}
      {catalog.status === "ready" && (
        <CatalogSelector entries={catalog.entries} selectedId={selectedId} onSelect={selectEntry} now={now} />
      )}

      {entry && (
        <PositionForm
          key={entry.entity_id}
          entry={entry}
          onSubmit={analyze}
          busy={analysis.status === "running"}
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
          <DataQualityPanel analysis={analysis.result} now={now} />
        </div>
      )}
    </main>
  )
}
