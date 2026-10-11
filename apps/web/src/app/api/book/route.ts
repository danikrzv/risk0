// Server-side proxy to the risk service book endpoint. Upstream bodies, URLs,
// and errors are never forwarded to the browser.

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../lib/upstream-errors"

// A book runs up to 20 pocket analyses upstream (a few at a time), so it gets
// a longer budget than a single analysis.
const UPSTREAM_TIMEOUT_MS = 120_000
const MAX_BODY_CHARACTERS = 64_000

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/** Minimal shape check so a misbehaving upstream cannot crash the page. */
function isBookShaped(value: unknown): boolean {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    Array.isArray(value.pockets) &&
    value.pockets.every((pocket) => isObject(pocket) && typeof pocket.decision === "string") &&
    isObject(value.totals)
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
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/book`, {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (upstream.status === 400 || upstream.status === 422) {
      return Response.json(
        { error: "The risk service rejected this book: check the pockets, weights, and scenarios" },
        { status: 422 },
      )
    }
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const result: unknown = await upstream.json()
    if (!isBookShaped(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
