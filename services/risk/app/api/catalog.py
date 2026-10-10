import os
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends

from app.adapters.morpho import DEFAULT_BASE_URL, MorphoClient
from app.catalog.repository import CatalogRepository
from app.catalog.service import CatalogService, HydratedCatalogResponse
from app.infrastructure.cache import SQLiteCache


SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]

router = APIRouter(prefix="/v1")


@lru_cache(maxsize=1)
def get_catalog_service() -> CatalogService:
    catalog_path = Path(
        os.environ.get(
            "RISK0_CATALOG_PATH", REPOSITORY_ROOT / "data" / "catalog" / "ethereum.json"
        )
    )
    cache_path = Path(
        os.environ.get("RISK0_CACHE_PATH", SERVICE_ROOT / ".cache" / "risk0.sqlite3")
    )
    morpho = MorphoClient(
        os.environ.get("MORPHO_API_URL", DEFAULT_BASE_URL),
        cache=SQLiteCache(cache_path),
    )
    return CatalogService(CatalogRepository(catalog_path), morpho)


@router.get("/catalog")
def list_catalog(
    service: CatalogService = Depends(get_catalog_service),
) -> HydratedCatalogResponse:
    return service.list_hydrated()
