import type { IncidentGraphResponse, RadarGraphResponse } from "@risk0/contracts"

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 15_000
const FILTER_NAMES = ["chain_id", "entity_type", "status", "visible", "incident_id"] as const

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
}

function isEntity(entity: unknown): boolean {
  return (
    isObject(entity) &&
    typeof entity.entity_id === "string" &&
    typeof entity.entity_type === "string" &&
    typeof entity.chain_id === "number" &&
    typeof entity.address_or_key === "string" &&
    typeof entity.label === "string" &&
    typeof entity.source === "string" &&
    typeof entity.catalog === "boolean" &&
    typeof entity.visible === "boolean" &&
    isObject(entity.metadata)
  )
}

function isRelationship(relationship: unknown): boolean {
  return (
    isObject(relationship) &&
    typeof relationship.source_entity_id === "string" &&
    typeof relationship.target_entity_id === "string" &&
    typeof relationship.relationship_type === "string" &&
    (typeof relationship.weight === "string" || relationship.weight === null) &&
    isObject(relationship.metadata)
  )
}

function isSimilarity(similarity: unknown): boolean {
  return (
    isObject(similarity) &&
    typeof similarity.catalog_entity_id === "string" &&
    typeof similarity.candidate_entity_id === "string" &&
    typeof similarity.score === "string" &&
    isObject(similarity.components) &&
    isStringArray(similarity.missing_dimensions) &&
    typeof similarity.model_version === "string" &&
    typeof similarity.stored === "boolean" &&
    typeof similarity.normally_visible === "boolean" &&
    typeof similarity.computed_at === "string"
  )
}

function isIncident(incident: unknown): boolean {
  return (
    isObject(incident) &&
    typeof incident.incident_id === "string" &&
    typeof incident.source_entity_id === "string" &&
    typeof incident.severity === "string" &&
    typeof incident.status === "string" &&
    isObject(incident.trigger_metrics) &&
    typeof incident.rule_version === "string" &&
    typeof incident.first_observed_at === "string" &&
    typeof incident.last_observed_at === "string" &&
    (typeof incident.resolved_at === "string" || incident.resolved_at === null) &&
    isStringArray(incident.reasons)
  )
}

function isImpact(impact: unknown): boolean {
  return (
    isObject(impact) &&
    typeof impact.incident_id === "string" &&
    typeof impact.target_entity_id === "string" &&
    isStringArray(impact.path) &&
    typeof impact.distance === "number" &&
    typeof impact.confidence === "string" &&
    (typeof impact.estimated_affected_amount === "string" || impact.estimated_affected_amount === null) &&
    isStringArray(impact.reasons) &&
    typeof impact.revealed === "boolean" &&
    typeof impact.alternate_path_count === "number"
  )
}

function isRadarGraphResponse(value: unknown): value is RadarGraphResponse {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    Array.isArray(value.entities) &&
    value.entities.every(isEntity) &&
    Array.isArray(value.relationships) &&
    value.relationships.every(isRelationship) &&
    Array.isArray(value.similarities) &&
    value.similarities.every(isSimilarity) &&
    Array.isArray(value.active_incidents) &&
    value.active_incidents.every(isIncident) &&
    typeof value.generated_at === "string"
  )
}

// The page renders the incident, its entities, and impact paths directly, so
// they are checked as strictly as the radar graph itself.
function isIncidentGraphResponse(value: unknown): value is IncidentGraphResponse {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    isIncident(value.incident) &&
    Array.isArray(value.entities) &&
    value.entities.every(isEntity) &&
    Array.isArray(value.relationships) &&
    value.relationships.every(isRelationship) &&
    Array.isArray(value.similarities) &&
    value.similarities.every(isSimilarity) &&
    Array.isArray(value.exposures) &&
    value.exposures.every(isObject) &&
    Array.isArray(value.impacts) &&
    value.impacts.every(isImpact) &&
    typeof value.generated_at === "string"
  )
}

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  const incoming = new URL(request.url).searchParams
  const incidentGraphId = incoming.get("incident_graph_id")
  const filters = new URLSearchParams()
  for (const name of FILTER_NAMES) {
    const value = incoming.get(name)
    if (value !== null) filters.set(name, value)
  }
  const query = filters.size > 0 ? `?${filters.toString()}` : ""
  const upstreamPath = incidentGraphId
    ? `/v1/graph/incidents/${encodeURIComponent(incidentGraphId)}`
    : `/v1/graph${query}`

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}${upstreamPath}`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const body: unknown = await upstream.json().catch(() => null)
    if (incidentGraphId ? !isIncidentGraphResponse(body) : !isRadarGraphResponse(body)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(body)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
