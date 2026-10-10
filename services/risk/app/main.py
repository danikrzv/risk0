import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.adapters.morpho import DEFAULT_BASE_URL, MorphoClient
from app.adapters.rpc_registry import RpcRegistry
from app.api.analysis import router as analysis_router
from app.api.catalog import router as catalog_router
from app.api.events import get_event_broker, router as events_router
from app.api.fixtures import router as fixtures_router
from app.api.radar import get_radar_context, router as radar_router
from app.catalog.repository import CatalogRepository
from app.config import radar_chain_ids
from app.fixture_mode import FIXTURE, data_mode, fixture_morpho_client
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.sqlite import is_busy_error
from app.radar.collectors import ChainStateCollector, MorphoStateCollector
from app.radar.discovery import MorphoDiscovery
from app.radar.repositories import (
    RadarDatabase,
    SchedulerRunRepository,
    default_radar_path,
)
from app.radar.scheduler import RadarScheduler
from app.radar.service import RadarService


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
RADAR_ENABLED_ENV = "RISK0_RADAR_ENABLED"
DATABASE_BUSY_DETAIL = "Risk database busy"

logger = logging.getLogger(__name__)


def radar_enabled() -> bool:
    value = os.environ.get(RADAR_ENABLED_ENV, "").strip().lower()
    if not value:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{RADAR_ENABLED_ENV} must be a boolean")


class _NoChainState:
    """Fixture-mode chain collector: recorded data only, so no RPC is ever opened."""

    def collect_all(self, chain_ids, now):
        return ()


def build_radar_scheduler() -> RadarScheduler:
    cache_path = Path(
        os.environ.get("RISK0_CACHE_PATH", SERVICE_ROOT / ".cache" / "risk0.sqlite3")
    )
    database_path = Path(os.environ.get("RISK0_RADAR_DB_PATH", default_radar_path(cache_path)))
    catalog_path = Path(
        os.environ.get(
            "RISK0_CATALOG_PATH", REPOSITORY_ROOT / "data" / "catalog" / "ethereum.json"
        )
    )
    cache = SQLiteCache(cache_path)
    fixture = data_mode() == FIXTURE
    morpho = (
        fixture_morpho_client()
        if fixture
        else MorphoClient(os.environ.get("MORPHO_API_URL", DEFAULT_BASE_URL), cache=cache)
    )
    radar_context = get_radar_context()
    # Share the API's database so scheduler writes invalidate its graph cache.
    database = radar_context.database
    if database is None:
        database = RadarDatabase(database_path)
        database.migrate()
    runs = SchedulerRunRepository(database)
    chain_ids = radar_chain_ids()
    service = RadarService(
        discovery=MorphoDiscovery(
            morpho,
            catalog=CatalogRepository(catalog_path).list_entries(),
            chain_ids=chain_ids,
        ),
        morpho_collector=MorphoStateCollector(morpho),
        chain_collector=(
            _NoChainState()
            if fixture
            else ChainStateCollector(RpcRegistry.from_environment())
        ),
        topology=radar_context.topology,
        states=radar_context.states,
        incidents=radar_context.incidents,
        exposures=radar_context.exposures,
        events=get_event_broker(),
        chain_ids=chain_ids,
    )
    return RadarScheduler(service, runs)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Fail fast on a mistyped RISK0_DATA_MODE (or radar chain allowlist) rather
    # than serving a healthy /v1/health while every request or cycle fails.
    mode = data_mode()
    radar_chain_ids()
    if mode == FIXTURE and os.environ.get("RISK0_RADAR_SEED_FIXTURES") == "1":
        from app.fixture_mode import seed_fixture_radar

        seed_fixture_radar(get_radar_context())
    scheduler = build_radar_scheduler() if radar_enabled() else None
    application.state.radar_scheduler = scheduler
    if scheduler is not None:
        await scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()


app = FastAPI(title="Risk0 Risk Service", version="0.1.0", lifespan=lifespan)
app.include_router(catalog_router)
app.include_router(analysis_router)
app.include_router(radar_router)
app.include_router(events_router)
app.include_router(fixtures_router)


@app.exception_handler(sqlite3.OperationalError)
async def database_busy(request: Request, error: sqlite3.OperationalError) -> Response:
    """Lock contention is transient: answer 503 so clients retry, not a 500 trace."""
    if not is_busy_error(error):
        raise error
    logger.warning("%s %s: risk database busy", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": DATABASE_BUSY_DETAIL})


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk0-risk", "version": "0.1.0"}
