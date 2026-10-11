// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../../test-fixtures"
import { POST } from "./analysis/route"
import { POST as postBook } from "./book/route"
import { GET } from "./catalog/route"
import { GET as getEvents } from "./events/route"
import { GET as getExposureImpact, POST as createExposure } from "./exposures/route"
import { GET as getEntityHealth } from "./graph/health/route"
import { GET as getGraph } from "./graph/route"
import { POST as markAllNotificationsRead } from "./notifications/read-all/route"
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
  incident: {
    incident_id: "incident:oracle/1",
    source_entity_id: "oracle:1:oracle",
    severity: "CRITICAL",
    status: "OPEN",
    trigger_metrics: {},
    rule_version: "oracle-health-v1",
    first_observed_at: "2026-10-10T10:00:00Z",
    last_observed_at: "2026-10-10T10:00:00Z",
    resolved_at: null,
    reasons: ["Oracle deviation is critical."],
  },
  entities: [],
  relationships: [],
  similarities: [],
  exposures: [],
  impacts: [
    {
      incident_id: "incident:oracle/1",
      target_entity_id: "vault:1:catalog",
      path: ["oracle:1:oracle", "vault:1:catalog"],
      distance: 1,
      confidence: "0.9",
      estimated_affected_amount: null,
      reasons: [],
      revealed: false,
      alternate_path_count: 0,
    },
  ],
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

    expect(response.status).toBe(503)
    expect(JSON.parse(text)).toEqual({ error: "Risk service is starting or unreachable" })
    expect(text).not.toContain("risk.internal")
  })

  it("maps a busy upstream (503) to a retryable 503 without forwarding its body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Risk database busy" }, { status: 503 })))

    const response = await GET()
    const text = await response.text()

    expect(response.status).toBe(503)
    expect(JSON.parse(text)).toEqual({ error: "Risk service is busy — retrying" })
    expect(text).not.toContain("database")
  })

  it("maps refused connections and timeouts to 503 and other errors to 502", async () => {
    const refused = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } })
    const timeout = new DOMException("The operation was aborted due to timeout", "TimeoutError")
    for (const failure of [refused, timeout]) {
      vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(failure)))
      const response = await GET()
      expect(response.status).toBe(503)
      expect(await response.json()).toEqual({ error: "Risk service is starting or unreachable" })
    }

    vi.stubGlobal("fetch", vi.fn(async () => new Response("not json", { status: 200 })))
    const malformed = await GET()
    expect(malformed.status).toBe(502)
    expect(await malformed.json()).toEqual({ error: "Risk service unavailable" })
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

describe("POST /api/book", () => {
  const validBook = {
    pockets: [{ position: validRequest.position, weight: "0.1" }],
    scenarios: validRequest.scenarios,
  }
  const bookResponse = {
    schema_version: "1.0.0",
    pockets: [{ pocket_id: "pocket-1", decision: "UNAVAILABLE" }],
    totals: {},
    generated_at: "2026-10-11T09:00:00Z",
  }
  const bookCall = (body: string) =>
    new Request("http://localhost/api/book", { method: "POST", headers: { "content-type": "application/json" }, body })

  it("forwards the book with a bounded timeout and returns the response", async () => {
    const fetchMock = vi.fn(async () => Response.json(bookResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await postBook(bookCall(JSON.stringify(validBook)))

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(bookResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/book`)
    expect(init.method).toBe("POST")
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(JSON.parse(String(init.body))).toEqual(validBook)
  })

  it("rejects malformed and oversized bodies before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    expect((await postBook(bookCall("{not json"))).status).toBe(400)
    expect((await postBook(bookCall(JSON.stringify({ padding: "x".repeat(70_000) })))).status).toBe(413)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("maps upstream validation errors to a sanitized 422", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: [{ msg: `weights at ${UPSTREAM}` }] }, { status: 422 })))

    const response = await postBook(bookCall(JSON.stringify(validBook)))
    const text = await response.text()

    expect(response.status).toBe(422)
    expect(JSON.parse(text)).toEqual({ error: "The risk service rejected this book: check the pockets, weights, and scenarios" })
    expect(text).not.toContain("risk.internal")
  })

  it("rejects responses that are not a book and never forwards upstream failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ pockets: "nope" })))
    expect((await postBook(bookCall(JSON.stringify(validBook)))).status).toBe(502)

    vi.stubGlobal("fetch", vi.fn(async () => new Response("RPC https://rpc.example.invalid/v2/fake-provider-key", { status: 500 })))
    const failed = await postBook(bookCall(JSON.stringify(validBook)))
    expect(failed.status).toBe(502)
    expect(await failed.text()).not.toContain("fake-provider-key")

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "busy" }, { status: 503 })))
    expect((await postBook(bookCall(JSON.stringify(validBook)))).status).toBe(503)
  })

  it("reports a missing server configuration without calling upstream", async () => {
    vi.stubEnv("RISK_API_URL", "")
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await postBook(bookCall(JSON.stringify(validBook)))

    expect(response.status).toBe(503)
    expect(fetchMock).not.toHaveBeenCalled()
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

  it("forwards a focus entity and maps an unknown one to a sanitized 404", async () => {
    const fetchMock = vi.fn(async () => Response.json(radarGraphResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getGraph(new Request("http://localhost/api/graph?focus=vault%3A1%3A0xabc"))
    expect(response.status).toBe(200)
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/graph?focus=vault%3A1%3A0xabc`)

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: `unknown entity at ${UPSTREAM}` }, { status: 404 })))
    const missing = await getGraph(new Request("http://localhost/api/graph?focus=vault%3A1%3Amissing"))
    const text = await missing.text()
    expect(missing.status).toBe(404)
    expect(JSON.parse(text)).toEqual({ error: "Graph entity not found" })
    expect(text).not.toContain("risk.internal")
  })

  it("rejects graph responses that do not match the radar contract", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ entities: [] })))

    const response = await getGraph(new Request("http://localhost/api/graph"))

    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
  })

  it("rejects incident graphs whose incident or impacts do not match the contract", async () => {
    for (const body of [
      { ...incidentGraphResponse, incident: { incident_id: "incident:oracle/1" } },
      { ...incidentGraphResponse, impacts: [{ target_entity_id: "vault:1:catalog", path: "oracle" }] },
      { ...incidentGraphResponse, entities: [{ entity_id: 1 }] },
    ]) {
      vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)))
      const response = await getGraph(new Request("http://localhost/api/graph?incident_graph_id=incident%3Aoracle%2F1"))
      expect(response.status).toBe(502)
    }
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

describe("POST /api/notifications/read-all", () => {
  it("marks every notification read and returns only the validated summary", async () => {
    const fetchMock = vi.fn(async () => Response.json({ schema_version: "1.0.0", updated: 24_031, debug: UPSTREAM }))
    vi.stubGlobal("fetch", fetchMock)

    const response = await markAllNotificationsRead()
    const text = await response.text()

    expect(response.status).toBe(200)
    expect(JSON.parse(text)).toEqual({ schema_version: "1.0.0", updated: 24_031 })
    expect(text).not.toContain("risk.internal")
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/notifications/read-all`)
    expect(init.method).toBe("POST")
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("rejects invalid summaries and maps upstream failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: "1.0.0", updated: "all" })))
    expect((await markAllNotificationsRead()).status).toBe(502)

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Risk database busy" }, { status: 503 })))
    const busy = await markAllNotificationsRead()
    expect(busy.status).toBe(503)
    expect(await busy.json()).toEqual({ error: "Risk service is busy — retrying" })

    vi.stubGlobal("fetch", vi.fn(async () => new Response(`failure from ${UPSTREAM}`, { status: 500 })))
    const failed = await markAllNotificationsRead()
    expect(failed.status).toBe(502)
    expect(await failed.text()).not.toContain("risk.internal")
  })

  it("reports a missing server configuration without calling upstream", async () => {
    vi.stubEnv("RISK_API_URL", "")
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    expect((await markAllNotificationsRead()).status).toBe(503)
    expect(fetchMock).not.toHaveBeenCalled()
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

const entityHealthResponse = {
  schema_version: "1.0.0",
  entity_id: "market:1:weth/usdc",
  health: {
    entity_id: "market:1:weth/usdc",
    status: "WARNING",
    score: "0.5",
    reasons: ["utilization exceeds warning threshold"],
    missing_fields: [],
    evaluated_at: "2026-10-10T12:05:00Z",
    rule_version: "radar-health-v1",
    derivation: {
      entity_type: "market",
      freshness: "fresh",
      checks: [{
        stage: "metrics",
        rule: "Utilization at or above the warning threshold",
        metric: "utilization",
        observed: "0.93",
        note: null,
        operator: ">=",
        threshold: "0.90",
        outcome: "breach",
        leads_to: "WARNING",
        decisive: true,
      }],
      dependencies: {
        dependencies: [{ entity_id: "oracle:1:a", status: "HEALTHY", counted: "full", weight: null }],
        weighted_totals: { HEALTHY: "0", WARNING: "0", CRITICAL: "0", UNKNOWN: "0" },
        material_allocation: "0.05",
        status: "HEALTHY",
        reason: "dependencies normal",
      },
      raw_status: "WARNING",
      raw_reasons: ["utilization exceeds warning threshold"],
      hysteresis: {
        previous_status: "HEALTHY",
        raw_status: "WARNING",
        rule: "warning_confirmation",
        consecutive_abnormal: 1,
        consecutive_normal: 0,
        counter: 2,
        cycles_required: 2,
        held: false,
        final_status: "WARNING",
      },
      final_status: "WARNING",
      score: "0.5",
      score_mapping: { HEALTHY: "1", WARNING: "0.5", CRITICAL: "0", UNKNOWN: null },
      rule_version: "radar-health-v1",
    },
  },
  generated_at: "2026-10-10T12:06:00Z",
}

describe("GET /api/graph/health", () => {
  it("proxies one entity's health with an encoded ID, no-store, and a bounded timeout", async () => {
    const fetchMock = vi.fn(async () => Response.json(entityHealthResponse))
    vi.stubGlobal("fetch", fetchMock)

    const response = await getEntityHealth(
      new Request("http://localhost/api/graph/health?entity_id=market%3A1%3Aweth%2Fusdc"),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual(entityHealthResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${UPSTREAM}/v1/graph/entities/market%3A1%3Aweth%2Fusdc/health`)
    expect(init.cache).toBe("no-store")
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it("accepts states without a derivation and entities never evaluated", async () => {
    for (const body of [
      { ...entityHealthResponse, health: { ...entityHealthResponse.health, derivation: null } },
      { ...entityHealthResponse, health: null },
    ]) {
      vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)))
      const response = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
      expect(response.status).toBe(200)
      expect(await response.json()).toEqual(body)
    }
  })

  it("rejects a missing entity ID before calling upstream", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await getEntityHealth(new Request("http://localhost/api/graph/health"))

    expect(response.status).toBe(400)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("rejects responses whose derivation does not match the contract", async () => {
    const derivation = entityHealthResponse.health.derivation
    for (const broken of [
      { ...derivation, checks: [{ ...derivation.checks[0], decisive: "yes" }] },
      { ...derivation, checks: [{ ...derivation.checks[0], outcome: "maybe" }] },
      { ...derivation, hysteresis: { ...derivation.hysteresis, held: null } },
      { ...derivation, dependencies: { ...derivation.dependencies, weighted_totals: {} } },
    ]) {
      const body = { ...entityHealthResponse, health: { ...entityHealthResponse.health, derivation: broken } }
      vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)))
      const response = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
      expect(response.status).toBe(502)
      expect(await response.json()).toEqual({ error: "Risk service returned an invalid response" })
    }
  })

  it("maps upstream failures without leaking them", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: `missing at ${UPSTREAM}` }, { status: 404 })))
    const missing = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
    const missingText = await missing.text()
    expect(missing.status).toBe(404)
    expect(JSON.parse(missingText)).toEqual({ error: "Graph entity not found" })
    expect(missingText).not.toContain("risk.internal")

    vi.stubGlobal("fetch", vi.fn(async () => new Response(`busy at ${UPSTREAM}`, { status: 503 })))
    const busy = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
    expect(busy.status).toBe(503)
    expect(await busy.text()).not.toContain("risk.internal")

    vi.stubGlobal("fetch", vi.fn(async () => new Response(`failure from ${UPSTREAM}`, { status: 500 })))
    const failed = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
    expect(failed.status).toBe(502)
    expect(await failed.text()).not.toContain("risk.internal")

    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fetch failed") }))
    const unreachable = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))
    expect(unreachable.status).toBe(503)
  })

  it("reports a missing server configuration without calling upstream", async () => {
    vi.stubEnv("RISK_API_URL", "")
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)

    const response = await getEntityHealth(new Request("http://localhost/api/graph/health?entity_id=a"))

    expect(response.status).toBe(503)
    expect(fetchMock).not.toHaveBeenCalled()
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

  it("resumes from a last_event_id query parameter and drops malformed event IDs", async () => {
    const fetchMock = vi.fn(async () => new Response("", { headers: { "content-type": "text/event-stream" } }))
    vi.stubGlobal("fetch", fetchMock)

    await getEvents(new Request("http://localhost/api/events?last_event_id=17"))
    await getEvents(new Request("http://localhost/api/events?last_event_id=abc", { headers: { "Last-Event-ID": "-3" } }))

    const [, resumed] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(new Headers(resumed.headers).get("Last-Event-ID")).toBe("17")
    const [, malformed] = fetchMock.mock.calls[1] as unknown as [string, RequestInit]
    expect(new Headers(malformed.headers).has("Last-Event-ID")).toBe(false)
  })

  it("aborts the upstream stream when the browser disconnects", async () => {
    const fetchMock = vi.fn(async () => new Response("", { headers: { "content-type": "text/event-stream" } }))
    vi.stubGlobal("fetch", fetchMock)
    const client = new AbortController()

    await getEvents(new Request("http://localhost/api/events", { signal: client.signal }))
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(init.signal?.aborted).toBe(false)
    client.abort()
    expect(init.signal?.aborted).toBe(true)
  })
})

