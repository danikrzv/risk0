import type {
  AnalysisRequest,
  AnalysisResponse,
  CatalogResponse,
  ExposureCreateRequest,
  ExposureImpactResponse,
  HealthStatus,
  IncidentGraphResponse,
  ManualExposure,
  Notification,
  NotificationResponse,
  RadarEntityType,
  RadarGraphResponse,
} from "@risk0/contracts"

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

export interface RadarGraphFilters {
  chainId?: number
  entityType?: RadarEntityType
  status?: HealthStatus
  visible?: boolean
  incidentId?: string
}

export function getRadarGraph(filters: RadarGraphFilters = {}): Promise<RadarGraphResponse> {
  const query = new URLSearchParams()
  if (filters.chainId !== undefined) query.set("chain_id", String(filters.chainId))
  if (filters.entityType !== undefined) query.set("entity_type", filters.entityType)
  if (filters.status !== undefined) query.set("status", filters.status)
  if (filters.visible !== undefined) query.set("visible", String(filters.visible))
  if (filters.incidentId !== undefined) query.set("incident_id", filters.incidentId)
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  return request<RadarGraphResponse>(`/api/graph${suffix}`, { method: "GET" })
}

export function getIncidentGraph(incidentId: string): Promise<IncidentGraphResponse> {
  const query = new URLSearchParams({ incident_graph_id: incidentId })
  return request<IncidentGraphResponse>(`/api/graph?${query.toString()}`, { method: "GET" })
}

export function getNotifications(
  cursor?: string,
  options: { limit?: number; unreadOnly?: boolean } = {},
): Promise<NotificationResponse> {
  const query = new URLSearchParams()
  if (cursor !== undefined) query.set("cursor", cursor)
  if (options.limit !== undefined) query.set("limit", String(options.limit))
  if (options.unreadOnly !== undefined) query.set("unread_only", String(options.unreadOnly))
  const suffix = query.size === 0 ? "" : `?${query.toString()}`
  return request<NotificationResponse>(`/api/notifications${suffix}`, { method: "GET" })
}

export function markNotificationRead(notificationId: string): Promise<Notification> {
  return request<Notification>("/api/notifications", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ notification_id: notificationId }),
  })
}

export function createExposure(exposure: ExposureCreateRequest): Promise<ManualExposure> {
  return request<ManualExposure>("/api/exposures", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(exposure),
  })
}

export function getExposureImpact(exposureId: string): Promise<ExposureImpactResponse> {
  const query = new URLSearchParams({ exposure_id: exposureId })
  return request<ExposureImpactResponse>(`/api/exposures?${query.toString()}`, { method: "GET" })
}

export function getRadarEventsUrl(lastEventId: number | null = null): string {
  return lastEventId === null ? "/api/events" : `/api/events?last_event_id=${lastEventId}`
}
