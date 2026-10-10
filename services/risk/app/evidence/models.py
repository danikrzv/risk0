from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.contracts.models import ConfidenceLevel
from app.infrastructure.freshness import DataQuality, Freshness


class EvidenceFormatError(ValueError):
    """Raised when an evidence payload does not match its documented shape."""


class EvidenceNotFoundError(LookupError):
    """Raised by fetchers when a source has no document at a URL."""


class EvidenceSource(StrEnum):
    DEFISCAN = "defiscan"
    DEFISAFETY = "defisafety"


class EvidenceStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class EvidenceDimension(StrEnum):
    AUDITS_TESTING = "audits_testing_verification"
    CONTROLS_GOVERNANCE = "upgradeability_admin_governance"
    TRACK_RECORD = "track_record_incidents"
    DEPENDENCIES = "oracle_external_dependencies"
    MONITORING_BOUNTY = "monitoring_bug_bounty"


class DimensionStatus(StrEnum):
    OBSERVED = "observed"
    STALE = "stale"
    CONFLICTING = "conflicting"
    MISSING = "missing"


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceFormatError(f"invalid timestamp {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceFormatError(f"timestamp {value!r} must include a timezone")
    return parsed


def _require_aware(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    """A raw source field preserved verbatim for the factor breakdown."""

    category: str
    label: str
    value: str
    observed_at: datetime | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceFactor:
    """One source's normalized 0-100 value for one scoring dimension."""

    dimension: EvidenceDimension
    value: Decimal
    source: EvidenceSource
    source_url: str
    observed_at: datetime | None
    rule: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("evidence factor values must be Decimal")
        if not self.value.is_finite() or not Decimal(0) <= self.value <= Decimal(100):
            raise ValueError("evidence factor values must be within 0-100")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    protocol: str
    source: EvidenceSource
    status: EvidenceStatus
    source_url: str
    methodology_url: str
    retrieved_at: datetime
    source_updated_at: datetime | None
    factors: tuple[EvidenceFactor, ...]
    observations: tuple[EvidenceObservation, ...]
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.retrieved_at, "retrieved_at")
        _require_aware(self.source_updated_at, "source_updated_at")
        if self.status is EvidenceStatus.UNAVAILABLE:
            if self.factors:
                raise ValueError("unavailable evidence cannot carry factors")
            if not self.unavailable_reason:
                raise ValueError("unavailable evidence requires a reason")
        if any(factor.source is not self.source for factor in self.factors):
            raise ValueError("factors must come from the bundle source")

    @classmethod
    def unavailable(
        cls,
        *,
        protocol: str,
        source: EvidenceSource,
        source_url: str,
        methodology_url: str,
        retrieved_at: datetime,
        reason: str,
    ) -> "EvidenceBundle":
        return cls(
            protocol=protocol,
            source=source,
            status=EvidenceStatus.UNAVAILABLE,
            source_url=source_url,
            methodology_url=methodology_url,
            retrieved_at=retrieved_at,
            source_updated_at=None,
            factors=(),
            observations=(),
            unavailable_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class CombinedEvidence:
    protocol: str
    bundles: tuple[EvidenceBundle, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        if any(bundle.protocol != self.protocol for bundle in self.bundles):
            raise ValueError("all evidence bundles must describe the same protocol")


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    source: EvidenceSource
    status: EvidenceStatus
    source_url: str
    methodology_url: str
    retrieved_at: datetime
    source_updated_at: datetime | None
    freshness: Freshness
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class DimensionScore:
    dimension: EvidenceDimension
    weight: Decimal
    status: DimensionStatus
    value: Decimal | None
    contribution: Decimal
    selected_source: EvidenceSource | None
    candidates: tuple[EvidenceFactor, ...]


@dataclass(frozen=True, slots=True)
class EvidenceScore:
    protocol: str
    model_version: str
    score: Decimal | None
    confidence: ConfidenceLevel
    dimensions: tuple[DimensionScore, ...]
    quality: DataQuality
    attributions: tuple[SourceAttribution, ...]
    evaluated_at: datetime
