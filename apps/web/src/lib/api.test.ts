import { afterEach, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../test-fixtures"
import {
  ApiError,
  createExposure,
  getCatalog,
  getExposureImpact,
  getIncidentGraph,
  getNotifications,
  getRadarEventsUrl,
  getRadarGraph,
  isRetryableError,
  markAllNotificationsRead,
  markNotificationRead,
  retryDelayMs,
  runAnalysis,
} from "./api"

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("browser API client", () => {
  it("loads the catalog from the same-origin proxy route", async () => {
    const fetchMock = vi.fn(async () => Response.json(catalogResponse))
    vi.stubGlobal("fetch", fetchMock)

    await expect(getCatalog()).resolves.toEqual(catalogResponse)
    expect(fetchMock).toHaveBeenCalledWith("/api/catalog", expect.objectContaining({ method: "GET" }))
  })

  it("posts analysis requests to the same-origin proxy route", async () => {
    const fetchMock = vi.fn(async () => Response.json(analysisResponse))
    vi.stubGlobal("fetch", fetchMock)
    const request = {
      position: { mode: "lender" as const, entity_id: "belem-rwa-eurcv", amount: "100000", numeraire: "EURCV" },
      scenarios: [{ scenario_id: "stablecoin-depeg" as const, severity: "0.10" }] as [
        { scenario_id: "stablecoin-depeg"; severity: string },
      ],
    }

    await expect(runAnalysis(request)).resolves.toEqual(analysisResponse)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe("/api/analysis")
    expect(init.method).toBe("POST")
    expect(JSON.parse(String(init.body))).toEqual(request)
  })

  it("surfaces the proxy's sanitized error message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: "Risk service unavailable" }, { status: 502 })),
    )

    const error = await getCatalog().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).message).toBe("Risk service unavailable")
    expect((error as ApiError).status).toBe(502)
  })

  it("uses a generic message when the proxy response is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>boom</html>", { status: 500 })))

    await expect(runAnalysis({} as never)).rejects.toThrow("Request failed (500)")
  })

  it("reports network failures without leaking details", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("connect ECONNREFUSED 10.0.0.1"))))

    await expect(getCatalog()).rejects.toThrow("Could not reach the Risk0 server")
  })

  it("loads a filtered radar graph from the same-origin proxy", async () => {
    const graph = {
      schema_version: "1.0.0",
      entities: [],
      relationships: [],
      similarities: [],
      active_incidents: [],
      generated_at: "2026-10-10T10:00:00Z",
    }
    const fetchMock = vi.fn(async () => Response.json(graph))
    vi.stubGlobal("fetch", fetchMock)

    await expect(
      getRadarGraph({ chainId: 1, entityType: "vault", status: "WARNING", visible: true }),
    ).resolves.toEqual(graph)
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/graph?chain_id=1&entity_type=vault&status=WARNING&visible=true",
      expect.objectContaining({ method: "GET" }),
    )
  })

  it("loads paginated notifications from the same-origin proxy", async () => {
    const notifications = {
      schema_version: "1.0.0",
      notifications: [],
      next_cursor: null,
      generated_at: "2026-10-10T10:00:00Z",
    }
    const fetchMock = vi.fn(async () => Response.json(notifications))
    vi.stubGlobal("fetch", fetchMock)

    await expect(getNotifications("notification-10")).resolves.toEqual(notifications)
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/notifications?cursor=notification-10",
      expect.objectContaining({ method: "GET" }),
    )
  })

  it("creates manual exposures through the same-origin proxy", async () => {
    const exposure = {
      entity_id: "vault:1:0x123",
      mode: "lender" as const,
      amount: "100000",
      numeraire: "USDC",
      collateral_amount: null,
      debt_amount: null,
      ltv: null,
    }
    const created = {
      ...exposure,
      exposure_id: "exposure-1",
      created_at: "2026-10-10T10:00:00Z",
    }
    const fetchMock = vi.fn(async () => Response.json(created, { status: 201 }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(createExposure(exposure)).resolves.toEqual(created)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe("/api/exposures")
    expect(init.method).toBe("POST")
    expect(JSON.parse(String(init.body))).toEqual(exposure)
  })

  it("provides only the same-origin SSE endpoint to browser consumers", () => {
    expect(getRadarEventsUrl()).toBe("/api/events")
  })

  it("marks notifications read and loads exposure impact through same-origin routes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ notification_id: "notification-1" }))
      .mockResolvedValueOnce(Response.json({ schema_version: "1.0.0", impacts: [] }))
    vi.stubGlobal("fetch", fetchMock)

    await markNotificationRead("notification-1")
    await getExposureImpact("exposure-1")

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/notifications")
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      notification_id: "notification-1",
    })
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/exposures?exposure_id=exposure-1")
  })

  it("loads a resolved incident graph through the same-origin graph proxy", async () => {
    const incidentGraph = { schema_version: "1.0.0", incident: { incident_id: "incident-1" }, entities: [], relationships: [], similarities: [], exposures: [], impacts: [], generated_at: "2026-10-10T10:00:00Z" }
    const fetchMock = vi.fn(async () => Response.json(incidentGraph))
    vi.stubGlobal("fetch", fetchMock)

    await expect(getIncidentGraph("incident:oracle/1")).resolves.toEqual(incidentGraph)
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/graph?incident_graph_id=incident%3Aoracle%2F1",
      expect.objectContaining({ method: "GET" }),
    )
  })

  it("marks every notification read through the same-origin proxy", async () => {
    const fetchMock = vi.fn(async () => Response.json({ schema_version: "1.0.0", updated: 3 }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(markAllNotificationsRead()).resolves.toEqual({ schema_version: "1.0.0", updated: 3 })
    expect(fetchMock).toHaveBeenCalledWith("/api/notifications/read-all", expect.objectContaining({ method: "POST" }))
  })

  it("retries only busy or unreachable failures, with capped exponential backoff", () => {
    expect(isRetryableError(new ApiError("Risk service is busy — retrying", 503))).toBe(true)
    expect(isRetryableError(new ApiError("Could not reach the Risk0 server", 0))).toBe(true)
    expect(isRetryableError(new ApiError("Risk service unavailable", 502))).toBe(false)
    expect(isRetryableError(new Error("boom"))).toBe(false)

    const middle = () => 0.5
    expect([0, 1, 2, 3, 10].map((attempt) => retryDelayMs(attempt, middle))).toEqual([1_000, 2_000, 4_000, 8_000, 30_000])
    expect(retryDelayMs(0, () => 0)).toBe(800)
    expect(retryDelayMs(0, () => 1)).toBe(1_200)
  })
})
