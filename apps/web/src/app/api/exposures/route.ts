import type { ExposureCreateRequest, ExposureImpactResponse, ManualExposure } from "@risk0/contracts"

const UPSTREAM_TIMEOUT_MS = 15_000
const MAX_BODY_CHARACTERS = 64_000

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null
}

function isExposureCreateRequest(value: unknown): value is ExposureCreateRequest {
  if (!isObject(value)) return false
  return (
    typeof value.entity_id === "string" &&
    (value.mode === "lender" || value.mode === "borrower") &&
    typeof value.amount === "string" &&
    typeof value.numeraire === "string" &&
    isNullableString(value.collateral_amount) &&
    isNullableString(value.debt_amount) &&
    isNullableString(value.ltv)
  )
}

function isManualExposure(value: unknown): value is ManualExposure {
  return (
    isObject(value) &&
    typeof value.exposure_id === "string" &&
    typeof value.created_at === "string" &&
    isExposureCreateRequest(value)
  )
}

function isExposureImpactResponse(value: unknown): value is ExposureImpactResponse {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    isManualExposure(value.exposure) &&
    Array.isArray(value.impacts) &&
    value.impacts.every(
      (impact) =>
        isObject(impact) &&
        typeof impact.incident_id === "string" &&
        typeof impact.target_entity_id === "string" &&
        Array.isArray(impact.path) &&
        impact.path.every((item) => typeof item === "string") &&
        typeof impact.distance === "number" &&
        typeof impact.confidence === "string" &&
        (typeof impact.estimated_affected_amount === "string" ||
          impact.estimated_affected_amount === null) &&
        Array.isArray(impact.reasons) &&
        impact.reasons.every((reason) => typeof reason === "string") &&
        typeof impact.revealed === "boolean" &&
        typeof impact.alternate_path_count === "number",
    ) &&
    typeof value.generated_at === "string"
  )
}

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })
  const exposureId = new URL(request.url).searchParams.get("exposure_id")
  if (!exposureId) return Response.json({ error: "Exposure ID is required" }, { status: 400 })

  try {
    const upstream = await fetch(
      `${baseUrl.replace(/\/$/, "")}/v1/exposures/${encodeURIComponent(exposureId)}/impact`,
      {
        method: "GET",
        headers: { accept: "application/json" },
        cache: "no-store",
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    )
    if (upstream.status === 404) {
      return Response.json({ error: "Exposure not found" }, { status: 404 })
    }
    if (!upstream.ok) return Response.json({ error: "Risk service unavailable" }, { status: 502 })
    const result: unknown = await upstream.json().catch(() => null)
    if (!isExposureImpactResponse(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result)
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}

export async function POST(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  const text = await request.text()
  if (text.length > MAX_BODY_CHARACTERS) {
    return Response.json({ error: "Request body is too large" }, { status: 413 })
  }

  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return Response.json({ error: "Request body must be valid JSON" }, { status: 400 })
  }
  if (!isExposureCreateRequest(body)) {
    return Response.json({ error: "Exposure is invalid" }, { status: 422 })
  }

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/exposures`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (upstream.status === 400 || upstream.status === 422) {
      return Response.json({ error: "The risk service rejected this exposure" }, { status: 422 })
    }
    if (!upstream.ok) return Response.json({ error: "Risk service unavailable" }, { status: 502 })
    const result: unknown = await upstream.json().catch(() => null)
    if (!isManualExposure(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result, { status: upstream.status })
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}
