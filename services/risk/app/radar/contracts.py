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


class HealthDerivationStage(StrEnum):
    DEPENDENCIES = "dependencies"
    FRESHNESS = "freshness"
    METRICS = "metrics"
    HYSTERESIS = "hysteresis"


class HealthCheckOutcome(StrEnum):
    BREACH = "breach"
    WITHIN = "within"
    MISSING = "missing"
    INFO = "info"


class HealthCheck(RadarContractModel):
    """One rule as evaluated: ``metric operator threshold`` is the breach condition."""

    stage: HealthDerivationStage
    rule: NonEmptyString
    metric: NonEmptyString | None = None
    observed: DecimalValue | None = None
    # Non-numeric observation (freshness, explicit status, missing field names).
    note: NonEmptyString | None = None
    operator: Literal[">", ">=", "<", "<="] | None = None
    threshold: DecimalValue | None = None
    outcome: HealthCheckOutcome
    # The status this rule produces when it breaches; null for informational rows.
    leads_to: HealthStatus | None = None
    # True when this rule decided the raw status, or when hysteresis held it.
    decisive: bool = False


class HealthDependencyContribution(RadarContractModel):
    entity_id: NonEmptyString
    status: HealthStatus
    # "weighted" dependencies count by allocation share; "full" ones by status alone.
    counted: Literal["weighted", "full"]
    weight: DecimalValue | None = None

    @model_validator(mode="after")
    def validate_weight(self) -> "HealthDependencyContribution":
        if (self.counted == "weighted") != (self.weight is not None):
            raise ValueError("only weighted dependencies carry a weight")
        return self


class HealthStatusTotals(RadarContractModel):
    HEALTHY: DecimalValue
    WARNING: DecimalValue
    CRITICAL: DecimalValue
    UNKNOWN: DecimalValue


class HealthDependencyAggregation(RadarContractModel):
    dependencies: tuple[HealthDependencyContribution, ...]
    # Allocation share held in dependencies of each status (weighted ones only).
    weighted_totals: HealthStatusTotals
    material_allocation: RatioValue
    status: HealthStatus
    reason: NonEmptyString


class HealthHysteresis(RadarContractModel):
    previous_status: HealthStatus | None
    raw_status: HealthStatus
    rule: Literal[
        "immediate", "already_abnormal", "warning_confirmation", "recovery_confirmation", "none"
    ]
    consecutive_abnormal: int = Field(ge=0)
    consecutive_normal: int = Field(ge=0)
    # Consecutive cycles counted including this evaluation, for confirmation rules.
    counter: int | None = Field(default=None, ge=1)
    cycles_required: int | None = Field(default=None, ge=1)
    held: bool
    final_status: HealthStatus


class HealthScoreMapping(RadarContractModel):
    HEALTHY: RatioValue = Decimal("1")
    WARNING: RatioValue = Decimal("0.5")
    CRITICAL: RatioValue = Decimal("0")
    UNKNOWN: None = None


class HealthDerivation(RadarContractModel):
    """How a health state was computed, in evaluation order."""

    entity_type: RadarEntityType
    freshness: FreshnessStatus
    checks: tuple[HealthCheck, ...]
    dependencies: HealthDependencyAggregation | None
    raw_status: HealthStatus
    raw_reasons: tuple[NonEmptyString, ...]
    hysteresis: HealthHysteresis
    final_status: HealthStatus
    score: RatioValue | None
    score_mapping: HealthScoreMapping = HealthScoreMapping()
    rule_version: NonEmptyString


class HealthState(RadarContractModel):
    entity_id: NonEmptyString
    status: HealthStatus
    score: RatioValue | None
    reasons: tuple[NonEmptyString, ...]
    missing_fields: tuple[NonEmptyString, ...]
    evaluated_at: UtcDateTime
    rule_version: NonEmptyString
    # Explanation of this evaluation; null for states recorded without one.
    derivation: HealthDerivation | None = None


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

    @model_validator(mode="after")
    def validate_pair_and_flags(self) -> "SimilarityRecord":
        if self.catalog_entity_id == self.candidate_entity_id:
            raise ValueError("a vault cannot be similar to itself")
        if self.normally_visible and not self.stored:
            raise ValueError("normally visible similarities must be stored")
        return self


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
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at cannot precede first_observed_at")
        if self.resolved_at is not None and self.resolved_at < self.first_observed_at:
            raise ValueError("resolved_at cannot precede first_observed_at")
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

    @model_validator(mode="after")
    def validate_path(self) -> "IncidentImpact":
        if self.path[-1] != self.target_entity_id:
            raise ValueError("impact path must end at the target entity")
        if self.distance != len(self.path) - 1:
            raise ValueError("impact distance must equal the number of path edges")
        if len(set(self.path)) != len(self.path):
            raise ValueError("impact path must not repeat entities")
        return self


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


class EntityHealthResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    entity_id: NonEmptyString
    # Latest recorded evaluation, or null before the entity is first evaluated.
    health: HealthState | None
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



class NotificationReadAllResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    updated: int = Field(ge=0)

class ExposureCreateRequest(ExposureFields):
    pass


class ExposureImpactResponse(RadarContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    exposure: ManualExposure
    impacts: tuple[IncidentImpact, ...]
    generated_at: UtcDateTime
