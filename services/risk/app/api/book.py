import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.analysis.book import run_book
from app.analysis.service import AnalysisService
from app.api.analysis import UNAVAILABLE_DETAIL, get_analysis_service
from app.contracts.models import BookRequest, BookResponse
from app.domain.scenario import ScenarioSeverityError, validate_selection


logger = logging.getLogger("risk0.book")
router = APIRouter(prefix="/v1")


@router.post("/book", response_model=BookResponse)
def analyze_book(
    request: BookRequest,
    service: AnalysisService | None = Depends(get_analysis_service),
) -> BookResponse | JSONResponse:
    """Analyze every pocket of a book; a failing pocket is UNAVAILABLE, never a failed book."""
    # A malformed scenario is the caller's error for the whole book, not a pocket outcome.
    selections = [*(request.scenarios or ())]
    for pocket in request.pockets:
        selections.extend(pocket.scenarios or ())
    try:
        for selection in selections:
            validate_selection(selection)
    except ScenarioSeverityError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    if service is None:
        return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_DETAIL})
    try:
        return run_book(request, service)
    except Exception as error:  # never return a stack trace, URL, or provider body
        logger.error("book analysis failed: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_DETAIL})
