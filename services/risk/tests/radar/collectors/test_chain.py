import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.contracts.models import FreshnessStatus
from app.radar.contracts import RadarEntityType


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
HEAD_TIMESTAMP = int(NOW.timestamp()) - 24
SECRET = "chain-secret-999"


class ChainRpc:
    def __init__(self, *, finalized: bool = True, mode: str = "ok") -> None:
        self.finalized = finalized
        self.mode = mode

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_chainId":
            result = hex(1)
        elif method == "eth_getBlockByNumber" and params[0] == "latest":
            result = {"number": hex(26_163_000), "timestamp": hex(HEAD_TIMESTAMP)}
        elif method == "eth_getBlockByNumber" and params[0] == "finalized":
            if not self.finalized:
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "unsupported"}},
                )
            result = {"number": hex(26_162_936), "timestamp": hex(HEAD_TIMESTAMP - 768)}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def collector(rpc: ChainRpc, urls=None):
    from app.adapters.rpc_registry import RpcRegistry
    from app.radar.collectors import ChainStateCollector

    registry = RpcRegistry(
        urls if urls is not None else {1: (f"https://rpc.example.io/v2/{SECRET}",)},
        http_client=httpx.Client(transport=httpx.MockTransport(rpc.handler)),
    )
    return ChainStateCollector(registry, chain_entity_ids={1: "chain:1:ethereum"})


def test_healthy_chain_reports_head_age_and_finality_lag():
    snapshot = collector(ChainRpc()).collect(1, NOW)

    assert snapshot.entity_id == "chain:1:ethereum"
    assert snapshot.state_type is RadarEntityType.CHAIN
    assert snapshot.freshness is FreshnessStatus.FRESH
    assert snapshot.observed_at == NOW
    assert snapshot.retrieved_at == NOW
    assert snapshot.source == "https://rpc.example.io"
    assert snapshot.source_identity == "rpc:1"
    assert snapshot.metrics == {
        "rpc_available": True,
        "head_block": Decimal(26_163_000),
        "head_timestamp": Decimal(HEAD_TIMESTAMP),
        "head_age_seconds": Decimal(24),
        "finalized_block": Decimal(26_162_936),
        "finality_lag_blocks": Decimal(64),
        "rpc_fallback_used": False,
    }
    assert snapshot.missing_fields == ()


def test_unsupported_finality_is_reported_missing_not_zero():
    snapshot = collector(ChainRpc(finalized=False)).collect(1, NOW)

    assert snapshot.freshness is FreshnessStatus.FRESH
    assert snapshot.metrics["finalized_block"] is None
    assert snapshot.metrics["finality_lag_blocks"] is None
    assert snapshot.missing_fields == ("finalized_block", "finality_lag_blocks")


@pytest.mark.parametrize("urls", [None, {}])
def test_total_rpc_failure_produces_an_explicitly_unknown_snapshot(urls):
    snapshot = collector(ChainRpc(mode="down"), urls).collect(1, NOW)

    assert snapshot.freshness is FreshnessStatus.EXPIRED
    assert snapshot.metrics["rpc_available"] is False
    for field in ("head_block", "head_timestamp", "head_age_seconds", "finalized_block"):
        assert snapshot.metrics[field] is None
        assert field in snapshot.missing_fields
    assert SECRET not in snapshot.model_dump_json()


def test_collect_all_isolates_each_chain():
    from app.adapters.rpc_registry import RpcRegistry
    from app.radar.collectors import ChainStateCollector

    rpc = ChainRpc()
    registry = RpcRegistry(
        {1: ("https://rpc.example.io",)},
        http_client=httpx.Client(transport=httpx.MockTransport(rpc.handler)),
    )
    chains = ChainStateCollector(
        registry, chain_entity_ids={1: "chain:1:ethereum", 8453: "chain:8453:base"}
    )

    snapshots = chains.collect_all((1, 8453), NOW)

    assert [(s.entity_id, s.freshness) for s in snapshots] == [
        ("chain:1:ethereum", FreshnessStatus.FRESH),
        ("chain:8453:base", FreshnessStatus.EXPIRED),
    ]
