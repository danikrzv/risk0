// Server-side proxy that marks every notification read. The request body is
// ignored, and only a re-built, validated summary is returned to the browser.

import type { NotificationReadAllResponse } from "../../../../lib/api"
import { upstreamErrorFailure, upstreamStatusFailure } from "../../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 15_000

export const dynamic = "force-dynamic"

function isReadAllResponse(value: unknown): value is NotificationReadAllResponse {
  if (typeof value !== "object" || value === null) return false
  const body = value as Record<string, unknown>
  return (
    body.schema_version === "1.0.0" &&
    typeof body.updated === "number" &&
    Number.isSafeInteger(body.updated) &&
    body.updated >= 0
  )
}

export async function POST(): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/notifications/read-all`, {
      method: "POST",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const result: unknown = await upstream.json().catch(() => null)
    if (!isReadAllResponse(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    const body: NotificationReadAllResponse = { schema_version: "1.0.0", updated: result.updated }
    return Response.json(body)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
