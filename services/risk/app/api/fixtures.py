from fastapi import APIRouter, Depends, HTTPException, status

from app.api.events import RadarEventBroker, get_event_broker
from app.api.radar import RadarContext, get_radar_context
from app.fixture_mode import (
    FIXTURE,
    data_mode,
    open_fixture_radar_incident,
    resolve_fixture_radar_incident,
)
from app.radar.contracts import Incident


router = APIRouter(prefix="/v1/fixtures/radar")


def _fixture_only() -> None:
    if data_mode() != FIXTURE:
        raise HTTPException(status_code=404, detail="Not found.")


@router.post(
    "/oracle-incident/open",
    response_model=Incident,
    status_code=status.HTTP_201_CREATED,
)
def open_oracle_incident(
    context: RadarContext = Depends(get_radar_context),
    broker: RadarEventBroker = Depends(get_event_broker),
) -> Incident:
    _fixture_only()
    incident = open_fixture_radar_incident(context, broker)
    context.graph_cache.invalidate()
    return incident


@router.post("/oracle-incident/resolve", response_model=Incident)
def resolve_oracle_incident(
    context: RadarContext = Depends(get_radar_context),
    broker: RadarEventBroker = Depends(get_event_broker),
) -> Incident:
    _fixture_only()
    incident = resolve_fixture_radar_incident(context, broker)
    context.graph_cache.invalidate()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found.")
    return incident
