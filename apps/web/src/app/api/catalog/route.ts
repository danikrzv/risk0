// Server-side proxy to the risk service catalog. Upstream bodies, URLs, and
// errors are never forwarded to the browser.

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 15_000

export const dynamic = "force-dynamic"

export async function GET(): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/catalog`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const body: unknown = await upstream.json()
    if (!body || typeof body !== "object" || !Array.isArray((body as { entries?: unknown }).entries)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(body)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
