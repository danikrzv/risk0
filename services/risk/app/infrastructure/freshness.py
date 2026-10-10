from dataclasses import dataclass
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE_USABLE = "stale_usable"
    EXPIRED = "expired"

    @classmethod
    def from_times(
        cls,
        fetched_at: datetime,
        now: datetime,
        ttl: timedelta,
        maximum_age: timedelta,
    ) -> "Freshness":
        if ttl <= timedelta(0):
            raise ValueError("TTL must be positive")
        if maximum_age < ttl:
            raise ValueError("maximum age must be at least the TTL")
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        age = now.astimezone(UTC) - fetched_at.astimezone(UTC)
        if age < ttl:
            return cls.FRESH
        if age < maximum_age:
            return cls.STALE_USABLE
        return cls.EXPIRED


@dataclass(frozen=True, slots=True)
class DataConflict:
    field: str
    values: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataQuality:
    freshness: tuple[Freshness, ...]
    conflicts: tuple[DataConflict, ...]
    critical_missing: tuple[str, ...]

    @classmethod
    def from_sources(
        cls,
        sources: Iterable[tuple[str, Freshness]],
        critical_sources: Iterable[str],
        conflicts: Iterable[DataConflict],
    ) -> "DataQuality":
        source_values = tuple(sources)
        critical = tuple(dict.fromkeys(critical_sources))
        usable = frozenset(
            source
            for source, freshness in source_values
            if freshness is not Freshness.EXPIRED
        )
        # A critical source is missing when it is absent or only present as expired.
        missing = tuple(source for source in critical if source not in usable)
        return cls(
            freshness=tuple(freshness for _, freshness in source_values),
            conflicts=tuple(conflicts),
            critical_missing=missing,
        )
