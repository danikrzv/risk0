// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../../test-fixtures"
import { POST } from "./analysis/route"
import { GET } from "./catalog/route"
import { GET as getEvents } from "./events/route"
import { GET as getExposureImpact, POST as createExposure } from "./exposures/route"
import { GET as getGraph } from "./graph/route"
import { GET as getNotifications, POST as markNotificationRead } from "./notifications/route"

const UPSTREAM = "http://risk.internal:8000/secret-path"
const validRequest = {
  position: { mode: "lender", entity_id: "belem-rwa-eurcv", amount: "100000", numeraire: "EURCV" },
  scenarios: [{ scenario_id: "stablecoin-depeg", severity: "0.10" }],
}

function analysisCall(body: string): Request {
  return new Request("http://localhost/api/analysis", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  })
}

const radarGraphResponse = {
  schema_version: "1.0.0",
  entities: [],
  relationships: [],
  similarities: [],
  active_incidents: [],
  generated_at: "2026-10-10T10:00:00Z",
}

const incidentGraphResponse = {
  schema_version: "1.0.0",
  incident: { incident_id: "incident:oracle/1" },
  entities: [],
  relationships: [],
  similarities: [],
  exposures: [],
  impacts: [],
  generated_at: "2026-10-10T10:00:00Z",
}

const notificationResponse = {
  schema_version: "1.0.0",
  notifications: [],
  next_cursor: null,
  generated_at: "2026-10-10T10:00:00Z",
}

const exposureRequest = {
  entity_id: "vault:1:0x123",
  mode: "lender",
  amount: "100000",
  numeraire: "USDC",
  collateral_amount: null,
  debt_amount: null,
  ltv: null,
}

const exposureResponse = {
  ...exposureRequest,
  exposure_id: "exposure-1",
  created_at: "2026-10-10T10:00:00Z",
}

const notification = {
  notification_id: "notification-1",
  incident_id: "incident-1",
  severity: "CRITICAL",
  title: "Critical market",
  message: "Market health is critical.",
  created_at: "2026-10-10T10:00:00Z",
  read_at: "2026-10-10T10:01:00Z",
}

const exposureImpactResponse = {
  schema_version: "1.0.0",
  exposure: exposureResponse,
  impacts: [],
  generated_at: "2026-10-10T10:01:00Z",
}

beforeEach(() => {
  vi.stubEnv("RISK_API_URL", UPSTREAM)
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe("GET /api/catalog", () => {
  it("proxies the risk service catalog", async () => {
    const fetchMock = vi.fn(async () => Response.json(catalogResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await GET()

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(catalogResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/catalog`)
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("sanitizes upstream failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(`Traceback at ${UPSTREAM} key=fake-provider-key`, { status: 500 }),
      ),
    )

    const response = await GET()
    const text = await response.text()

    expect(response.status).toBe(502)
    expect(JSON.parse(text)).toEqual({ error: "Risk service unavailable" })
    expect(text).not.toContain("risk.internal")
    expect(text).not.toContain("fake-provider-key")
  })

  it("sanitizes network errors that mention the upstream URL", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error(`ECONNREFUSED ${UPSTREAM}`))))

    const response = await GET()
    const text = await response.text()

    expect(response.status).toBe(502)
    expect(text).not.toContain("risk.internal")
  })

  it("reports a missing server configuration without calling upstream", async () => {
    vi.stubEnv("RISK_API_URL", "")
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await GET()

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({ error: "Risk service is not configured" })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe("POST /api/analysis", () => {
  it("forwards the analysis request body and returns the response", async () => {
    const fetchMock = vi.fn(async () => Response.json(analysisResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await POST(analysisCall(JSON.stringify(validRequest)))

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(analysisResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/analysis`)
    expect(init.method).toBe("POST")
    expect(JSON.parse(String(init.body))).toEqual(validRequest)
  })

  it("rejects malformed JSON before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await POST(analysisCall("{not json"))

    expect(response.status).toBe(400)
    expect(await response.json()).toEqual({ error: "Request body must be valid JSON" })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("rejects oversized bodies before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await POST(analysisCall(JSON.stringify({ padding: "x".repeat(70_000) })))

    expect(response.status).toBe(413)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("maps upstream validation errors to a sanitized 422", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ detail: [{ msg: `bad input at ${UPSTREAM}` }] }, { status: 422 }),
      ),
    )

    const response = await POST(analysisCall(JSON.stringify(validRequest)))
    const text = await response.text()

    expect(response.status).toBe(422)
    expect(JSON.parse(text)).toEqual({ error: "The risk service rejected this position or scenario selection" })
    expect(text).not.toContain("risk.internal")
  })

  it("rejects a 200 response that is not an analysis", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ graph: {} })))

    const response = await POST(analysisCall(JSON.stringify(validRequest)))

    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
  })

  it("maps upstream server errors to a sanitized 502", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("RPC https://rpc.example.invalid/v2/fake-provider-key", { status: 500 })))

    const response = await POST(analysisCall(JSON.stringify(validRequest)))
    const text = await response.text()

    expect(response.status).toBe(502)
    expect(text).not.toContain("fake-provider-key")
  })
})

describe("GET /api/graph", () => {
  it("proxies an encoded incident graph identifier", async () => {
    const fetchMock = vi.fn(async () => Response.json(incidentGraphResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getGraph(new Request("http://localhost/api/graph?incident_graph_id=incident%3Aoracle%2F1"))

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(incidentGraphResponse)
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/graph/incidents/incident%3Aoracle%2F1`)
  })

  it("proxies graph filters with no-store and a bounded timeout", async () => {
    const fetchMock = vi.fn(async () => Response.json(radarGraphResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getGraph(
      new Request("http://localhost/api/graph?chain_id=1&entity_type=vault&visible=true"),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(radarGraphResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/graph?chain_id=1&entity_type=vault&visible=true`)
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("rejects graph responses that do not match the radar contract", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ entities: [] })))

    const response = await getGraph(new Request("http://localhost/api/graph"))

    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
  })

  it("does not leak graph upstream failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(`failure from ${UPSTREAM}`, { status: 500 })),
    )

    const response = await getGraph(new Request("http://localhost/api/graph"))
    const text = await response.text()

    expect(response.status).toBe(502)
    expect(text).not.toContain("risk.internal")
  })
})

describe("GET /api/notifications", () => {
  it("proxies pagination and validates notification responses", async () => {
    const fetchMock = vi.fn(async () => Response.json(notificationResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getNotifications(
      new Request("http://localhost/api/notifications?cursor=notification-10"),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(notificationResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/notifications?cursor=notification-10`)
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("rejects invalid notification responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ notifications: "secret" })))

    const response = await getNotifications(new Request("http://localhost/api/notifications"))

    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
  })
})

describe("POST /api/notifications", () => {
  it("marks a notification read through the sanitized proxy", async () => {
    const fetchMock = vi.fn(async () => Response.json(notification))
    vi.stubGlobal("fetch", fetchMock)

    const response = await markNotificationRead(
      new Request("http://localhost/api/notifications", {
        method: "POST",
        body: JSON.stringify({ notification_id: "notification-1" }),
      }),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(notification)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/notifications/notification-1/read`)
    expect(init.method).toBe("POST")
  })
})

describe("POST /api/exposures", () => {
  function exposureCall(body: string): Request {
    return new Request("http://localhost/api/exposures", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    })
  }

  it("forwards a valid exposure and validates the created exposure", async () => {
    const fetchMock = vi.fn(async () => Response.json(exposureResponse, { status: 201 }))
    vi.stubGlobal("fetch", fetchMock)

    const response = await createExposure(exposureCall(JSON.stringify(exposureRequest)))

    expect(response.status).toBe(201)
    expect(await response.json()).toEqual(exposureResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/exposures`)
    expect(init.method).toBe("POST")
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(JSON.parse(String(init.body))).toEqual(exposureRequest)
  })

  it("rejects malformed and oversized bodies before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const malformed = await createExposure(exposureCall("{not json"))
    const oversized = await createExposure(
      exposureCall(JSON.stringify({ ...exposureRequest, amount: "1".repeat(70_000) })),
    )

    expect(malformed.status).toBe(400)
    expect(oversized.status).toBe(413)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("rejects invalid exposure requests and sanitized upstream validation errors", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({ detail: `invalid at ${UPSTREAM}` }, { status: 422 }),
    )
    vi.stubGlobal("fetch", fetchMock)

    const invalid = await createExposure(exposureCall(JSON.stringify({ mode: "lender" })))
    expect(invalid.status).toBe(422)
    expect(fetchMock).not.toHaveBeenCalled()

    const rejected = await createExposure(exposureCall(JSON.stringify(exposureRequest)))
    const text = await rejected.text()
    expect(rejected.status).toBe(422)
    expect(text).not.toContain("risk.internal")
  })

  it("rejects invalid created exposures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ exposure_id: "secret" })))

    const response = await createExposure(exposureCall(JSON.stringify(exposureRequest)))

    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
  })
})

describe("GET /api/exposures", () => {
  it("loads a persisted exposure impact by ID", async () => {
    const fetchMock = vi.fn(async () => Response.json(exposureImpactResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getExposureImpact(
      new Request("http://localhost/api/exposures?exposure_id=exposure-1"),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(exposureImpactResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/exposures/exposure-1/impact`)
    expect(init.method).toBe("GET")
  })

  it("rejects a missing exposure ID before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await getExposureImpact(new Request("http://localhost/api/exposures"))

    expect(response.status).toBe(400)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe("GET /api/events", () => {
  it("preserves the SSE stream and Last-Event-ID without exposing upstream", async () => {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("id: 42\nevent: graph.updated\ndata: {}\n\n"))
        controller.close()
      },
    })
    const fetchMock = vi.fn(async () =>
      new Response(stream, {
        headers: { "content-type": "text/event-stream; charset=utf-8" },
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    const response = await getEvents(
      new Request("http://localhost/api/events", { headers: { "Last-Event-ID": "41" } }),
    )

    expect(response.status).toBe(200)
    expect(response.headers.get("content-type")).toContain("text/event-stream")
    expect(response.headers.get("cache-control")).toContain("no-cache")
    expect(await response.text()).toContain("event: graph.updated")
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/events`)
    expect(new Headers(init.headers).get("Last-Event-ID")).toBe("41")
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("rejects non-SSE and failed upstream responses generically", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(UPSTREAM, { status: 500 })))
    const failed = await getEvents(new Request("http://localhost/api/events"))
    expect(failed.status).toBe(502)
    expect(await failed.json()).toEqual({ error: "Risk service unavailable" })

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ url: UPSTREAM })))
    const invalid = await getEvents(new Request("http://localhost/api/events"))
    expect(invalid.status).toBe(502)
    expect(await invalid.json()).toEqual({ error: "Risk service returned an invalid response" })
  })
})
