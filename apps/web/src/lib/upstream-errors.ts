// Server-only helpers shared by the proxy routes. They turn upstream failures
// into fixed, sanitized messages: upstream bodies, URLs, and error text are
// never forwarded to the browser.

export const BUSY_MESSAGE = "Risk service is busy — retrying"
export const UNREACHABLE_MESSAGE = "Risk service is starting or unreachable"
export const UNAVAILABLE_MESSAGE = "Risk service unavailable"

// Network-level failures that mean the service is down, restarting, or slow,
// rather than broken: the browser can retry these.
const TRANSIENT_CODES = new Set([
  "ECONNREFUSED",
  "ECONNRESET",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "ENOTFOUND",
  "EAI_AGAIN",
  "ETIMEDOUT",
  "EPIPE",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_SOCKET",
])

function errorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null
  const code = (error as { code?: unknown }).code
  return typeof code === "string" ? code : null
}

/** True for timeouts, refused connections, and other fetch-level network failures. */
export function isTransientNetworkError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === "TimeoutError" || error.name === "AbortError") return true
  const codes = [errorCode(error), errorCode(error.cause)]
  if (codes.some((code) => code !== null && TRANSIENT_CODES.has(code))) return true
  // Node's fetch reports every connection failure as `TypeError: fetch failed`.
  if (error instanceof TypeError && error.message === "fetch failed") return true
  return /\bECONNREFUSED\b|\bECONNRESET\b|\bETIMEDOUT\b/.test(error.message)
}

/** Response for a non-OK upstream status the route does not handle itself. */
export function upstreamStatusFailure(status: number): Response {
  if (status === 503) return Response.json({ error: BUSY_MESSAGE }, { status: 503 })
  return Response.json({ error: UNAVAILABLE_MESSAGE }, { status: 502 })
}

/** Response for an exception thrown while calling or reading the upstream. */
export function upstreamErrorFailure(error: unknown): Response {
  if (isTransientNetworkError(error)) return Response.json({ error: UNREACHABLE_MESSAGE }, { status: 503 })
  return Response.json({ error: UNAVAILABLE_MESSAGE }, { status: 502 })
}
