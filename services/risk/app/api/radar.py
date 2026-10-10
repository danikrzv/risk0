import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.radar.analysis import ExposureImpactEstimator
from app.radar.contracts import (
    EntityHistoryResponse,
    ExposureCreateRequest,
    ExposureImpactResponse,
    HealthStatus,
    IncidentGraphResponse,
    ManualExposure,
    Notification,
    PositionMode,
    NotificationResponse,
    RadarEntity,
    RadarEntityType,
    RadarGraphResponse,
    SimilarityResponse,
    StateSnapshot,
)
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)


SERVICE_ROOT = Path(__file__).resolve().parents[2]


class GraphCache:
    """Built graph responses for the current database revision.

    The web refetches GET /v1/graph after every SSE event and on focus, often
    several times in a row with nothing changed in between. Any committed row
    change bumps the revision and drops every cached entry.
    """

    def __init__(self, max_entries: int = 32) -> None:
        self._max_entries = max_entries
        self._revision = -1
        self._entries: dict[tuple[object, ...], RadarGraphResponse] = {}
        self._lock = threading.Lock()

    def get(self, revision: int, key: tuple[object, ...]) -> RadarGraphResponse | None:
        with self._lock:
            if revision != self._revision:
                return None
            return self._entries.get(key)

    def put(
        self, revision: int, key: tuple[object, ...], response: RadarGraphResponse
    ) -> None:
        with self._lock:
            if revision < self._revision:
                return
            if revision != self._revision:
                self._revision = revision
                self._entries = {}
            if len(self._entries) >= self._max_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = response


@dataclass(frozen=True, slots=True)
class RadarContext:
    topology: TopologyRepository
    states: StateRepository
    incidents: IncidentRepository
    exposures: ExposureRepository
    # When set, GET /v1/graph is cached per database revision.
    database: RadarDatabase | None = None
    graph_cache: GraphCache = field(default_factory=GraphCache, compare=False)


@lru_cache(maxsize=1)
def get_radar_context() -> RadarContext:
    default_path = SERVICE_ROOT / ".cache" / "risk0.sqlite3"
    database = RadarDatabase(
        Path(
            os.environ.get(
                "RISK0_RADAR_DB_PATH",
                os.environ.get("RISK0_CACHE_PATH", default_path),
            )
        )
    )
    database.migrate()
    return RadarContext(
        topology=TopologyRepository(database),
        states=StateRepository(database),
        incidents=IncidentRepository(database),
        exposures=ExposureRepository(database),
        database=database,
    )


router = APIRouter(prefix="/v1")


@router.get("/graph", response_model=RadarGraphResponse)
def graph(
    chain_id: int | None = Query(default=None, gt=0),
    entity_type: RadarEntityType | None = None,
    health_status: HealthStatus | None = Query(default=None, alias="status"),
    visible: bool | None = None,
    incident_id: str | None = None,
    context: RadarContext = Depends(get_radar_context),
) -> RadarGraphResponse:
    key = (chain_id, entity_type, health_status, visible, incident_id)
    # Read the revision before building: a write that commits mid-build bumps
    # it, so this (possibly older) result is never served for the new data.
    revision = context.database.revision if context.database is not None else None
    if revision is not None:
        cached = context.graph_cache.get(revision, key)
        if cached is not None:
            return cached.model_copy(update={"generated_at": datetime.now(UTC)})
    response = _build_graph(
        context, chain_id, entity_type, health_status, visible, incident_id
    )
    if revision is not None:
        context.graph_cache.put(revision, key, response)
    return response


def _build_graph(
    context: RadarContext,
    chain_id: int | None,
    entity_type: RadarEntityType | None,
    health_status: HealthStatus | None,
    visible: bool | None,
    incident_id: str | None,
) -> RadarGraphResponse:
    entities = context.topology.entities(
        chain_id=chain_id, entity_type=entity_type, visible=visible
    )
    latest_health = context.states.latest_health_all()
    if health_status is not None:
        entities = tuple(
            entity
            for entity in entities
            if latest_health.get(entity.entity_id) is not None
            and latest_health[entity.entity_id].status is health_status
        )
    if incident_id is not None:
        incident = context.incidents.get(incident_id)
        if incident is None:
            raise _not_found("Incident")
        impacts = context.incidents.impacts(incident_id)
        affected = {incident.source_entity_id}
        for impact in impacts:
            affected.update(impact.path)
            affected.add(impact.target_entity_id)
        entities = tuple(entity for entity in entities if entity.entity_id in affected)
    entities = tuple(_entity_with_health(entity, latest_health) for entity in entities)
    entity_ids = {entity.entity_id for entity in entities}
    relationships = tuple(
        relationship
        for relationship in context.topology.relationships()
        if relationship.source_entity_id in entity_ids
        and relationship.target_entity_id in entity_ids
    )
    similarities = tuple(
        similarity
        for similarity in context.topology.similarities()
        if similarity.catalog_entity_id in entity_ids
        and similarity.candidate_entity_id in entity_ids
    )
    active_incidents = tuple(
        incident
        for incident in context.incidents.active()
        if incident.source_entity_id in entity_ids
    )
    return RadarGraphResponse(
        entities=entities,
        relationships=relationships,
        similarities=similarities,
        active_incidents=active_incidents,
        generated_at=datetime.now(UTC),
    )


@router.get("/graph/incidents/{incident_id}", response_model=IncidentGraphResponse)
def incident_graph(
    incident_id: str, context: RadarContext = Depends(get_radar_context)
) -> IncidentGraphResponse:
    incident = context.incidents.get(incident_id)
    if incident is None:
        raise _not_found("Incident")
    impacts = context.incidents.impacts(incident_id)
    entity_ids = {incident.source_entity_id}
    for impact in impacts:
        entity_ids.update(impact.path)
        entity_ids.add(impact.target_entity_id)
    latest_health = context.states.latest_health_all()
    entities = tuple(
        _entity_with_health(entity, latest_health)
        for entity in context.topology.entities(entity_ids=tuple(entity_ids))
    )
    relationships = tuple(
        relationship
        for relationship in context.topology.relationships(entity_ids=tuple(entity_ids))
        if relationship.source_entity_id in entity_ids
        and relationship.target_entity_id in entity_ids
    )
    similarities = tuple(
        similarity
        for similarity in context.topology.similarities()
        if similarity.catalog_entity_id in entity_ids
        and similarity.candidate_entity_id in entity_ids
    )
    exposures = context.exposures.list_exposures_for_incident(incident_id)
    return IncidentGraphResponse(
        incident=incident,
        entities=entities,
        relationships=relationships,
        similarities=similarities,
        exposures=exposures,
        impacts=impacts,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/entities/{entity_id}/history", response_model=EntityHistoryResponse
)
def entity_history(
    entity_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    context: RadarContext = Depends(get_radar_context),
) -> EntityHistoryResponse:
    entity = context.topology.get_entity(entity_id)
    if entity is None:
        raise _not_found("Entity")
    try:
        history = context.states.history(entity_id, limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid history cursor.") from None
    return EntityHistoryResponse(
        entity=_sanitize_entity(entity),
        snapshots=tuple(_sanitize_snapshot(item) for item in history.snapshots),
        health_states=history.health_states,
        next_cursor=history.next_cursor,
        generated_at=datetime.now(UTC),
    )


@router.get("/similarities/{vault_id}", response_model=SimilarityResponse)
def similarities(
    vault_id: str, context: RadarContext = Depends(get_radar_context)
) -> SimilarityResponse:
    entity = context.topology.get_entity(vault_id)
    if entity is None:
        raise _not_found("Vault")
    if entity.entity_type is not RadarEntityType.VAULT:
        raise HTTPException(status_code=422, detail="Similarity requires a vault.")
    return SimilarityResponse(
        vault_id=vault_id,
        similarities=context.topology.similarities(catalog_entity_id=vault_id),
        generated_at=datetime.now(UTC),
    )


@router.get("/notifications", response_model=NotificationResponse)
def notifications(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = None,
    unread_only: bool = False,
    context: RadarContext = Depends(get_radar_context),
) -> NotificationResponse:
    try:
        page = context.incidents.list_notifications(
            limit=limit, cursor=cursor, unread_only=unread_only
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid notification cursor.") from None
    return NotificationResponse(
        notifications=page.notifications,
        next_cursor=page.next_cursor,
        generated_at=datetime.now(UTC),
    )


@router.post("/notifications/{notification_id}/read", response_model=Notification)
def mark_notification_read(
    notification_id: str, context: RadarContext = Depends(get_radar_context)
) -> Notification:
    notification = context.incidents.mark_read(notification_id, datetime.now(UTC))
    if notification is None:
        raise _not_found("Notification")
    return notification


@router.post(
    "/exposures", response_model=ManualExposure, status_code=status.HTTP_201_CREATED
)
def create_exposure(
    request: ExposureCreateRequest,
    context: RadarContext = Depends(get_radar_context),
) -> ManualExposure:
    target = context.topology.get_entity(request.entity_id)
    if target is None:
        raise _not_found("Entity")
    allowed = (
        _BORROWER_TARGETS if request.mode is PositionMode.BORROWER else _LENDER_TARGETS
    )
    if target.entity_type not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                "Borrower exposures require a market or position."
                if request.mode is PositionMode.BORROWER
                else "Lender exposures require a vault, market, or position."
            ),
        )
    try:
        exposure = context.exposures.create(request, created_at=datetime.now(UTC))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid exposure.") from None
    _propagate_exposure(context, exposure)
    return exposure


_LENDER_TARGETS = frozenset(
    {RadarEntityType.VAULT, RadarEntityType.MARKET, RadarEntityType.POSITION}
)
_BORROWER_TARGETS = frozenset({RadarEntityType.MARKET, RadarEntityType.POSITION})


def _propagate_exposure(context: RadarContext, exposure: ManualExposure) -> None:
    """Estimate a new exposure against the stored impacts of every active incident.

    Monitoring only re-propagates when incidents or topology change, so without
    this a new exposure would show no impact until the next such change.
    """
    estimator = ExposureImpactEstimator()
    relationships = None
    impacts = []
    for incident in context.incidents.active():
        stored = context.incidents.impacts(incident.incident_id)
        if not stored:
            continue
        if relationships is None:
            relationships = context.topology.relationships()
        impacts.extend(
            estimator.estimate(exposure, incident, stored, relationships).impacts
        )
    if impacts:
        context.exposures.replace_impacts(exposure.exposure_id, impacts)


@router.get(
    "/exposures/{exposure_id}/impact", response_model=ExposureImpactResponse
)
def exposure_impact(
    exposure_id: str, context: RadarContext = Depends(get_radar_context)
) -> ExposureImpactResponse:
    exposure = context.exposures.get(exposure_id)
    if exposure is None:
        raise _not_found("Exposure")
    return ExposureImpactResponse(
        exposure=exposure,
        impacts=context.exposures.impacts(exposure_id),
        generated_at=datetime.now(UTC),
    )


def _entity_with_health(entity: RadarEntity, latest_health: dict) -> RadarEntity:
    health = latest_health.get(entity.entity_id)
    metadata = dict(entity.metadata)
    if health is not None:
        metadata.update(
            {
                "health_status": health.status.value,
                "health_score": health.score,
                "health_evaluated_at": health.evaluated_at.isoformat(),
            }
        )
    return _sanitize_entity(entity.model_copy(update={"metadata": metadata}))


def _sanitize_entity(entity: RadarEntity) -> RadarEntity:
    return entity.model_copy(update={"source": _sanitize_source(entity.source)})


def _sanitize_snapshot(snapshot: StateSnapshot) -> StateSnapshot:
    return snapshot.model_copy(update={"source": _sanitize_source(snapshot.source)})


def _sanitize_source(source: str) -> str:
    if "://" not in source:
        return source.split("?", 1)[0]
    parsed = urlsplit(source)
    if not parsed.hostname:
        return "redacted"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{resource} not found.")
