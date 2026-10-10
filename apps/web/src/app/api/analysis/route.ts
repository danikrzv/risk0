// Server-side proxy to the risk service analysis endpoint. Upstream bodies,
// URLs, and errors are never forwarded to the browser.

const UPSTREAM_TIMEOUT_MS = 30_000
const MAX_BODY_CHARACTERS = 64_000

export const dynamic = "force-dynamic"

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
    return Response.json(await upstream.json())
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}
