from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal

from app.adapters.rpc_registry import RpcRegistry, RpcUnavailableError
from app.contracts.models import FreshnessStatus, MetricValue
from app.radar.collectors.common import build_snapshot, unknown_snapshot
from app.radar.contracts import RadarEntityType, StateSnapshot


CHAIN_FIELDS = (
    "head_block",
    "head_timestamp",
    "head_age_seconds",
    "finalized_block",
    "finality_lag_blocks",
)


class ChainStateCollector:
    """Reads chain head and finality through the RPC registry's ordered fallback."""

    def __init__(
        self,
        registry: RpcRegistry,
        *,
        chain_entity_ids: Mapping[int, str] | None = None,
    ) -> None:
        self._registry = registry
        self._entity_ids = dict(chain_entity_ids or {})

    def collect(self, chain_id: int, now: datetime) -> StateSnapshot:
        entity_id = self._entity_ids.get(chain_id, f"chain:{chain_id}:{chain_id}")
        source_identity = f"rpc:{chain_id}"
        try:
            head = self._registry.call(chain_id, "eth_getBlockByNumber", ["latest", False])
            head_block, head_timestamp = _block(head.result)
        except (RpcUnavailableError, KeyError, TypeError, ValueError):
            # No provider could answer: the chain is unknown, never healthy or zero.
            return unknown_snapshot(
                entity_id,
                RadarEntityType.CHAIN,
                now=now,
                source=source_identity,
                source_identity=source_identity,
                fields=CHAIN_FIELDS,
                extra_metrics={"rpc_available": False, "rpc_fallback_used": None},
            )
        try:
            finalized = self._registry.call(
                chain_id, "eth_getBlockByNumber", ["finalized", False]
            )
            finalized_block: int | None = _block(finalized.result)[0]
        except (RpcUnavailableError, KeyError, TypeError, ValueError):
            finalized_block = None
        age = int(now.astimezone(UTC).timestamp()) - head_timestamp
        metrics: dict[str, MetricValue] = {
            "rpc_available": True,
            "head_block": Decimal(head_block),
            "head_timestamp": Decimal(head_timestamp),
            "head_age_seconds": Decimal(max(age, 0)),
            "finalized_block": None if finalized_block is None else Decimal(finalized_block),
            "finality_lag_blocks": (
                None if finalized_block is None else Decimal(head_block - finalized_block)
            ),
            "rpc_fallback_used": bool(head.failures),
        }
        return build_snapshot(
            entity_id,
            RadarEntityType.CHAIN,
            observed_at=now,
            retrieved_at=now,
            source=head.provider,
            source_identity=source_identity,
            freshness=FreshnessStatus.FRESH,
            metrics=metrics,
            missing_fields=(name for name in CHAIN_FIELDS if metrics[name] is None),
        )

    def collect_all(
        self, chain_ids: Iterable[int], now: datetime
    ) -> tuple[StateSnapshot, ...]:
        return tuple(self.collect(chain_id, now) for chain_id in chain_ids)


def _block(block: object) -> tuple[int, int]:
    if not isinstance(block, Mapping):
        raise ValueError("invalid block")
    return int(str(block["number"]), 16), int(str(block["timestamp"]), 16)
