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


def build_default_analysis_service() -> AnalysisService:
    """Live wiring. Without ETHEREUM_RPC_URL the Ethereum-backed providers are absent,
    so quotes, gas, and history become UNAVAILABLE instead of failing the request."""
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

    quotes = gas = prices = history = None
    if settings is not None:
        from app.adapters.ethereum import EthereumClient
        from app.adapters.uniswap import UniswapClient
        from app.history.estimator import HistoricalProbabilityService
        from app.history.normalization import NormalizedHistoryCache

        ethereum = EthereumClient(settings)
        quotes = UniswapQuotes(UniswapClient(settings, cache=cache), UniswapClient(settings))
        gas = EthereumGas(ethereum)
        prices = ChainlinkPrices(ethereum)
        history = HistoricalProbabilityService(ethereum, NormalizedHistoryCache(cache))

    return AnalysisService(
        CatalogRepository(catalog_path),
        morpho,
        probabilities=HybridProbabilityProvider(history, evidence),
        quotes=quotes,
        gas=gas,
        prices=prices,
    )


@lru_cache(maxsize=1)
def get_analysis_service() -> AnalysisService:
    return build_default_analysis_service()


@router.post("/analysis", response_model=AnalysisResponse)
def analyze(
    request: AnalysisRequest,
    service: AnalysisService = Depends(get_analysis_service),
) -> AnalysisResponse | JSONResponse:
    try:
        return service.analyze(request)
    except AnalysisError as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.public_message})
    except Exception as error:  # never return a stack trace, URL, or provider body
        logger.error("analysis failed: %s", type(error).__name__)
        return JSONResponse(
            status_code=503, content={"detail": "The risk analysis is temporarily unavailable."}
        )
