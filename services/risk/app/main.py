from fastapi import FastAPI

from app.api.analysis import router as analysis_router
from app.api.catalog import router as catalog_router

app = FastAPI(title="Risk0 Risk Service", version="0.1.0")
app.include_router(catalog_router)
app.include_router(analysis_router)


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk0-risk", "version": "0.1.0"}
