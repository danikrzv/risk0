from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class OracleFeedObservation:
    entity_id: str
    represented_pair: str
    price: Decimal
    observed_at: datetime
    heartbeat_seconds: int
    source_family: str
    derived: bool = False

    @property
    def hard_failure(self) -> bool:
        return self.price <= 0

    def __post_init__(self) -> None:
        if not self.entity_id or not self.represented_pair or not self.source_family:
            raise ValueError("oracle observation identifiers must not be empty")
        if not isinstance(self.price, Decimal) or not self.price.is_finite():
            raise ValueError("oracle price must be a finite decimal")
        # A non-positive price is a hard oracle failure, not an invalid observation:
        # consensus classifies it as CRITICAL instead of rejecting the reading.
        if self.heartbeat_seconds <= 0:
            raise ValueError("oracle heartbeat must be positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("oracle timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VaultProfile:
    entity_id: str
    market_allocations: Mapping[str, Decimal]
    loan_assets: frozenset[str]
    collateral_assets: frozenset[str]
    oracle_groups: frozenset[str]
    irms: frozenset[str]
    lltvs: tuple[Decimal, ...]
    chain_id: int
    curator: str | None

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("vault entity_id must not be empty")
        if self.chain_id <= 0:
            raise ValueError("vault chain_id must be positive")
        allocations = dict(self.market_allocations)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in allocations.values()):
            raise ValueError("market allocations must be finite decimals")
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in self.lltvs):
            raise ValueError("LLTV values must be finite decimals")
        if any(not key or value < 0 for key, value in allocations.items()):
            raise ValueError("market allocations require non-empty IDs and non-negative weights")
        if any(value < 0 or value > 1 for value in self.lltvs):
            raise ValueError("LLTV values must be ratios")
        object.__setattr__(self, "market_allocations", MappingProxyType(allocations))
