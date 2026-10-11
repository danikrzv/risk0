import type { EntityHealthResponse } from "@risk0/contracts"

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 15_000
const HEALTH_STATUSES = new Set(["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"])
const OUTCOMES = new Set(["breach", "within", "missing", "info"])
const STAGES = new Set(["dependencies", "freshness", "metrics", "hysteresis"])

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isNullableString(value: unknown): boolean {
  return typeof value === "string" || value === null
}

function isStatus(value: unknown): boolean {
  return typeof value === "string" && HEALTH_STATUSES.has(value)
}

function isNullableCount(value: unknown): boolean {
  return value === null || (typeof value === "number" && Number.isInteger(value))
}

function isStringArray(value: unknown): boolean {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
}

function isCheck(check: unknown): boolean {
  return (
    isObject(check) &&
    typeof check.stage === "string" && STAGES.has(check.stage) &&
    typeof check.rule === "string" &&
    isNullableString(check.metric) &&
    isNullableString(check.observed) &&
    isNullableString(check.note) &&
    isNullableString(check.operator) &&
    isNullableString(check.threshold) &&
    typeof check.outcome === "string" && OUTCOMES.has(check.outcome) &&
    (check.leads_to === null || isStatus(check.leads_to)) &&
    typeof check.decisive === "boolean"
  )
}

function isAggregation(aggregation: unknown): boolean {
  if (aggregation === null) return true
  return (
    isObject(aggregation) &&
    Array.isArray(aggregation.dependencies) &&
    aggregation.dependencies.every((item) =>
      isObject(item) &&
      typeof item.entity_id === "string" &&
      isStatus(item.status) &&
      (item.counted === "weighted" || item.counted === "full") &&
      isNullableString(item.weight)) &&
    isObject(aggregation.weighted_totals) &&
    [...HEALTH_STATUSES].every((status) => typeof (aggregation.weighted_totals as Record<string, unknown>)[status] === "string") &&
    typeof aggregation.material_allocation === "string" &&
    isStatus(aggregation.status) &&
    typeof aggregation.reason === "string"
  )
}

function isHysteresis(hysteresis: unknown): boolean {
  return (
    isObject(hysteresis) &&
    (hysteresis.previous_status === null || isStatus(hysteresis.previous_status)) &&
    isStatus(hysteresis.raw_status) &&
    typeof hysteresis.rule === "string" &&
    typeof hysteresis.consecutive_abnormal === "number" &&
    typeof hysteresis.consecutive_normal === "number" &&
    isNullableCount(hysteresis.counter) &&
    isNullableCount(hysteresis.cycles_required) &&
    typeof hysteresis.held === "boolean" &&
    isStatus(hysteresis.final_status)
  )
}

function isDerivation(derivation: unknown): boolean {
  if (derivation === null || derivation === undefined) return true
  return (
    isObject(derivation) &&
    typeof derivation.entity_type === "string" &&
    typeof derivation.freshness === "string" &&
    Array.isArray(derivation.checks) &&
    derivation.checks.every(isCheck) &&
    isAggregation(derivation.dependencies) &&
    isStatus(derivation.raw_status) &&
    isStringArray(derivation.raw_reasons) &&
    isHysteresis(derivation.hysteresis) &&
    isStatus(derivation.final_status) &&
    isNullableString(derivation.score) &&
    isObject(derivation.score_mapping) &&
    typeof derivation.rule_version === "string"
  )
}

function isHealthState(health: unknown): boolean {
  if (health === null) return true
  return (
    isObject(health) &&
    typeof health.entity_id === "string" &&
    isStatus(health.status) &&
    isNullableString(health.score) &&
    isStringArray(health.reasons) &&
    isStringArray(health.missing_fields) &&
    typeof health.evaluated_at === "string" &&
    typeof health.rule_version === "string" &&
    isDerivation(health.derivation)
  )
}

// The panel renders every check, so the derivation is validated as strictly
// as the graph itself.
function isEntityHealthResponse(value: unknown): value is EntityHealthResponse {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    typeof value.entity_id === "string" &&
    isHealthState(value.health) &&
    typeof value.generated_at === "string"
  )
}

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })
  const entityId = new URL(request.url).searchParams.get("entity_id")
  if (!entityId) return Response.json({ error: "Entity ID is required" }, { status: 400 })

  try {
    const upstream = await fetch(
      `${baseUrl.replace(/\/$/, "")}/v1/graph/entities/${encodeURIComponent(entityId)}/health`,
      {
        method: "GET",
        headers: { accept: "application/json" },
        cache: "no-store",
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    )
    if (upstream.status === 404) {
      return Response.json({ error: "Graph entity not found" }, { status: 404 })
    }
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const body: unknown = await upstream.json().catch(() => null)
    if (!isEntityHealthResponse(body)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(body)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
