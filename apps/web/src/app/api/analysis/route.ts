// Server-side proxy to the risk service analysis endpoint. Upstream bodies,
// URLs, and errors are never forwarded to the browser.

const UPSTREAM_TIMEOUT_MS = 30_000
const MAX_BODY_CHARACTERS = 64_000

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/** Minimal shape check so a misbehaving upstream cannot crash the page. */
function isAnalysisShaped(value: unknown): boolean {
  return (
    isObject(value) &&
    isObject(value.graph) &&
    isObject(value.recommendation) &&
    isObject(value.exit_estimate) &&
    Array.isArray(value.scenario_results)
  )
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

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/analysis`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (upstream.status === 400 || upstream.status === 422) {
      return Response.json(
        { error: "The risk service rejected this position or scenario selection" },
        { status: 422 },
      )
    }
    if (!upstream.ok) return Response.json({ error: "Risk service unavailable" }, { status: 502 })
    const result: unknown = await upstream.json()
    if (!isAnalysisShaped(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result)
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}
