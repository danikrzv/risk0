import logging
import os
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.adapters.morpho import DEFAULT_BASE_URL, MorphoClient
from app.analysis.service import (
    AnalysisError,
    AnalysisService,
    ChainlinkLatest,
    ChainlinkOracleHealth,
    ChainlinkPrices,
    EthereumGas,
    FileAndDefiScanEvidence,
    HybridProbabilityProvider,
    UniswapQuotes,
)
from app.catalog.repository import CatalogRepository
from app.contracts.models import AnalysisRequest, AnalysisResponse
from app.infrastructure.cache import SQLiteCache


SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]

logger = logging.getLogger("risk0.analysis")
router = APIRouter(prefix="/v1")


UNAVAILABLE_DETAIL = "The risk analysis is temporarily unavailable."


def build_default_analysis_service() -> AnalysisService:
    """Live wiring. Without ETHEREUM_RPC_URL the Ethereum-backed providers are absent:
    quotes, gas, and history become UNAVAILABLE, and with no live oracle provider
    every graph oracle is missing critical data, so the recommendation is
    UNAVAILABLE (never STAY) instead of failing the request."""
    from app.adapters.defiscan import DefiScanClient
    from app.config import Settings

    catalog_path = Path(
        os.environ.get(
            "RISK0_CATALOG_PATH", REPOSITORY_ROOT / "data" / "catalog" / "ethereum.json"
        )
    )
    cache_path = Path(
        os.environ.get("RISK0_CACHE_PATH", SERVICE_ROOT / ".cache" / "risk0.sqlite3")
    )
    evidence_path = REPOSITORY_ROOT / "data" / "evidence" / "defisafety.ethereum.json"
    cache = SQLiteCache(cache_path)
    morpho = MorphoClient(os.environ.get("MORPHO_API_URL", DEFAULT_BASE_URL), cache=cache)
    evidence = FileAndDefiScanEvidence(evidence_path, DefiScanClient())

    try:
        settings = Settings.from_environment()
    except ValueError:
        logger.warning("ETHEREUM_RPC_URL is not configured; Ethereum-backed data is unavailable")
        settings = None

    quotes = gas = prices = history = oracles = None
    if settings is not None:
        from app.adapters.ethereum import EthereumClient
        from app.adapters.uniswap import UniswapClient
        from app.history.estimator import HistoricalProbabilityService
        from app.history.normalization import NormalizedHistoryCache

        ethereum = EthereumClient(settings)
        quotes = UniswapQuotes(UniswapClient(settings, cache=cache), UniswapClient(settings))
        gas = EthereumGas(ethereum)
        chainlink = ChainlinkLatest(ethereum)
        prices = ChainlinkPrices(chainlink)
        oracles = ChainlinkOracleHealth(chainlink)
        history = HistoricalProbabilityService(ethereum, NormalizedHistoryCache(cache))

    return AnalysisService(
        CatalogRepository(catalog_path),
        morpho,
        probabilities=HybridProbabilityProvider(history, evidence),
        quotes=quotes,
        gas=gas,
        prices=prices,
        oracles=oracles,
    )


@lru_cache(maxsize=1)
def get_default_service() -> AnalysisService:
    return build_default_analysis_service()


def get_analysis_service() -> AnalysisService | None:
    """The shared service, or None when it cannot be built (bad catalog path,
    unwritable cache directory). The failure is not cached, so a repaired
    environment recovers on the next request without a restart; only the
    exception type is logged."""
    try:
        return get_default_service()
    except Exception as error:
        logger.error("analysis service construction failed: %s", type(error).__name__)
        return None


@router.post("/analysis", response_model=AnalysisResponse)
def analyze(
    request: AnalysisRequest,
    service: AnalysisService | None = Depends(get_analysis_service),
) -> AnalysisResponse | JSONResponse:
    if service is None:
        return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_DETAIL})
    try:
        return service.analyze(request)
    except AnalysisError as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.public_message})
    except Exception as error:  # never return a stack trace, URL, or provider body
        logger.error("analysis failed: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_DETAIL})
