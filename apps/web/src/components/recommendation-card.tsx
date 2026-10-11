import type { AnalysisResponse, RecommendationDecision } from "@risk0/contracts"

import { compareDecimal, formatQuantity } from "../lib/format"

// UNAVAILABLE is a data-quality state; it must never look like a safe result.
const TONES: Record<RecommendationDecision, "safe" | "danger" | "unavailable"> = {
  STAY: "safe",
  EXIT: "danger",
  UNAVAILABLE: "unavailable",
}


export function RecommendationCard({
  analysis,
  numeraire,
  onSelectDependency,
  onViewDependencies,
}: {
  analysis: AnalysisResponse
  numeraire: string
  onSelectDependency: (nodeId: string) => void
  /** Present when the analyzed entry is on the radar: shows its dependencies there. */
  onViewDependencies?: () => void
}) {
  const { recommendation, exit_estimate: exit, graph } = analysis
  const tone = TONES[recommendation.decision]
  const amount = (value: string) => `${formatQuantity(value)} ${numeraire}`
  const dominant = recommendation.dominant_dependency
  const dominantLabel = dominant ? (graph.nodes.find((node) => node.node_id === dominant)?.label ?? dominant) : null
  const hasRange = recommendation.expected_loss_low != null && recommendation.expected_loss_high != null
  const comparisonText =
    recommendation.decision === "UNAVAILABLE"
      ? "The comparison cannot support a decision with the current data."
      : compareDecimal(recommendation.expected_loss, recommendation.exit_cost) === 1
        ? "Expected 30-day loss exceeds the cost of exiting now."
        : "Expected 30-day loss does not exceed the cost of exiting now."
  const triggerOverride = recommendation.decision === "EXIT" && recommendation.triggers.length > 0

  return (
    <section
      className={`panel recommendation recommendation--${tone}`}
      data-tone={tone}
      aria-labelledby="recommendation-heading"
    >
      <h2 id="recommendation-heading" className="visually-hidden">
        Recommendation
      </h2>
      <h3 className="recommendation__decision">{recommendation.decision}</h3>
      {recommendation.decision === "UNAVAILABLE" && (
        <p className="recommendation__notice">
          Critical data is missing or too stale for a defensible decision. This is not a recommendation to stay.
        </p>
      )}
      {recommendation.decision === "EXIT" && !exit.withdrawable && (
        <p className="recommendation__notice">Exit recommended, but full exit is currently unavailable</p>
      )}
      <p>{recommendation.explanation}</p>
      <p className="recommendation__confidence">{`Confidence: ${recommendation.confidence}`}</p>
      {onViewDependencies && (
        <button type="button" className="button button--secondary" onClick={onViewDependencies}>
          View dependencies
        </button>
      )}

      <div role="group" aria-label="Stay versus exit" className="comparison">
        <dl>
          <dt>Expected 30-day loss if staying</dt>
          <dd>{amount(recommendation.expected_loss)}</dd>
          {hasRange && (
            <>
              <dt>Expected-loss range</dt>
              <dd>{`${formatQuantity(recommendation.expected_loss_low!)} – ${amount(recommendation.expected_loss_high!)}`}</dd>
            </>
          )}
          <dt>Cost of exiting now</dt>
          <dd>{amount(recommendation.exit_cost)}</dd>
        </dl>
        <p>{comparisonText}</p>
        {triggerOverride && <p>A hard safety trigger requires exit regardless of the cost comparison.</p>}
      </div>

      <h4>Triggers</h4>
      {recommendation.triggers.length > 0 ? (
        <ul className="triggers" aria-label="Recommendation triggers">
          {recommendation.triggers.map((trigger) => (
            <li key={trigger}>{trigger}</li>
          ))}
        </ul>
      ) : (
        <p>No hard safety trigger fired.</p>
      )}

      {dominant && (
        <p>
          Dominant dependency:{" "}
          <button type="button" className="link-button" onClick={() => onSelectDependency(dominant)}>
            {dominantLabel}
          </button>
        </p>
      )}

      <table className="components" aria-label="Exit cost breakdown">
        <tbody>
          <tr>
            <th scope="row">Slippage</th>
            <td>{amount(exit.slippage)}</td>
          </tr>
          <tr>
            <th scope="row">Fees</th>
            <td>{amount(exit.fees)}</td>
          </tr>
          <tr>
            <th scope="row">Gas</th>
            <td>{amount(exit.gas)}</td>
          </tr>
          <tr>
            <th scope="row">Price impact</th>
            <td>{amount(exit.price_impact)}</td>
          </tr>
          <tr>
            <th scope="row">Total</th>
            <td>{amount(exit.total_cost)}</td>
          </tr>
        </tbody>
      </table>
      <p className="hint">
        {`Route: ${exit.route.length ? exit.route.join(" → ") : "no swap"}${exit.quote_block != null ? ` · quote block ${exit.quote_block}` : ""} · ${exit.quote_age_seconds} s old`}
      </p>
      {exit.warnings.length > 0 && (
        <ul className="warnings" aria-label="Exit warnings">
          {exit.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
