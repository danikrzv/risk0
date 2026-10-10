# Risk service image. Build from the repository root (docker-compose.yml does):
#   docker build -f services/risk/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/risk0-venv \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

# Dependencies first, for layer caching.
COPY services/risk/pyproject.toml services/risk/uv.lock services/risk/
RUN uv sync --project services/risk --frozen --no-dev --no-install-project

# Service code, its recorded fixtures (used only by RISK0_DATA_MODE=fixture), and
# the version-controlled catalog, feeds, scenarios, and evidence.
COPY services/risk services/risk
COPY data data
RUN uv sync --project services/risk --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "--project", "services/risk", "uvicorn", "app.main:app", "--app-dir", "services/risk", "--host", "0.0.0.0", "--port", "8000"]
