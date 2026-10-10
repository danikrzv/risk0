import { afterEach, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../test-fixtures"
import { ApiError, getCatalog, runAnalysis } from "./api"

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
})
