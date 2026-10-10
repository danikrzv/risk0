const EVENT_STREAM_TIMEOUT_MS = 5 * 60_000
const MAX_EVENT_ID_CHARACTERS = 32
const EVENT_ID = /^\d+$/

export const dynamic = "force-dynamic"

export async function GET(request: Request): Promise<Response> {
  const baseUrl = process.env.RISK_API_URL
  if (!baseUrl) return Response.json({ error: "Risk service is not configured" }, { status: 503 })

  const headers = new Headers({ accept: "text/event-stream" })
  // EventSource sends Last-Event-ID itself only on its own retries; the client
  // passes it as a query parameter when it reconnects with a new EventSource.
  const lastEventId =
    request.headers.get("Last-Event-ID") ?? new URL(request.url).searchParams.get("last_event_id")
  // The risk service rejects non-numeric IDs, which would turn into a reconnect loop.
  if (lastEventId && lastEventId.length <= MAX_EVENT_ID_CHARACTERS && EVENT_ID.test(lastEventId)) {
    headers.set("Last-Event-ID", lastEventId)
  }

  try {
    const upstream = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/events`, {
      method: "GET",
      headers,
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(EVENT_STREAM_TIMEOUT_MS)]),
    })
    if (!upstream.ok) return Response.json({ error: "Risk service unavailable" }, { status: 502 })
    if (!upstream.body || !upstream.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream")) {
      return Response.json({ error: "Risk service returned an invalid response" }, { status: 502 })
    }

    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "text/event-stream",
        "cache-control": "no-cache, no-store, no-transform",
        connection: "keep-alive",
        "x-accel-buffering": "no",
      },
    })
  } catch {
    return Response.json({ error: "Risk service unavailable" }, { status: 502 })
  }
}
