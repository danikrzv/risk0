from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class CachePolicy:
    ttl: timedelta
    retention: timedelta

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0):
            raise ValueError("cache TTL must be positive")
        if self.retention < self.ttl:
            raise ValueError("cache retention must be at least the TTL")
