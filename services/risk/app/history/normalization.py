import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from app.config import CachePolicy
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


HOUR = timedelta(hours=1)
HISTORY_POLICY = CachePolicy(ttl=timedelta(days=1), retention=timedelta(days=365))
HISTORY_NAMESPACE = "normalized_history"


@dataclass(frozen=True, slots=True)
class Observation:
    timestamp: datetime
    value: Decimal
    block_number: int
    log_index: int
    transaction_hash: str = ""
    is_forward_filled: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "observation timestamp")
        if not isinstance(self.value, Decimal):
            raise TypeError("observation value must be Decimal")


@dataclass(frozen=True, slots=True)
class DataGap:
    start: datetime
    end: datetime
    missing_observations: int


@dataclass(frozen=True, slots=True)
class NormalizedSeries:
    observations: tuple[Observation, ...]
    gaps: tuple[DataGap, ...]
    start: datetime
    end: datetime
    source: str
    retrieved_at: datetime
    freshness: Freshness

    def __post_init__(self) -> None:
        _require_aware(self.start, "series start")
        _require_aware(self.end, "series end")
        _require_aware(self.retrieved_at, "series retrieval time")
        if self.end < self.start:
            raise ValueError("series end must not precede start")


@dataclass(frozen=True, slots=True)
class CachedNormalizedSeries:
    series: NormalizedSeries
    freshness: Freshness


def normalize_hourly(
    observations: Sequence[Observation],
    maximum_gap: timedelta,
) -> NormalizedSeries:
    if not observations:
        raise ValueError("at least one observation is required")
    if maximum_gap < timedelta(0):
        raise ValueError("maximum_gap must not be negative")

    by_block: dict[int, Observation] = {}
    for observation in observations:
        existing = by_block.get(observation.block_number)
        if existing is None or _order(observation) > _order(existing):
            by_block[observation.block_number] = observation

    by_hour: dict[datetime, Observation] = {}
    for observation in by_block.values():
        hour = observation.timestamp.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0
        )
        normalized = replace(observation, timestamp=hour)
        existing = by_hour.get(hour)
        if existing is None or _order(normalized) > _order(existing):
            by_hour[hour] = normalized

    actual = tuple(by_hour[hour] for hour in sorted(by_hour))
    normalized_observations: list[Observation] = [actual[0]]
    gaps: list[DataGap] = []
    maximum_missing = int(maximum_gap / HOUR)
    for previous, current in zip(actual, actual[1:], strict=False):
        missing = int((current.timestamp - previous.timestamp) / HOUR) - 1
        if missing <= 0:
            normalized_observations.append(current)
            continue
        if missing <= maximum_missing:
            for offset in range(1, missing + 1):
                normalized_observations.append(
                    replace(
                        previous,
                        timestamp=previous.timestamp + offset * HOUR,
                        is_forward_filled=True,
                    )
                )
        else:
            gaps.append(
                DataGap(
                    start=previous.timestamp + HOUR,
                    end=current.timestamp - HOUR,
                    missing_observations=missing,
                )
            )
        normalized_observations.append(current)

    return NormalizedSeries(
        observations=tuple(normalized_observations),
        gaps=tuple(gaps),
        start=actual[0].timestamp,
        end=actual[-1].timestamp,
        source="normalized:hourly",
        retrieved_at=max(item.timestamp for item in actual),
        freshness=Freshness.FRESH,
    )


class NormalizedHistoryCache:
    def __init__(self, cache: SQLiteCache) -> None:
        self._cache = cache

    def get(self, feed: str, now: datetime) -> CachedNormalizedSeries | None:
        cached = self._cache.get(HISTORY_NAMESPACE, feed, now)
        if cached is None:
            return None
        return CachedNormalizedSeries(
            series=_deserialize_series(cached.payload),
            freshness=cached.freshness,
        )

    def set(self, feed: str, series: NormalizedSeries, fetched_at: datetime) -> None:
        self._cache.set(
            HISTORY_NAMESPACE,
            feed,
            _serialize_series(series),
            fetched_at,
            HISTORY_POLICY,
        )


def _serialize_series(series: NormalizedSeries) -> bytes:
    payload = {
        "observations": [
            {
                "timestamp": item.timestamp.isoformat(),
                "value": str(item.value),
                "block_number": item.block_number,
                "log_index": item.log_index,
                "transaction_hash": item.transaction_hash,
                "is_forward_filled": item.is_forward_filled,
            }
            for item in series.observations
        ],
        "gaps": [
            {
                "start": gap.start.isoformat(),
                "end": gap.end.isoformat(),
                "missing_observations": gap.missing_observations,
            }
            for gap in series.gaps
        ],
        "start": series.start.isoformat(),
        "end": series.end.isoformat(),
        "source": series.source,
        "retrieved_at": series.retrieved_at.isoformat(),
        "freshness": series.freshness.value,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _deserialize_series(payload: bytes) -> NormalizedSeries:
    value: dict[str, Any] = json.loads(payload)
    return NormalizedSeries(
        observations=tuple(
            Observation(
                timestamp=datetime.fromisoformat(item["timestamp"]),
                value=Decimal(item["value"]),
                block_number=item["block_number"],
                log_index=item["log_index"],
                transaction_hash=item["transaction_hash"],
                is_forward_filled=item["is_forward_filled"],
            )
            for item in value["observations"]
        ),
        gaps=tuple(
            DataGap(
                start=datetime.fromisoformat(item["start"]),
                end=datetime.fromisoformat(item["end"]),
                missing_observations=item["missing_observations"],
            )
            for item in value["gaps"]
        ),
        start=datetime.fromisoformat(value["start"]),
        end=datetime.fromisoformat(value["end"]),
        source=value["source"],
        retrieved_at=datetime.fromisoformat(value["retrieved_at"]),
        freshness=Freshness(value["freshness"]),
    )


def _order(observation: Observation) -> tuple[datetime, int, int, str, Decimal]:
    return (
        observation.timestamp.astimezone(UTC),
        observation.block_number,
        observation.log_index,
        observation.transaction_hash,
        observation.value,
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
