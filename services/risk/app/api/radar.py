import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.events import RadarEventBroker, get_event_broker
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
    NotificationReadAllResponse,
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


# GET /v1/graph returns a pruned view, not the whole topology: the focus set
# (catalog and visible entities), the focus set's neighbours, and the paths of
# the most important active incidents. See `_build_graph`.
GRAPH_INCIDENT_LIMIT = 50
# Most telling propagation paths (revealed first) kept per listed incident; the
# full set comes from GET /v1/graph/incidents/{id}.
GRAPH_PATHS_PER_INCIDENT = 2
# Focus entities with more live relationships than this (a chain, a protocol,
# a major asset) are hubs: their neighbours are only listed on request.
GRAPH_HUB_DEGREE = 64
# Neighbours of the focus set listed, those shared by most focus entities first.
GRAPH_NEIGHBOUR_LIMIT = 25
# Neighbours listed for an explicitly focused entity (?focus=), heaviest first.
GRAPH_FOCUS_NEIGHBOUR_LIMIT = 100
GRAPH_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _CachedGraph:
    revision: int
    event_sequence: int
    built_at: float
    response: RadarGraphResponse


class GraphCache:
    """Recently built graph responses.

    The web refetches GET /v1/graph after every SSE event and on focus, often
    several times in a row. An entry built at the current database revision is
    always served. The scheduler writes continuously, so an entry from an older
    revision is still served for `ttl_seconds` - unless an SSE event was
    published since it was built: events follow the writes they announce, and
    the refetch an event triggers must see them. Background churn thus costs at
    most one rebuild per TTL or event. User-initiated writes call
    `invalidate()` so their effect shows on the very next read.
    """

    def __init__(
        self,
        max_entries: int = 32,
        *,
        ttl_seconds: float = GRAPH_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._generation = 0
        self._entries: dict[tuple[object, ...], _CachedGraph] = {}
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        """Read before building; `put` drops a build that an invalidation overtook."""
        with self._lock:
            return self._generation

    def get(
        self, revision: int, event_sequence: int, key: tuple[object, ...]
    ) -> RadarGraphResponse | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.revision == revision or (
                entry.event_sequence == event_sequence
                and self._clock() - entry.built_at < self._ttl_seconds
            ):
                return entry.response
            del self._entries[key]
            return None

    def put(
        self,
        generation: int,
        revision: int,
        event_sequence: int,
        key: tuple[object, ...],
        response: RadarGraphResponse,
    ) -> None:
        """Store a response built from data read after `revision` and `event_sequence`."""
        with self._lock:
            if generation != self._generation:
                return
            current = self._entries.get(key)
            if current is not None and current.revision > revision:
                return
            if current is None and len(self._entries) >= self._max_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = _CachedGraph(
                revision, event_sequence, self._clock(), response
            )

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries = {}


@dataclass(frozen=True, slots=True)
class RadarContext:
    topology: TopologyRepository
    states: StateRepository
    incidents: IncidentRepository
    exposures: ExposureRepository
    # When set, GET /v1/graph is cached (see GraphCache) and built from one
    # consistent read transaction.
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
    focus: str | None = Query(default=None, min_length=1),
    context: RadarContext = Depends(get_radar_context),
    broker: RadarEventBroker = Depends(get_event_broker),
) -> RadarGraphResponse:
    key = (chain_id, entity_type, health_status, visible, incident_id, focus)
    # Read the versions before building: a write or event that lands mid-build
    # moves them, so this (possibly older) result is never treated as current.
    revision = context.database.revision if context.database is not None else None
    event_sequence = broker.sequence
    generation = context.graph_cache.generation
    if revision is not None:
        cached = context.graph_cache.get(revision, event_sequence, key)
        if cached is not None:
            return cached.model_copy(update={"generated_at": datetime.now(UTC)})
    with _read_snapshot(context):
        response = _build_graph(
            context, chain_id, entity_type, health_status, visible, incident_id, focus
        )
    if revision is not None:
        context.graph_cache.put(generation, revision, event_sequence, key, response)
    return response


@contextmanager
def _read_snapshot(context: RadarContext) -> Iterator[None]:
    """Run the repositories' reads in one transaction when the database is known."""
    if context.database is None:
        yield
        return
    with context.database.transaction():
        yield


def _build_graph(
    context: RadarContext,
    chain_id: int | None,
    entity_type: RadarEntityType | None,
    health_status: HealthStatus | None,
    visible: bool | None,
    incident_id: str | None,
    focus: str | None,
) -> RadarGraphResponse:
    """Build the pruned radar view.

    Without `incident_id` the view holds:
    - the focus set: catalog and visible entities;
    - up to GRAPH_NEIGHBOUR_LIMIT of their direct neighbours, those shared by
      the most focus entities first; hidden vaults and the neighbours of hubs
      (more than GRAPH_HUB_DEGREE relationships) are left out;
    - the GRAPH_INCIDENT_LIMIT most important active incidents (most severe,
      then sourced on the focus set, then most recent) with their sources and
      up to GRAPH_PATHS_PER_INCIDENT propagation paths each;
    - with `focus`, that entity and up to GRAPH_FOCUS_NEIGHBOUR_LIMIT of its
      neighbours (hidden vaults excepted), even when it is a hub.
    With `incident_id` the view is that incident's whole propagation footprint.
    The other filters then narrow the view; relationships and similarities are
    kept only between returned entities, incidents only when their source is.
    """
    if incident_id is not None:
        if context.incidents.get(incident_id) is None:
            raise _not_found("Incident")
        entity_ids = set(context.incidents.path_entity_ids((incident_id,)))
        candidates = None
    else:
        focus_ids = context.topology.catalog_or_visible_ids()
        candidates = context.incidents.top_active(
            GRAPH_INCIDENT_LIMIT, preferred_source_ids=focus_ids
        )
        entity_ids = {
            *focus_ids,
            *context.topology.neighbour_ids(
                focus_ids, max_degree=GRAPH_HUB_DEGREE, limit=GRAPH_NEIGHBOUR_LIMIT
            ),
            *context.incidents.path_entity_ids(
                tuple(incident.incident_id for incident in candidates),
                paths_per_incident=GRAPH_PATHS_PER_INCIDENT,
                preferred_target_ids=focus_ids,
            ),
        }
        if focus is not None:
            if context.topology.get_entity(focus) is None:
                raise _not_found("Entity")
            entity_ids.add(focus)
            entity_ids.update(
                context.topology.neighbour_ids(
                    (focus,), limit=GRAPH_FOCUS_NEIGHBOUR_LIMIT
                )
            )
    entities = context.topology.entities(
        chain_id=chain_id,
        entity_type=entity_type,
        visible=visible,
        entity_ids=sorted(entity_ids),
    )
    latest_health = context.states.latest_health_for(
        tuple(entity.entity_id for entity in entities)
    )
    if health_status is not None:
        entities = tuple(
            entity
            for entity in entities
            if latest_health.get(entity.entity_id) is not None
            and latest_health[entity.entity_id].status is health_status
        )
    entities = tuple(_entity_with_health(entity, latest_health) for entity in entities)
    returned_ids = tuple(entity.entity_id for entity in entities)
    returned = set(returned_ids)
    if candidates is None:
        candidates = context.incidents.top_active(
            GRAPH_INCIDENT_LIMIT, source_ids=returned_ids
        )
    return RadarGraphResponse(
        entities=entities,
        relationships=context.topology.relationships_among(returned_ids),
        similarities=context.topology.similarities_among(returned_ids),
        active_incidents=tuple(
            incident for incident in candidates if incident.source_entity_id in returned
        ),
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


@router.post("/notifications/read-all", response_model=NotificationReadAllResponse)
def mark_all_notifications_read(
    context: RadarContext = Depends(get_radar_context),
) -> NotificationReadAllResponse:
    updated = context.incidents.mark_all_read(datetime.now(UTC))
    context.graph_cache.invalidate()
    return NotificationReadAllResponse(updated=updated)


@router.post("/notifications/{notification_id}/read", response_model=Notification)
def mark_notification_read(
    notification_id: str, context: RadarContext = Depends(get_radar_context)
) -> Notification:
    notification = context.incidents.mark_read(notification_id, datetime.now(UTC))
    if notification is None:
        raise _not_found("Notification")
    context.graph_cache.invalidate()
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
    context.graph_cache.invalidate()
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
