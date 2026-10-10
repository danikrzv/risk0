from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)


def reject_binary_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("binary floats are not valid domain decimal values")
    return value


def require_integer(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("chain_id must be a JSON integer")
    return value


def serialize_fixed_point(value: Decimal) -> str:
    """Emit plain decimal notation; `str(Decimal)` would produce `0E-18` or `1E-7`."""
    if value.is_zero():
        value = abs(value)
    return format(value, "f")


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(reject_binary_float),
    PlainSerializer(serialize_fixed_point, return_type=str, when_used="json"),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
HexIdentifier = Annotated[
    str, StringConstraints(pattern=r"^0x[0-9A-Fa-f]+$")
]
LltvValue = Annotated[DecimalValue, Field(ge=0, le=1)]
# Ethereum mainnet and Base. Analysis remains mainnet-only; Base entries are listed.
CatalogChainId = Literal[1, 8453]
ChainIdValue = Annotated[CatalogChainId, BeforeValidator(require_integer)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogEntityType(StrEnum):
    MORPHO_VAULT_V2 = "morpho_vault_v2"
    MORPHO_VAULT_V1 = "morpho_vault_v1"
    MORPHO_MARKET = "morpho_market"


class CatalogProvenance(StrEnum):
    VERIFIED_BELEM = "verified_belem"
    BELEM_DISTRIBUTED = "belem_distributed"
    REPRESENTATIVE_PROXY = "representative_proxy"
    ROCKAWAYX_CURATED = "rockawayx_curated"


class PositionMode(StrEnum):
    LENDER = "lender"
    BORROWER = "borrower"


class ScenarioId(StrEnum):
    STABLECOIN_DEPEG = "stablecoin-depeg"
    COLLATERAL_SHOCK = "collateral-shock"
    ORACLE_FAILURE = "oracle-failure"
    LIQUIDITY_COLLAPSE = "liquidity-collapse"
    PROTOCOL_IMPAIRMENT = "protocol-impairment"


class RiskNodeType(StrEnum):
    POSITION = "position"
    VAULT = "vault"
    MARKET = "market"
    PROTOCOL = "protocol"
    ASSET = "asset"
    ORACLE = "oracle"
    POOL = "pool"
    CHAIN = "chain"


class RiskEdgeType(StrEnum):
    ALLOCATED_TO = "allocated_to"
    LENDS = "lends"
    BORROWS = "borrows"
    USES_COLLATERAL = "uses_collateral"
    USES_ORACLE = "uses_oracle"
    BACKED_BY = "backed_by"
    EXITS_THROUGH = "exits_through"
    DEPLOYED_ON = "deployed_on"


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE_USABLE = "stale_usable"
    EXPIRED = "expired"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNAVAILABLE = "unavailable"


class ProbabilityMethod(StrEnum):
    EMPIRICAL_ROLLING_WINDOW = "empirical_rolling_window"
    MODEL_ESTIMATED_PROTOCOL = "model_estimated_protocol"
    UNAVAILABLE = "unavailable"


class RecommendationDecision(StrEnum):
    STAY = "STAY"
    EXIT = "EXIT"
    UNAVAILABLE = "UNAVAILABLE"


class CatalogAvailability(StrEnum):
    AVAILABLE = "available"
    CATALOG_INACTIVE = "catalog_inactive"
    NOT_FOUND = "not_found"
    DELISTED = "delisted"
    DATA_CONFLICT = "data_conflict"
    CRITICAL_DATA_MISSING = "critical_data_missing"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class CatalogEntry(ContractModel):
    entity_id: NonEmptyString
    chain_id: ChainIdValue
    entity_type: CatalogEntityType
    address_or_market_id: HexIdentifier
    name: NonEmptyString
    description: NonEmptyString
    provenance: CatalogProvenance
    provenance_label: NonEmptyString
    supported: bool
    supported_modes: tuple[PositionMode, ...] = Field(min_length=1)
    lltv: LltvValue | None

    @field_validator("supported_modes")
    @classmethod
    def supported_modes_are_unique(
        cls, modes: tuple[PositionMode, ...]
    ) -> tuple[PositionMode, ...]:
        if len(modes) != len(set(modes)):
            raise ValueError("supported_modes must be unique")
        return modes


class CatalogDocument(ContractModel):
    entries: tuple[CatalogEntry, ...]


class CatalogMetrics(ContractModel):
    asset_symbol: NonEmptyString
    collateral_symbol: NonEmptyString | None
    total_assets: DecimalValue
    total_assets_usd: DecimalValue | None
    liquidity_assets: DecimalValue
    liquidity_usd: DecimalValue | None
    utilization: DecimalValue | None


class HydratedCatalogEntry(CatalogEntry):
    analysis_available: bool
    availability: CatalogAvailability
    metrics: CatalogMetrics | None
    source: NonEmptyString | None
    fetched_at: datetime | None
    freshness: FreshnessStatus | None
    age_seconds: int | None = Field(default=None, ge=0)
    block_number: int | None = Field(default=None, ge=0)
    warnings: tuple[NonEmptyString, ...] = ()


class CatalogResponse(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    entries: tuple[HydratedCatalogEntry, ...]
    generated_at: datetime


class PositionInput(ContractModel):
    mode: PositionMode
    entity_id: NonEmptyString
    amount: DecimalValue
    collateral_amount: DecimalValue | None = None
    debt_amount: DecimalValue | None = None
    ltv: DecimalValue | None = None
    numeraire: NonEmptyString

    @model_validator(mode="before")
    @classmethod
    def validate_mode_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        borrower_fields = ("collateral_amount", "debt_amount", "ltv")
        if value.get("mode") == PositionMode.LENDER and any(
            field in value for field in borrower_fields
        ):
            raise ValueError("lender positions forbid borrower fields")
        if value.get("mode") == PositionMode.BORROWER and any(
            field not in value or value[field] is None for field in borrower_fields
        ):
            raise ValueError("borrower positions require collateral, debt, and LTV")
        return value


class ScenarioSelection(ContractModel):
    scenario_id: ScenarioId
    severity: DecimalValue


MetricValue: TypeAlias = DecimalValue | str | bool | None


class RiskNode(ContractModel):
    node_id: str
    node_type: RiskNodeType
    label: str
    metrics: dict[str, MetricValue]
    provenance: str
    freshness: FreshnessStatus
    observed_at: datetime


class RiskEdge(ContractModel):
    source: str
    target: str
    relationship: RiskEdgeType
    exposure_weight: DecimalValue | None = None


class RiskGraph(ContractModel):
    nodes: tuple[RiskNode, ...]
    edges: tuple[RiskEdge, ...]
    generated_at: datetime
    completeness_warnings: tuple[str, ...] = ()


class ProbabilityEstimate(ContractModel):
    method: ProbabilityMethod
    low: DecimalValue | None
    central: DecimalValue | None
    high: DecimalValue | None
    confidence: ConfidenceLevel
    horizon_days: Literal[30]
    source: NonEmptyString
    retrieved_at: datetime
    freshness: FreshnessStatus
    explanation: str


class ScenarioResult(ContractModel):
    scenario_id: str
    affected_path: tuple[str, ...]
    stressed_values: dict[str, MetricValue]
    hard_triggers: tuple[str, ...]
    impact: DecimalValue
    impact_components: dict[str, DecimalValue]
    probability: ProbabilityEstimate
    confidence: ConfidenceLevel
    explanation: str


class ExitEstimate(ContractModel):
    withdrawable: bool
    route: tuple[str, ...]
    slippage: DecimalValue
    fees: DecimalValue
    gas: DecimalValue
    price_impact: DecimalValue
    total_cost: DecimalValue
    quote_age_seconds: int = Field(ge=0)
    quote_block: int | None = Field(default=None, ge=0)
    quoted_at: datetime
    source: NonEmptyString
    retrieved_at: datetime
    freshness: FreshnessStatus
    warnings: tuple[str, ...] = ()


class Recommendation(ContractModel):
    decision: RecommendationDecision
    expected_loss: DecimalValue
    expected_loss_low: DecimalValue | None = None
    expected_loss_high: DecimalValue | None = None
    exit_cost: DecimalValue
    triggers: tuple[str, ...]
    dominant_dependency: str | None
    confidence: ConfidenceLevel
    explanation: str


class AnalysisRequest(ContractModel):
    position: PositionInput
    scenarios: tuple[ScenarioSelection, ...] = Field(min_length=1)


class AnalysisResponse(ContractModel):
    graph: RiskGraph
    scenario_results: tuple[ScenarioResult, ...]
    exit_estimate: ExitEstimate
    recommendation: Recommendation
    analyzed_at: datetime
