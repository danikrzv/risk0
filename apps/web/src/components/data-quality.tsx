import type { AnalysisResponse, FreshnessStatus, ProbabilityEstimate } from "@risk0/contracts"

import { ageSeconds, elapsedSeconds, formatAge, formatProbabilityRange } from "../lib/format"

const FRESHNESS_LABELS: Record<FreshnessStatus, string> = {
  fresh: "Fresh",
  stale_usable: "Stale",
  expired: "Expired",
}

const MODEL_VERSION_PATTERN = /\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*-v\d+)\b/

export function FreshnessBadge({
  freshness,
  observedAt,
  source,
  now,
  ageSeconds: serverAge,
}: {
  freshness: FreshnessStatus
  observedAt: string
  source: string
  now: number
  /** Age reported by the risk service; preferred over the browser clock, which may be skewed. */
  ageSeconds?: number
}) {
  const age = formatAge(serverAge ?? ageSeconds(observedAt, now))
  return (
    <span className={`badge badge--${freshness}`} title={observedAt}>
      {`${FRESHNESS_LABELS[freshness]} · ${age} old · ${source}`}
    </span>
  )
}

/** The model version reported in a model-estimated explanation, if any. */
export function modelVersion(estimate: ProbabilityEstimate): string | null {
  return estimate.explanation.match(MODEL_VERSION_PATTERN)?.[1] ?? null
}

export function probabilityMethodLabel(estimate: ProbabilityEstimate): string {
  switch (estimate.method) {
    case "empirical_rolling_window":
      return "Empirical"
    case "model_estimated_protocol":
      return `Model-estimated · ${modelVersion(estimate) ?? "model version not reported"}`
    case "unavailable":
      return "Probability unavailable"
  }
}

export function ProbabilityMethodLabel({ estimate }: { estimate: ProbabilityEstimate }) {
  return (
    <span className={`method method--${estimate.method}`}>{probabilityMethodLabel(estimate)}</span>
  )
}

interface SourceRow {
  key: string
  label: string
  freshness: FreshnessStatus
  observedAt: string
  source: string
  ageSeconds?: number
  detail?: string
}

function sourceRows(analysis: AnalysisResponse, elapsed: number): SourceRow[] {
  const nodes = analysis.graph.nodes.map((node) => ({
    key: `node:${node.node_id}`,
    label: node.label,
    freshness: node.freshness,
    observedAt: node.observed_at,
    source: node.provenance,
  }))
  const exit = analysis.exit_estimate
  const quote = {
    key: "exit-quote",
    label: "Exit quote",
    freshness: exit.freshness,
    observedAt: exit.quoted_at,
    source: exit.source,
    ageSeconds: exit.quote_age_seconds + elapsed,
  }
  const probabilities = analysis.scenario_results.map((result) => ({
    key: `probability:${result.scenario_id}`,
    label: `${result.scenario_id} probability`,
    freshness: result.probability.freshness,
    observedAt: result.probability.retrieved_at,
    source: result.probability.source,
    detail: `${probabilityMethodLabel(result.probability)} · ${formatProbabilityRange(result.probability)} · confidence ${result.probability.confidence}`,
  }))
  return [...nodes, quote, ...probabilities]
}

export function DataQualityPanel({
  analysis,
  now,
  receivedAt,
}: {
  analysis: AnalysisResponse
  now: number
  /** When this device received the analysis; server ages are advanced by the time since. */
  receivedAt?: number
}) {
  const rows = sourceRows(analysis, elapsedSeconds(receivedAt, now))
  const degraded = rows.filter((row) => row.freshness !== "fresh")
  const warnings = [...analysis.graph.completeness_warnings, ...analysis.exit_estimate.warnings]

  return (
    <section className="panel" aria-labelledby="data-quality-heading">
      <h2 id="data-quality-heading">Data quality</h2>
      <p className={degraded.length ? "quality-summary quality-summary--degraded" : "quality-summary"}>
        {degraded.length === 0
          ? "All sources fresh"
          : `${degraded.length} stale source${degraded.length === 1 ? "" : "s"}`}
      </p>
      {warnings.length > 0 && (
        <ul className="warnings" aria-label="Data warnings">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      <ul className="sources" aria-label="Sources">
        {rows.map((row) => (
          <li key={row.key}>
            <span className="sources__label">{row.label}</span>
            <FreshnessBadge
              freshness={row.freshness}
              observedAt={row.observedAt}
              source={row.source}
              now={now}
              ageSeconds={row.ageSeconds}
            />
            {row.detail && <span className="sources__detail">{row.detail}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}
