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
    source: str = ""

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
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> NormalizedSeries:
    if not observations:
        raise ValueError("at least one observation is required")
    if maximum_gap < timedelta(0):
        raise ValueError("maximum_gap must not be negative")

    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")

    by_identity: dict[tuple[int, str, int], Observation] = {}
    for observation in observations:
        identity = (
            observation.block_number,
            observation.transaction_hash,
            observation.log_index,
        )
        existing = by_identity.get(identity)
        if existing is None or _order(observation) > _order(existing):
            by_identity[identity] = observation

    if start is None or end is None:
        normalized_start = min(item.timestamp for item in by_identity.values())
        normalized_end = max(item.timestamp for item in by_identity.values())
        normalized_start = _floor_hour(normalized_start)
        normalized_end = _floor_hour(normalized_end)
    else:
        normalized_start, normalized_end = normalize_period(start, end)

    by_hour: dict[datetime, Observation] = {}
    for observation in by_identity.values():
        hour = _floor_hour(observation.timestamp)
        if hour < normalized_start or hour > normalized_end:
            continue
        normalized = replace(observation, timestamp=hour)
        existing = by_hour.get(hour)
        if existing is None or _order(normalized) > _order(existing):
            by_hour[hour] = normalized

    actual = tuple(by_hour[hour] for hour in sorted(by_hour))
    if not actual:
        raise ValueError("no observations fall inside the requested period")
    normalized_observations: list[Observation] = [actual[0]]
    gaps: list[DataGap] = []
    if actual[0].timestamp > normalized_start:
        gaps.append(_gap(normalized_start, actual[0].timestamp - HOUR))
    for previous, current in zip(actual, actual[1:], strict=False):
        missing = int((current.timestamp - previous.timestamp) / HOUR) - 1
        if missing <= 0:
            normalized_observations.append(current)
            continue
        if current.timestamp - previous.timestamp <= maximum_gap:
            for offset in range(1, missing + 1):
                normalized_observations.append(
                    replace(
                        previous,
                        timestamp=previous.timestamp + offset * HOUR,
                        is_forward_filled=True,
                    )
                )
        else:
            gaps.append(_gap(previous.timestamp + HOUR, current.timestamp - HOUR))
        normalized_observations.append(current)

    if actual[-1].timestamp < normalized_end:
        gaps.append(_gap(actual[-1].timestamp + HOUR, normalized_end))

    sources = {item.source for item in actual if item.source}
    source = sources.pop() if len(sources) == 1 else "normalized:hourly"

    return NormalizedSeries(
        observations=tuple(normalized_observations),
        gaps=tuple(gaps),
        start=normalized_start,
        end=normalized_end,
        source=source,
        retrieved_at=max(item.timestamp for item in actual),
        freshness=Freshness.FRESH,
    )


class NormalizedHistoryCache:
    def __init__(self, cache: SQLiteCache) -> None:
        self._cache = cache

    def get(
        self,
        feed: str,
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> CachedNormalizedSeries | None:
        normalized_start, normalized_end = normalize_period(start, end)
        cached = self._cache.get(
            HISTORY_NAMESPACE,
            _cache_key(feed, normalized_start, normalized_end),
            now,
        )
        if cached is None:
            return None
        series = _deserialize_series(cached.payload)
        if series.start != normalized_start or series.end != normalized_end:
            return None
        return CachedNormalizedSeries(
            series=series,
            freshness=cached.freshness,
        )

    def set(self, feed: str, series: NormalizedSeries, fetched_at: datetime) -> None:
        self._cache.set(
            HISTORY_NAMESPACE,
            _cache_key(feed, series.start, series.end),
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
                "source": item.source,
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
                source=item.get("source", value["source"]),
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


def normalize_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    _require_aware(start, "period start")
    _require_aware(end, "period end")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    normalized_start = _floor_hour(start_utc)
    if start_utc != normalized_start:
        normalized_start += HOUR
    normalized_end = _floor_hour(end_utc)
    if normalized_end < normalized_start:
        raise ValueError("period contains no complete UTC hour")
    return normalized_start, normalized_end


def _floor_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _gap(start: datetime, end: datetime) -> DataGap:
    return DataGap(
        start=start,
        end=end,
        missing_observations=int((end - start) / HOUR) + 1,
    )


def _cache_key(feed: str, start: datetime, end: datetime) -> str:
    return f"{feed}|{start.isoformat()}|{end.isoformat()}"
