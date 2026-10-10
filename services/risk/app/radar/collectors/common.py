from collections.abc import Iterable, Mapping
from datetime import datetime

from app.contracts.models import FreshnessStatus, MetricValue
from app.radar.contracts import RadarEntityType, StateSnapshot


def build_snapshot(
    entity_id: str,
    state_type: RadarEntityType,
    *,
    observed_at: datetime,
    retrieved_at: datetime,
    source: str,
    source_identity: str,
    freshness: FreshnessStatus,
    metrics: Mapping[str, MetricValue],
    missing_fields: Iterable[str] = (),
) -> StateSnapshot:
    return StateSnapshot(
        # Deterministic: re-collecting the same observation yields the same ID.
        snapshot_id=f"{entity_id}|{source_identity}|{observed_at.isoformat()}",
        entity_id=entity_id,
        state_type=state_type,
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        source=source,
        source_identity=source_identity,
        freshness=freshness,
        metrics=dict(metrics),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
    )


def unknown_snapshot(
    entity_id: str,
    state_type: RadarEntityType,
    *,
    now: datetime,
    source: str,
    source_identity: str,
    fields: Iterable[str],
    extra_metrics: Mapping[str, MetricValue] | None = None,
) -> StateSnapshot:
    """An explicit no-data observation: every metric is null and listed as missing."""
    names = tuple(fields)
    return build_snapshot(
        entity_id,
        state_type,
        observed_at=now,
        retrieved_at=now,
        source=source,
        source_identity=source_identity,
        freshness=FreshnessStatus.EXPIRED,
        metrics={**{name: None for name in names}, **(extra_metrics or {})},
        missing_fields=("state", *names),
    )
