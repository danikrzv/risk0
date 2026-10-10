from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.contracts.models import (
    DecimalValue,
    FreshnessStatus,
    MetricValue,
    NonEmptyString,
    PositionMode,
)


SIMILARITY_STORAGE_THRESHOLD = Decimal("0.33")
SIMILARITY_VISIBILITY_THRESHOLD = Decimal("0.60")
SIMILARITY_VISIBLE_LIMIT = 10


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


UtcDateTime = Annotated[datetime, AfterValidator(require_utc)]
RatioValue = Annotated[DecimalValue, Field(ge=0, le=1)]
NonNegativeDecimalValue = Annotated[DecimalValue, Field(ge=0)]


class RadarContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class RadarEntityType(StrEnum):
    CHAIN = "chain"
    PROTOCOL = "protocol"
    VAULT = "vault"
    MARKET = "market"
    ASSET = "asset"
    ORACLE = "oracle"
    IRM = "irm"
    POSITION = "position"


class IncidentSeverity(StrEnum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class RadarRelationshipType(StrEnum):
    ALLOCATED_TO = "allocated_to"
    LENDS = "lends"
    BORROWS = "borrows"
    USES_COLLATERAL = "uses_collateral"
    USES_ORACLE = "uses_oracle"
    USES_IRM = "uses_irm"
    BACKED_BY = "backed_by"
    DEPLOYED_ON = "deployed_on"
    EXITS_THROUGH = "exits_through"
    SIMILAR_TO = "similar_to"


class RadarEntity(RadarContractModel):
    entity_id: NonEmptyString
    entity_type: RadarEntityType
    chain_id: int = Field(gt=0)
    address_or_key: NonEmptyString
    label: NonEmptyString
    source: NonEmptyString
    catalog: bool = False
    visible: bool = True
    metadata: dict[str, MetricValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stable_id(self) -> "RadarEntity":
        expected = f"{self.entity_type.value}:{self.chain_id}:{self.address_or_key}"
        if self.entity_id != expected:
            raise ValueError(f"entity_id must equal {expected}")
        return self


class RadarRelationship(RadarContractModel):
    source_entity_id: NonEmptyString
    target_entity_id: NonEmptyString
    relationship_type: RadarRelationshipType
    weight: RatioValue | None = None
    metadata: dict[str, MetricValue] = Field(default_factory=dict)


class StateSnapshot(RadarContractModel):
    snapshot_id: NonEmptyString
    entity_id: NonEmptyString
    state_type: RadarEntityType
    observed_at: UtcDateTime
    retrieved_at: UtcDateTime
    source: NonEmptyString
    source_identity: NonEmptyString
    freshness: FreshnessStatus
    metrics: dict[str, MetricValue]
    missing_fields: tuple[NonEmptyString, ...] = ()


class HealthState(RadarContractModel):
    entity_id: NonEmptyString
    status: HealthStatus
    score: RatioValue | None
    reasons: tuple[NonEmptyString, ...]
    missing_fields: tuple[NonEmptyString, ...]
    evaluated_at: UtcDateTime
    rule_version: NonEmptyString


class SimilarityPolicy(RadarContractModel):
    storage_threshold: Literal["0.33"] = "0.33"
    visibility_threshold: Literal["0.60"] = "0.60"
    visible_limit: Literal[10] = 10


class SimilarityRecord(RadarContractModel):
    catalog_entity_id: NonEmptyString
    candidate_entity_id: NonEmptyString
    score: RatioValue
    components: dict[str, RatioValue]
    missing_dimensions: tuple[NonEmptyString, ...] = ()
    model_version: Literal["vault-similarity-v1"] = "vault-similarity-v1"
    stored: bool
    normally_visible: bool
    computed_at: UtcDateTime


class Incident(RadarContractModel):
    incident_id: NonEmptyString
    source_entity_id: NonEmptyString
    severity: IncidentSeverity
    status: IncidentStatus
    trigger_metrics: dict[str, MetricValue]
    rule_version: NonEmptyString
    first_observed_at: UtcDateTime
    last_observed_at: UtcDateTime
    resolved_at: UtcDateTime | None = None
    reasons: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_resolution(self) -> "Incident":
        if self.status is IncidentStatus.OPEN and self.resolved_at is not None:
            raise ValueError("open incidents cannot have resolved_at")
        if self.status is IncidentStatus.RESOLVED and self.resolved_at is None:
            raise ValueError("resolved incidents require resolved_at")
        return self


class IncidentImpact(RadarContractModel):
    incident_id: NonEmptyString
    target_entity_id: NonEmptyString
    path: tuple[NonEmptyString, ...] = Field(min_length=1)
    distance: int = Field(ge=0)
    confidence: RatioValue
    estimated_affected_amount: NonNegativeDecimalValue | None
    reasons: tuple[NonEmptyString, ...]
    revealed: bool
    alternate_path_count: int = Field(default=0, ge=0)


class Notification(RadarContractModel):
    notification_id: NonEmptyString
    incident_id: NonEmptyString
    severity: IncidentSeverity
    title: NonEmptyString
    message: NonEmptyString
    created_at: UtcDateTime
    read_at: UtcDateTime | None = None


class ExposureFields(RadarContractModel):
    entity_id: NonEmptyString
    mode: PositionMode
    amount: NonNegativeDecimalValue
    numeraire: NonEmptyString
    collateral_amount: NonNegativeDecimalValue | None = None
    debt_amount: NonNegativeDecimalValue | None = None
    ltv: RatioValue | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_mode_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        borrower_fields = ("collateral_amount", "debt_amount", "ltv")
        if value.get("mode") == PositionMode.LENDER and any(
            value.get(field) is not None for field in borrower_fields
        ):
            raise ValueError("lender exposures forbid borrower fields")
        if value.get("mode") == PositionMode.BORROWER and any(
            value.get(field) is None for field in borrower_fields
        ):
            raise ValueError("borrower exposures require collateral, debt, and LTV")
        return value


class ManualExposure(ExposureFields):
    exposure_id: NonEmptyString
    created_at: UtcDateTime


class SchedulerRun(RadarContractModel):
    run_id: NonEmptyString
    loop: Literal["fast", "slow"]
    status: Literal["running", "completed", "partial", "failed", "skipped"]
    started_at: UtcDateTime
    finished_at: UtcDateTime | None
    processed_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    message: str


class RadarGraphResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    entities: tuple[RadarEntity, ...]
    relationships: tuple[RadarRelationship, ...]
    similarities: tuple[SimilarityRecord, ...]
    active_incidents: tuple[Incident, ...]
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_entity_ids(self) -> "RadarGraphResponse":
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("graph entity IDs must be unique")
        return self


class IncidentGraphResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    incident: Incident
    entities: tuple[RadarEntity, ...]
    relationships: tuple[RadarRelationship, ...]
    similarities: tuple[SimilarityRecord, ...]
    exposures: tuple[ManualExposure, ...]
    impacts: tuple[IncidentImpact, ...]
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_entity_ids(self) -> "IncidentGraphResponse":
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("graph entity IDs must be unique")
        return self


class EntityHistoryResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    entity: RadarEntity
    snapshots: tuple[StateSnapshot, ...]
    health_states: tuple[HealthState, ...]
    next_cursor: str | None
    generated_at: UtcDateTime


class SimilarityResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    vault_id: NonEmptyString
    policy: SimilarityPolicy = SimilarityPolicy()
    similarities: tuple[SimilarityRecord, ...]
    generated_at: UtcDateTime


class NotificationResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    notifications: tuple[Notification, ...]
    next_cursor: str | None
    generated_at: UtcDateTime


class ExposureCreateRequest(ExposureFields):
    pass


class ExposureImpactResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    exposure: ManualExposure
    impacts: tuple[IncidentImpact, ...]
    generated_at: UtcDateTime
