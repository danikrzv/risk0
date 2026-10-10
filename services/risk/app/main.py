from fastapi import FastAPI

app = FastAPI(title="Risk0 Risk Service", version="0.1.0")


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk0-risk", "version": "0.1.0"}
