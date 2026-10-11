// Server-side proxy to the risk service analysis endpoint. Upstream bodies,
// URLs, and errors are never forwarded to the browser, with one bounded
// exception: the risk service's own validation message for a rejected
// position (its 422 `detail`), which says what is wrong with the input, is
// forwarded with any URL or upstream host redacted and its length capped.

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 30_000
const MAX_BODY_CHARACTERS = 64_000
const MAX_DETAIL_CHARACTERS = 400
const REJECTED_MESSAGE = "The risk service rejected this position or scenario selection"

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/** The risk service's validation `detail` (a string, or Pydantic's list of {loc, msg}), sanitized. */
function validationDetail(body: unknown, baseUrl: string): string | null {
  if (!isObject(body)) return null
  const detail = body.detail
  let text: string | null = null
  if (typeof detail === "string") {
    text = detail
  } else if (Array.isArray(detail)) {
    text = detail
      .map((item) => {
        if (!isObject(item) || typeof item.msg !== "string") return null
        const location = Array.isArray(item.loc)
          ? item.loc.filter((part): part is string => typeof part === "string" && part !== "body").join(".")
          : ""
        return location ? `${location}: ${item.msg}` : item.msg
      })
      .filter((item): item is string => item !== null)
      .join("; ")
  }
  if (!text) return null
  const hosts: string[] = []
  try {
    const upstream = new URL(baseUrl)
    hosts.push(upstream.host, upstream.hostname)
  } catch {
    // An unparseable base URL has no host to redact.
  }
  let clean = text.replace(/https?:\/\/[^\s"')]+/g, "[redacted]")
  for (const host of hosts) {
    if (host) clean = clean.split(host).join("[redacted]")
  }
  // eslint-disable-next-line no-control-regex
  clean = clean.replace(/[\u0000-\u001f]+/g, " ").trim()
  if (!clean) return null
  return clean.length > MAX_DETAIL_CHARACTERS ? `${clean.slice(0, MAX_DETAIL_CHARACTERS)}…` : clean
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
      let detail: string | null = null
      try {
        detail = validationDetail(await upstream.json(), baseUrl)
      } catch {
        detail = null
      }
      return Response.json(
        { error: detail ? `${REJECTED_MESSAGE}: ${detail}` : REJECTED_MESSAGE },
        { status: 422 },
      )
    }
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const result: unknown = await upstream.json()
    if (!isAnalysisShaped(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
