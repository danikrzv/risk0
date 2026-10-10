from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.analysis import router as analysis_router
from app.api.catalog import router as catalog_router
from app.fixture_mode import data_mode


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Fail fast on a mistyped RISK0_DATA_MODE rather than serving a healthy
    # /v1/health while every catalog and analysis request fails.
    data_mode()
    yield


app = FastAPI(title="Risk0 Risk Service", version="0.1.0", lifespan=lifespan)
app.include_router(catalog_router)
app.include_router(analysis_router)


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk0-risk", "version": "0.1.0"}
