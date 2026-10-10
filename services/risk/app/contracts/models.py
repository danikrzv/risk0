from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field


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


class CatalogEntry(ContractModel):
    entity_id: str
    chain_id: int = Field(ge=1, le=1)
    entity_type: CatalogEntityType
    address_or_market_id: str
    name: str
    description: str
    provenance: CatalogProvenance
    provenance_label: str
    supported: bool
    supported_modes: tuple[PositionMode, ...]
    lltv: Decimal | None


class CatalogResponse(ContractModel):
    entries: tuple[CatalogEntry, ...]


class PositionInput(ContractModel):
    mode: PositionMode
    entity_id: str
    amount: Decimal
    collateral_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    ltv: Decimal | None = None
    numeraire: str


class ScenarioSelection(ContractModel):
    scenario_id: ScenarioId
    severity: Decimal


MetricValue: TypeAlias = Decimal | str | bool | None


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
    exposure_weight: Decimal | None = None


class RiskGraph(ContractModel):
    nodes: tuple[RiskNode, ...]
    edges: tuple[RiskEdge, ...]
    generated_at: datetime
    completeness_warnings: tuple[str, ...] = ()


class ProbabilityEstimate(ContractModel):
    method: ProbabilityMethod
    low: Decimal | None
    central: Decimal | None
    high: Decimal | None
    confidence: ConfidenceLevel
    horizon_days: int = Field(ge=1)
    explanation: str


class ScenarioResult(ContractModel):
    scenario_id: str
    affected_path: tuple[str, ...]
    stressed_values: dict[str, MetricValue]
    hard_triggers: tuple[str, ...]
    impact: Decimal
    impact_components: dict[str, Decimal]
    probability: ProbabilityEstimate
    confidence: ConfidenceLevel
    explanation: str


class ExitEstimate(ContractModel):
    withdrawable: bool
    route: tuple[str, ...]
    slippage: Decimal
    fees: Decimal
    gas: Decimal
    price_impact: Decimal
    total_cost: Decimal
    quote_age_seconds: int = Field(ge=0)
    quote_block: int | None = Field(default=None, ge=0)
    quoted_at: datetime | None = None
    warnings: tuple[str, ...] = ()


class Recommendation(ContractModel):
    decision: RecommendationDecision
    expected_loss: Decimal
    expected_loss_low: Decimal | None = None
    expected_loss_high: Decimal | None = None
    exit_cost: Decimal
    triggers: tuple[str, ...]
    dominant_dependency: str | None
    confidence: ConfidenceLevel
    explanation: str


class AnalysisRequest(ContractModel):
    position: PositionInput
    scenarios: tuple[ScenarioSelection, ...]


class AnalysisResponse(ContractModel):
    graph: RiskGraph
    scenario_results: tuple[ScenarioResult, ...]
    exit_estimate: ExitEstimate
    recommendation: Recommendation
    analyzed_at: datetime
