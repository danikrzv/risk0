// Plain-language rendering of a HealthDerivation from the risk service. Values
// stay decimal strings: they are only reformatted, never computed with floats.

import type { HealthCheck, HealthDerivation, HealthStatus } from "@risk0/contracts"

import { formatPercent, formatQuantity, humanize } from "./format"

type Operator = NonNullable<HealthCheck["operator"]>

const SYMBOL: Record<Operator, string> = { ">": ">", ">=": "≥", "<": "<", "<=": "≤" }
const NEGATION: Record<Operator, Operator> = { ">": "<=", ">=": "<", "<": ">=", "<=": ">" }

/** Shape markers, so a status never relies on color alone. */
export const HEALTH_MARKER: Record<HealthStatus, string> = {
  HEALTHY: "●",
  WARNING: "▲",
  CRITICAL: "■",
  UNKNOWN: "◆",
}

const SCORE: Record<HealthStatus, string> = {
  HEALTHY: "1",
  WARNING: "0.5",
  CRITICAL: "0",
  UNKNOWN: "none",
}

/** "score 0.5 = WARNING"; UNKNOWN has no score. */
export function scoreSentence(status: HealthStatus, score: string | null): string {
  return score === null ? `no score = ${status}` : `score ${score} = ${status}`
}

export function scoreMapping(): string {
  return (Object.keys(SCORE) as HealthStatus[]).map((status) => `${status} ${SCORE[status]}`).join(" · ")
}

function isShare(check: HealthCheck): boolean {
  return check.metric?.endsWith("_allocation") ?? false
}

function trimPercent(ratio: string): string {
  return formatPercent(ratio).replace(/\.?0+%$/, "%")
}

/** A decimal as the check reports it: shares as percentages, large values grouped. */
export function formatCheckValue(check: HealthCheck, value: string): string {
  if (isShare(check)) return trimPercent(value)
  const magnitude = value.replace(/^-/, "").split(".")[0]
  return magnitude.length > 4 ? formatQuantity(value) : value
}

export function metricLabel(check: HealthCheck): string {
  return check.metric ? humanize(check.metric) : humanize(check.stage)
}

/** The observation shown in the Value column. */
export function observedText(check: HealthCheck): string {
  if (check.observed !== null) {
    const value = formatCheckValue(check, check.observed)
    return check.note ? `${value} (${check.note})` : value
  }
  if (check.note !== null) return humanize(check.note)
  return check.outcome === "missing" ? "missing" : "—"
}

/** "≥ 0.90", the condition under which the rule produces `leads_to`. */
export function conditionText(check: HealthCheck): string | null {
  if (check.operator === null || check.threshold === null) return null
  return `${SYMBOL[check.operator]} ${formatCheckValue(check, check.threshold)}`
}

function thresholdKind(status: HealthStatus | null): string {
  if (status === "CRITICAL") return "critical threshold"
  if (status === "WARNING") return "warning threshold"
  return "data-quality threshold"
}

/** "utilization 0.93 ≥ 0.90" when breached; the negated operator when within. */
function comparison(check: HealthCheck): string | null {
  if (check.operator === null || check.threshold === null || check.observed === null) return null
  const operator = check.outcome === "within" ? NEGATION[check.operator] : check.operator
  return `${metricLabel(check)} ${formatCheckValue(check, check.observed)} ${SYMBOL[operator]} ${formatCheckValue(check, check.threshold)}`
}

export function resultText(check: HealthCheck): string {
  const target = check.leads_to ? ` → ${check.leads_to}` : ""
  if (check.stage === "hysteresis") {
    if (!check.leads_to) return "Info"
    return check.decisive ? `Held${target}` : `Applies${target}`
  }
  if (check.outcome === "breach") return `Breach${target}`
  if (check.outcome === "missing") return check.decisive ? `Missing${target}` : "Missing"
  if (check.outcome === "info") return `Applies${target}`
  return "Within"
}

/** Why the raw status (before hysteresis) is what it is. */
function rawCause(derivation: HealthDerivation): string {
  const decisive = derivation.checks.filter((check) => check.decisive && check.stage !== "hysteresis")
  const [first, ...others] = decisive
  if (!first) {
    return derivation.dependencies
      ? "every check is within its threshold and dependencies are normal"
      : "every check is within its threshold"
  }
  let cause: string
  const compared = comparison(first)
  if (first.stage === "freshness") {
    cause = first.rule.startsWith("HEALTHY")
      ? `the snapshot is ${humanize(first.note ?? "not fresh")}; HEALTHY requires a fresh snapshot`
      : "the snapshot is expired"
  } else if (first.metric === "missing_fields") {
    cause = `required metrics are missing${first.note ? `: ${first.note}` : ""}`
  } else if (first.metric === "measurements") {
    cause = "no measurement is available"
  } else if (first.metric === "status" && first.outcome === "info") {
    cause = `the source supplied an explicit ${first.note ?? ""} status`.replace(/\s+/g, " ")
  } else if (compared) {
    cause = `${compared} (${thresholdKind(first.leads_to)})`
    if (first.stage === "dependencies" && derivation.dependencies) {
      cause = `${compared} (${derivation.dependencies.reason})`
    }
    const critical = derivation.checks.find((check) =>
      check !== first &&
      check.metric === first.metric &&
      check.outcome === "within" &&
      check.leads_to === "CRITICAL" &&
      check.threshold !== null &&
      check.operator !== null,
    )
    if (first.leads_to === "WARNING" && critical?.operator && critical.threshold) {
      const side = critical.operator === ">" || critical.operator === ">=" ? "below" : "above"
      cause += `; ${side} the ${formatCheckValue(critical, critical.threshold)} critical threshold`
    }
  } else {
    cause = first.rule.charAt(0).toLowerCase() + first.rule.slice(1)
  }
  if (others.length > 0) cause += ` (and ${others.length} more ${others.length === 1 ? "breach" : "breaches"})`
  return cause
}

/** One sentence: the final status, the decisive rule, and any hysteresis hold. */
export function explainHealth(derivation: HealthDerivation): string {
  const { hysteresis, final_status: status, raw_status: raw } = derivation
  const cause = rawCause(derivation)
  if (hysteresis.held && hysteresis.rule === "warning_confirmation") {
    return `${status}: the raw ${raw} (${cause}) is pending confirmation, ${hysteresis.counter}/${hysteresis.cycles_required} consecutive cycles.`
  }
  if (hysteresis.held && hysteresis.rule === "recovery_confirmation") {
    return `${status}: recovery to ${raw} is pending, ${hysteresis.counter}/${hysteresis.cycles_required} normal cycles (raw ${raw} because ${cause}).`
  }
  return `${status} because ${cause}.`
}

/** "raw WARNING, pending 1/2 cycles → still HEALTHY". */
export function hysteresisText(derivation: HealthDerivation): string {
  const h = derivation.hysteresis
  const cycles = `${h.counter}/${h.cycles_required} cycles`
  switch (h.rule) {
    case "immediate":
      return `raw ${h.raw_status} applies immediately → ${h.final_status}`
    case "already_abnormal":
      return `raw ${h.raw_status}, previous ${h.previous_status ?? "state"} already abnormal → ${h.final_status} without confirmation`
    case "warning_confirmation":
      return h.held
        ? `raw ${h.raw_status}, pending ${cycles} → still ${h.final_status}`
        : `raw ${h.raw_status}, confirmed ${cycles} → ${h.final_status}`
    case "recovery_confirmation":
      return h.held
        ? `raw ${h.raw_status}, recovery pending ${cycles} → still ${h.final_status}`
        : `raw ${h.raw_status}, recovered after ${cycles} → ${h.final_status}`
    default:
      return `raw ${h.raw_status}, no confirmation needed → ${h.final_status}`
  }
}

export function shareText(value: string): string {
  return trimPercent(value)
}
