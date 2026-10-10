from dataclasses import dataclass
from datetime import datetime, timedelta
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

        age = now - fetched_at
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
