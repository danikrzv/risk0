import type { RadarGraphResponse } from "@risk0/contracts"

const UPSTREAM_TIMEOUT_MS = 15_000
const FILTER_NAMES = ["chain_id", "entity_type", "status", "visible", "incident_id"] as const

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isRadarGraphResponse(value: unknown): value is RadarGraphResponse {
  if (!isObject(value)) return false
  return (
    value.schema_version === "1.0.0" &&
    Array.isArray(value.entities) &&
    value.entities.every(
      (entity) =>
        isObject(entity) &&
        typeof entity.entity_id === "string" &&
        typeof entity.entity_type === "string" &&
        typeof entity.chain_id === "number" &&
        typeof entity.address_or_key === "string" &&
        typeof entity.label === "string" &&
        typeof entity.source === "string" &&
        typeof entity.catalog === "boolean" &&
        typeof entity.visible === "boolean" &&
        isObject(entity.metadata),
    ) &&
    Array.isArray(value.relationships) &&
    value.relationships.every(
      (relationship) =>
        isObject(relationship) &&
        typeof relationship.source_entity_id === "string" &&
        typeof relationship.target_entity_id === "string" &&
        typeof relationship.relationship_type === "string" &&
        (typeof relationship.weight === "string" || relationship.weight === null) &&
        isObject(relationship.metadata),
    ) &&
    Array.isArray(value.similarities) &&
    value.similarities.every(
      (similarity) =>
        isObject(similarity) &&
        typeof similarity.catalog_entity_id === "string" &&
        typeof similarity.candidate_entity_id === "string" &&
        typeof similarity.score === "string" &&
        isObject(similarity.components) &&
        Array.isArray(similarity.missing_dimensions) &&
        similarity.missing_dimensions.every((dimension) => typeof dimension === "string") &&
        typeof similarity.model_version === "string" &&
        typeof similarity.stored === "boolean" &&
        typeof similarity.normally_visible === "boolean" &&
        typeof similarity.computed_at === "string",
    ) &&
    Array.isArray(value.active_incidents) &&
    value.active_incidents.every(
      (incident) =>
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
        Array.isArray(incident.reasons) &&
        incident.reasons.every((reason) => typeof reason === "string"),
    ) &&
    typeof value.generated_at === "string"
  )
}

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  const incoming = new URL(request.url).searchParams
  const filters = new URLSearchParams()
  for (const name of FILTER_NAMES) {
    const value = incoming.get(name)
    if (value !== null) filters.set(name, value)
  }
  const query = filters.size > 0 ? `?${filters.toString()}` : ""

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/graph${query}`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (!upstream.ok) return Response.json({ error: "Risk service unavailable" }, { status: 502 })
    const body: unknown = await upstream.json().catch(() => null)
    if (!isRadarGraphResponse(body)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(body)
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}
