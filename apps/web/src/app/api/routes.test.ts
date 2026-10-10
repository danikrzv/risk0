// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../../test-fixtures"
import { POST } from "./analysis/route"
import { GET } from "./catalog/route"

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

  it("maps upstream server errors to a sanitized 502", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("RPC https://rpc.example.invalid/v2/fake-provider-key", { status: 500 })))

    const response = await POST(analysisCall(JSON.stringify(validRequest)))
    const text = await response.text()

    expect(response.status).toBe(502)
    expect(text).not.toContain("fake-provider-key")
  })
})
