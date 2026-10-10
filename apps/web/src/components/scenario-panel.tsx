"use client"

import type { RiskGraph, ScenarioId, ScenarioResult, ScenarioSelection } from "@risk0/contracts"
import { useId } from "react"

import { compareDecimal, formatProbabilityRange, formatQuantity, isPositiveDecimal, percentToRatio, ratioToPercent } from "../lib/format"
import { ProbabilityMethodLabel } from "./data-quality"

export interface ScenarioDefinition {
  scenario_id: ScenarioId
  name: string
  severity: string
  threshold: string
}

// Mirrors data/scenarios/default.json; a test keeps the two in sync.
export const DEFAULT_SCENARIOS: ScenarioDefinition[] = [
  {
    scenario_id: "stablecoin-depeg",
    name: "Stablecoin depeg",
    severity: "0.10",
    threshold: "EURCV or USDC falls 10% from its reference value",
  },
  {
    scenario_id: "collateral-shock",
    name: "Collateral shock",
    severity: "0.15",
    threshold: "wstETH, WETH, or WBTC falls 15% over 24 hours",
  },
  {
    scenario_id: "oracle-failure",
    name: "Oracle failure",
    severity: "0.05",
    threshold: "Price is stale for 30 minutes or deviates by at least 5%",
  },
  {
    scenario_id: "liquidity-collapse",
    name: "Exit-liquidity collapse",
    severity: "0.80",
    threshold: "Executable Uniswap depth falls 80%",
  },
  {
    scenario_id: "protocol-impairment",
    name: "Protocol impairment",
    severity: "0.10",
    threshold: "A smart-contract or control failure causes at least a 10% loss",
  },
]

export interface ScenarioDraft {
  scenario_id: ScenarioId
  enabled: boolean
  /** Severity as a percentage string, e.g. "15". */
  severity: string
}

export type ScenarioErrors = Partial<Record<ScenarioId | "_form", string>>

const SEVERITY_ERROR = "Enter a severity above 0% and at most 100%"

export function scenarioName(scenarioId: string): string {
  return DEFAULT_SCENARIOS.find((scenario) => scenario.scenario_id === scenarioId)?.name ?? scenarioId
}

export function defaultScenarioDrafts(): ScenarioDraft[] {
  return DEFAULT_SCENARIOS.map((scenario) => ({
    scenario_id: scenario.scenario_id,
    enabled: true,
    severity: ratioToPercent(scenario.severity),
  }))
}

export function toScenarioSelections(drafts: ScenarioDraft[]): {
  selections: ScenarioSelection[]
  errors: ScenarioErrors
} {
  const errors: ScenarioErrors = {}
  const selections: ScenarioSelection[] = []
  for (const draft of drafts) {
    if (!draft.enabled) continue
    if (!isPositiveDecimal(draft.severity) || compareDecimal(draft.severity, "100") === 1) {
      errors[draft.scenario_id] = SEVERITY_ERROR
      continue
    }
    selections.push({ scenario_id: draft.scenario_id, severity: percentToRatio(draft.severity) })
  }
  if (!drafts.some((draft) => draft.enabled)) errors._form = "Select at least one scenario"
  return { selections: Object.keys(errors).length ? [] : selections, errors }
}

function ScenarioRow({
  definition,
  draft,
  error,
  onChange,
}: {
  definition: ScenarioDefinition
  draft: ScenarioDraft
  error?: string
  onChange: (draft: ScenarioDraft) => void
}) {
  const id = useId()
  return (
    <li className={`scenario${draft.enabled ? "" : " scenario--disabled"}`}>
      <label className="scenario__toggle">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => onChange({ ...draft, enabled: event.target.checked })}
        />
        {definition.name}
      </label>
      <span className="scenario__threshold">{definition.threshold}</span>
      <div className="field field--inline">
        <label htmlFor={id}>{`${definition.name} severity (%)`}</label>
        <input
          id={id}
          type="text"
          inputMode="decimal"
          value={draft.severity}
          disabled={!draft.enabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          onChange={(event) => onChange({ ...draft, severity: event.target.value })}
        />
        {error && (
          <span id={`${id}-error`} className="field__error">
            {error}
          </span>
        )}
      </div>
    </li>
  )
}

export function ScenarioPanel({
  drafts,
  onChange,
  errors = {},
}: {
  drafts: ScenarioDraft[]
  onChange: (drafts: ScenarioDraft[]) => void
  errors?: ScenarioErrors
}) {
  return (
    <fieldset className="scenarios">
      <legend>3. Select or edit 30-day scenarios</legend>
      <ul>
        {DEFAULT_SCENARIOS.map((definition) => {
          const draft = drafts.find((item) => item.scenario_id === definition.scenario_id)
          if (!draft) return null
          return (
            <ScenarioRow
              key={definition.scenario_id}
              definition={definition}
              draft={draft}
              error={errors[definition.scenario_id]}
              onChange={(next) =>
                onChange(drafts.map((item) => (item.scenario_id === next.scenario_id ? next : item)))
              }
            />
          )
        })}
      </ul>
      {errors._form && <p className="field__error">{errors._form}</p>}
    </fieldset>
  )
}

export function ScenarioResults({
  results,
  graph,
  numeraire,
}: {
  results: ScenarioResult[]
  graph: RiskGraph
  numeraire: string
}) {
  const labels = new Map(graph.nodes.map((node) => [node.node_id, node.label]))
  return (
    <section className="panel" aria-labelledby="scenario-results-heading">
      <h2 id="scenario-results-heading">Scenario impacts</h2>
      <div className="results">
        {results.map((result) => {
          const headingId = `scenario-${result.scenario_id}`
          return (
            <article key={result.scenario_id} className="result" aria-labelledby={headingId}>
              <h3 id={headingId}>{scenarioName(result.scenario_id)}</h3>
              <dl>
                <dt>Impact</dt>
                <dd>{`${formatQuantity(result.impact)} ${numeraire}`}</dd>
                <dt>30-day probability</dt>
                <dd>{formatProbabilityRange(result.probability)}</dd>
                <dt>Method</dt>
                <dd>
                  <ProbabilityMethodLabel estimate={result.probability} />
                </dd>
                <dt>Confidence</dt>
                <dd>{result.confidence}</dd>
                <dt>Affected path</dt>
                <dd>{result.affected_path.map((nodeId) => labels.get(nodeId) ?? nodeId).join(" → ")}</dd>
              </dl>
              {result.hard_triggers.length > 0 && (
                <ul className="triggers" aria-label="Hard triggers">
                  {result.hard_triggers.map((trigger) => (
                    <li key={trigger}>{trigger}</li>
                  ))}
                </ul>
              )}
              {Object.keys(result.impact_components).length > 0 && (
                <table className="components">
                  <caption>Impact components</caption>
                  <tbody>
                    {Object.entries(result.impact_components).map(([name, value]) => (
                      <tr key={name}>
                        <th scope="row">{name.replace(/_/g, " ")}</th>
                        <td>{`${formatQuantity(value)} ${numeraire}`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p>{result.explanation}</p>
              {result.probability.method === "model_estimated_protocol" && (
                <section className="breakdown" aria-label="Model breakdown">
                  <h4>Model breakdown</h4>
                  <p>{result.probability.explanation}</p>
                </section>
              )}
            </article>
          )
        })}
      </div>
    </section>
  )
}
