import type { Notification, NotificationResponse } from "@risk0/contracts"

import { upstreamErrorFailure, upstreamStatusFailure } from "../../../lib/upstream-errors"

const UPSTREAM_TIMEOUT_MS = 15_000
const MAX_BODY_CHARACTERS = 4_096

export const dynamic = "force-dynamic"

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isNotification(value: unknown): value is Notification {
  return (
    isObject(value) &&
    typeof value.notification_id === "string" &&
    typeof value.incident_id === "string" &&
    typeof value.severity === "string" &&
    typeof value.title === "string" &&
    typeof value.message === "string" &&
    typeof value.created_at === "string" &&
    (typeof value.read_at === "string" || value.read_at === null)
  )
}

function isNotificationResponse(value: unknown): value is NotificationResponse {
  return (
    isObject(value) &&
    value.schema_version === "1.0.0" &&
    Array.isArray(value.notifications) &&
    value.notifications.every(isNotification) &&
    (typeof value.next_cursor === "string" || value.next_cursor === null) &&
    typeof value.generated_at === "string"
  )
}

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  const incoming = new URL(request.url).searchParams
  const forwarded = new URLSearchParams()
  for (const name of ["cursor", "limit", "unread_only"]) {
    const value = incoming.get(name)
    if (value !== null) forwarded.set(name, value)
  }
  const query = forwarded.size === 0 ? "" : `?${forwarded.toString()}`

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/notifications${query}`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const body: unknown = await upstream.json().catch(() => null)
    if (!isNotificationResponse(body)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(body)
  } catch (error) {
    return upstreamErrorFailure(error)
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
  if (!isObject(body) || typeof body.notification_id !== "string" || !body.notification_id) {
    return Response.json({ error: "Notification is invalid" }, { status: 422 })
  }

  try {
    const upstream = await fetch(
      `${baseUrl.replace(/\/$/, "")}/v1/notifications/${encodeURIComponent(body.notification_id)}/read`,
      {
        method: "POST",
        headers: { accept: "application/json" },
        cache: "no-store",
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    )
    if (upstream.status === 404) {
      return Response.json({ error: "Notification not found" }, { status: 404 })
    }
    if (!upstream.ok) return upstreamStatusFailure(upstream.status)
    const result: unknown = await upstream.json().catch(() => null)
    if (!isNotification(result)) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }
    return Response.json(result)
  } catch (error) {
    return upstreamErrorFailure(error)
  }
}
