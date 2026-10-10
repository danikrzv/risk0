import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.fixtures.morpho.rockawayx import ROCKAWAYX_VAULTS, graphql_key


MORPHO_FIXTURES = Path(__file__).parent / "fixtures" / "morpho"
MORPHO_BASE_URL = "https://api.morpho.org"


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeMorphoApi:
    """Serves recorded, sanitized Morpho REST and GraphQL responses."""

    def __init__(self) -> None:
        self.rest: dict[str, object] = {}
        self.graphql: dict[str, object] = {}
        for name in ("market-state.json", "vault-state.json", "vault-allocations.json"):
            recorded = json.loads((MORPHO_FIXTURES / name).read_text())
            self.rest.update(recorded.get("rest", {}))
            self.graphql.update(recorded.get("graphql", {}))
        for vault in ROCKAWAYX_VAULTS:
            self.rest.update(vault.rest())
            self.graphql.update(vault.graphql())
        self.mode = "ok"
        self.failing_paths: set[str] = set()
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("simulated outage", request=request)
        if self.mode == "error" or request.url.path in self.failing_paths:
            return httpx.Response(503, json={"message": "unavailable"})
        if request.method == "POST" and request.url.path == "/graphql":
            variables = json.loads(request.content)["variables"]
            body = self.graphql.get(self._graphql_key(request.content, variables))
        else:
            body = self.rest.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"message": "not found", "statusCode": 404})
        return httpx.Response(200, json=body)

    @staticmethod
    def _graphql_key(content: bytes, variables: dict[str, object]) -> str:
        query = json.loads(content)["query"]
        chain_id = variables["chainId"]
        if "marketById" in query:
            return graphql_key("market", variables["marketId"], chain_id)
        if "vaultV2ByAddress" in query:
            return graphql_key("vault_v2", variables["address"], chain_id)
        return graphql_key("vault_v1", variables["address"], chain_id)

    def graphql_requests(self) -> list[dict[str, object]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.method == "POST" and request.url.path == "/graphql"
        ]

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def market_graphql(self, market_id: str) -> dict:
        return self.graphql[f"market:{market_id}"]["data"]["marketById"]

    def vault_graphql(self, version: str, address: str, chain_id: int = 1) -> dict:
        root = "vaultV2ByAddress" if version == "v2" else "vaultByAddress"
        key = graphql_key(f"vault_{version}", address, chain_id)
        return self.graphql[key]["data"][root]

    def remove_market(self, market_id: str) -> None:
        for path in list(self.rest):
            if market_id in path:
                del self.rest[path]
        del self.graphql[f"market:{market_id}"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 10, 9, 15, tzinfo=UTC))


@pytest.fixture
def morpho_api() -> FakeMorphoApi:
    return FakeMorphoApi()


@pytest.fixture
def cache(tmp_path):
    from app.infrastructure.cache import SQLiteCache

    return SQLiteCache(tmp_path / "cache.sqlite3")


@pytest.fixture
def morpho_client(morpho_api, cache, clock):
    from app.adapters.morpho import MorphoClient

    return MorphoClient(
        MORPHO_BASE_URL,
        http_client=morpho_api.http_client(),
        cache=cache,
        clock=clock,
        sleep=lambda _: None,
    )
