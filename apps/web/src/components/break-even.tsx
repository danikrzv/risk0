import type { BreakEven, BreakEvenPoint } from "@risk0/contracts"

import { compareDecimal, formatPercent, formatQuantity } from "../lib/format"
import { scenarioName } from "./scenario-panel"

/**
 * A probability with enough digits to stay readable when tiny: 30-day
 * probabilities and break-evens are often well below 0.01%.
 */
export function formatProbability(ratio: string): string {
  if (compareDecimal(ratio, "0") === 0) return "0%"
  const magnitude = ratio.startsWith("-") ? ratio.slice(1) : ratio
  if (compareDecimal(magnitude, "0.01") >= 0) return formatPercent(ratio)
  const digits = compareDecimal(magnitude, "0.0001") >= 0 ? 4 : 6
  // Extra digits only where they carry information: "0.9000%" reads as "0.90%".
  return formatPercent(ratio, digits).replace(/(\.\d\d\d*?)0+%$/, "$1%")
}

export function breakEvenPoint(breakEven: BreakEven | null | undefined): BreakEvenPoint | null {
  if (!breakEven?.dominant_scenario_id) return null
  return breakEven.points.find((point) => point.scenario_id === breakEven.dominant_scenario_id) ?? null
}

/** The flip point (other scenarios at their central estimates), else the scenario-alone break-even. */
export function breakEvenThreshold(point: BreakEvenPoint): string | null {
  return point.decision_flip_probability ?? point.break_even_probability
}

/**
 * One sentence a manager can act on: "EXIT becomes optimal above X%
 * probability of <scenario>; current estimate Y%". Null when the service sent
 * no break-even view (older responses).
 */
export function breakEvenSummary(breakEven: BreakEven | null | undefined): string | null {
  if (!breakEven) return null
  if (!breakEven.probability_dependent) {
    return "A hard safety trigger fired: the decision does not depend on any probability."
  }
  const point = breakEvenPoint(breakEven)
  if (!point) return "No selected scenario has a non-zero impact, so there is no break-even probability."
  const name = scenarioName(point.scenario_id)
  const estimate =
    point.current_probability === null
      ? "current estimate unavailable"
      : `current estimate ${formatProbability(point.current_probability)}`
  const threshold = breakEvenThreshold(point)
  if (threshold === null) {
    return `Break-even unavailable for ${name}: ${point.reasons.join("; ") || "an input is unknown"}.`
  }
  if (compareDecimal(threshold, "1") >= 0) {
    return `No probability of ${name} makes EXIT cheaper than staying; ${estimate}.`
  }
  if (compareDecimal(threshold, "0") <= 0) {
    return `EXIT is optimal at any non-zero probability of ${name}: the other scenarios already reach the cost of exiting; ${estimate}.`
  }
  return `EXIT becomes optimal above ${formatProbability(threshold)} probability of ${name}; ${estimate}.`
}

/**
 * The cost the thresholds use: exit cost plus the yield exiting gives up.
 * Null for older responses without carry, or when the exit cost is unknown.
 */
export function breakEvenCostText(breakEven: BreakEven | null | undefined, numeraire: string): string | null {
  if (!breakEven?.probability_dependent) return null
  const total = breakEven.cost_of_exiting ?? null
  const forgone = breakEven.forgone_yield ?? null
  if (total === null || forgone === null || breakEven.exit_cost === null) return null
  const amount = (value: string) => `${formatQuantity(value)} ${numeraire}`
  return `Break-even against exit cost ${amount(breakEven.exit_cost)} + forgone yield ${amount(forgone)} = ${amount(total)}.`
}

export function BreakEvenLine({
  breakEven,
  numeraire = "",
}: {
  breakEven: BreakEven | null | undefined
  numeraire?: string
}) {
  const summary = breakEvenSummary(breakEven)
  if (!summary) return null
  const cost = breakEvenCostText(breakEven, numeraire)
  return <p className="break-even">{cost ? `${summary} ${cost}` : summary}</p>
}
