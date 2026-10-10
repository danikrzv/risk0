import type { AnalysisRequest, AnalysisResponse, CatalogResponse } from "@risk0/contracts"

// Browser-safe client: only same-origin proxy routes are called, so the risk
// service URL and any provider credentials stay on the server.

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { accept: "application/json", ...init.headers },
    })
  } catch {
    throw new ApiError("Could not reach the Risk0 server", 0)
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body: unknown = await response.json()
      if (body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string") {
        message = (body as { error: string }).error
      }
    } catch {
      // Non-JSON error bodies are never shown to the user.
    }
    throw new ApiError(message, response.status)
  }
  return (await response.json()) as T
}

export function getCatalog(): Promise<CatalogResponse> {
  return request<CatalogResponse>("/api/catalog", { method: "GET" })
}

export function runAnalysis(analysis: AnalysisRequest): Promise<AnalysisResponse> {
  return request<AnalysisResponse>("/api/analysis", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(analysis),
  })
}
