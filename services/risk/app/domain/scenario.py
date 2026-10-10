"""Scenario definitions and the read-only position view the scenario engine needs.

Severities are fractions in [0, 1]: a 0.15 collateral shock means the collateral
loses 15% of its reference value. Bounds come from the spec's default scenario set.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from app.contracts.models import PositionMode, RiskNodeType, ScenarioId, ScenarioSelection


class ScenarioPosition(Protocol):
    """Structural view of a validated position (Task 5's `ValidatedPosition`).

    Values are `Decimal`. `*_value` fields and lender `amount` share one unit, the
    position numeraire, which is also the unit of every scenario impact.
    """

    @property
    def mode(self) -> PositionMode: ...  # lender or borrower

    @property
    def entity_id(self) -> str: ...  # curated catalog entry id, e.g. "weth-usdc-860"

    @property
    def numeraire(self) -> str: ...  # unit impacts are expressed in, e.g. "USD"

    @property
    def amount(self) -> Decimal: ...  # lender deposit (numeraire units); borrowers: as submitted

    @property
    def collateral_amount(self) -> Decimal | None: ...  # borrower collateral in token units

    @property
    def debt_amount(self) -> Decimal | None: ...  # borrower debt in token units

    @property
    def ltv(self) -> Decimal | None: ...  # borrower current loan-to-value

    @property
    def collateral_value(self) -> Decimal | None: ...  # borrower collateral in numeraire units

    @property
    def debt_value(self) -> Decimal | None: ...  # borrower debt in numeraire units

    @property
    def lltv(self) -> Decimal | None: ...  # market liquidation LTV; None for vault lenders


class EventFamily(StrEnum):
    PRICE_DEPEG = "price_depeg"
    PRICE_SHOCK = "price_shock"
    ORACLE_FAILURE = "oracle_failure"
    LIQUIDITY_COLLAPSE = "liquidity_collapse"
    PROTOCOL_IMPAIRMENT = "protocol_impairment"


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: ScenarioId
    event_family: EventFamily
    description: str
    affected_node_types: tuple[RiskNodeType, ...]
    # Asset symbols the event applies to; empty when the event is not asset-specific.
    affected_assets: tuple[str, ...]
    default_severity: Decimal
    minimum_severity: Decimal
    maximum_severity: Decimal
    minimum_inclusive: bool
    maximum_inclusive: bool

    def accepts(self, severity: Decimal) -> bool:
        if not severity.is_finite():
            return False
        above = (
            severity >= self.minimum_severity
            if self.minimum_inclusive
            else severity > self.minimum_severity
        )
        below = (
            severity <= self.maximum_severity
            if self.maximum_inclusive
            else severity < self.maximum_severity
        )
        return above and below

    def bounds_label(self) -> str:
        opening = "[" if self.minimum_inclusive else "("
        closing = "]" if self.maximum_inclusive else ")"
        return f"{opening}{self.minimum_severity}, {self.maximum_severity}{closing}"


# Oracle-failure event thresholds (spec § "Default Scenario Set", § 13).
ORACLE_STALE_THRESHOLD_SECONDS = Decimal("1800")
ORACLE_DEVIATION_THRESHOLD = Decimal("0.05")
# § 13: lender hard trigger when stressed available liquidity < 20% of the position.
WITHDRAWAL_TRIGGER_RATIO = Decimal("0.20")
# Morpho Blue liquidation incentive: min(M, 1 / (cursor * lltv + (1 - cursor))).
MORPHO_LIQUIDATION_CURSOR = Decimal("0.3")
MORPHO_MAX_LIQUIDATION_INCENTIVE_FACTOR = Decimal("1.15")

# Impact component names. Only PERMANENT_LOSS_COMPONENTS are summed into `impact`;
# a liquidity shortfall is reported but is never a realized loss.
ASSET_IMPAIRMENT = "asset_impairment"
LIQUIDATION_INCENTIVE = "liquidation_incentive"
LIQUIDITY_SHORTFALL = "liquidity_shortfall"
PROTOCOL_IMPAIRMENT = "protocol_impairment"
IMPACT_COMPONENTS = (
    ASSET_IMPAIRMENT,
    LIQUIDATION_INCENTIVE,
    LIQUIDITY_SHORTFALL,
    PROTOCOL_IMPAIRMENT,
)
PERMANENT_LOSS_COMPONENTS = (ASSET_IMPAIRMENT, LIQUIDATION_INCENTIVE, PROTOCOL_IMPAIRMENT)

# Hard trigger names consumed by the recommendation rule (Task 10).
LIQUIDATION_TRIGGER = "liquidation"
ORACLE_FAILURE_TRIGGER = "oracle_failure"
WITHDRAWAL_SHORTFALL_TRIGGER = "withdrawal_shortfall"


SCENARIO_DEFINITIONS: Mapping[ScenarioId, ScenarioDefinition] = MappingProxyType(
    {
        ScenarioId.STABLECOIN_DEPEG: ScenarioDefinition(
            scenario_id=ScenarioId.STABLECOIN_DEPEG,
            event_family=EventFamily.PRICE_DEPEG,
            description="EURCV or USDC falls from its reference value",
            affected_node_types=(RiskNodeType.ASSET,),
            affected_assets=("EURCV", "USDC"),
            default_severity=Decimal("0.10"),
            minimum_severity=Decimal("0"),
            maximum_severity=Decimal("1"),
            minimum_inclusive=False,
            maximum_inclusive=True,
        ),
        ScenarioId.COLLATERAL_SHOCK: ScenarioDefinition(
            scenario_id=ScenarioId.COLLATERAL_SHOCK,
            event_family=EventFamily.PRICE_SHOCK,
            description="wstETH, WETH, or WBTC collateral falls over 24 hours",
            affected_node_types=(RiskNodeType.ASSET,),
            affected_assets=("wstETH", "WETH", "WBTC"),
            default_severity=Decimal("0.15"),
            minimum_severity=Decimal("0"),
            maximum_severity=Decimal("1"),
            minimum_inclusive=False,
            maximum_inclusive=True,
        ),
        ScenarioId.ORACLE_FAILURE: ScenarioDefinition(
            scenario_id=ScenarioId.ORACLE_FAILURE,
            event_family=EventFamily.ORACLE_FAILURE,
            description=(
                "Oracle price is stale for 30 minutes or deviates from the "
                "comparison market; severity is the adverse price deviation"
            ),
            affected_node_types=(RiskNodeType.ORACLE,),
            affected_assets=(),
            default_severity=ORACLE_DEVIATION_THRESHOLD,
            minimum_severity=ORACLE_DEVIATION_THRESHOLD,
            maximum_severity=Decimal("1"),
            minimum_inclusive=True,
            maximum_inclusive=False,
        ),
        ScenarioId.LIQUIDITY_COLLAPSE: ScenarioDefinition(
            scenario_id=ScenarioId.LIQUIDITY_COLLAPSE,
            event_family=EventFamily.LIQUIDITY_COLLAPSE,
            description=(
                "Executable Uniswap depth and immediately withdrawable "
                "liquidity fall by the severity"
            ),
            affected_node_types=(
                RiskNodeType.POOL,
                RiskNodeType.VAULT,
                RiskNodeType.MARKET,
            ),
            affected_assets=(),
            default_severity=Decimal("0.80"),
            minimum_severity=Decimal("0"),
            maximum_severity=Decimal("1"),
            minimum_inclusive=False,
            maximum_inclusive=True,
        ),
        ScenarioId.PROTOCOL_IMPAIRMENT: ScenarioDefinition(
            scenario_id=ScenarioId.PROTOCOL_IMPAIRMENT,
            event_family=EventFamily.PROTOCOL_IMPAIRMENT,
            description="A smart-contract or control failure impairs exposed capital",
            affected_node_types=(RiskNodeType.PROTOCOL,),
            affected_assets=(),
            default_severity=Decimal("0.10"),
            minimum_severity=Decimal("0.10"),
            maximum_severity=Decimal("1"),
            minimum_inclusive=True,
            maximum_inclusive=True,
        ),
    }
)


class ScenarioSeverityError(ValueError):
    pass


def validate_selection(selection: ScenarioSelection) -> ScenarioDefinition:
    definition = SCENARIO_DEFINITIONS[selection.scenario_id]
    if not definition.accepts(selection.severity):
        raise ScenarioSeverityError(
            f"{selection.scenario_id.value} severity {selection.severity} is outside "
            f"{definition.bounds_label()}"
        )
    return definition


def liquidation_incentive_factor(lltv: Decimal) -> Decimal:
    denominator = MORPHO_LIQUIDATION_CURSOR * lltv + (Decimal(1) - MORPHO_LIQUIDATION_CURSOR)
    return min(MORPHO_MAX_LIQUIDATION_INCENTIVE_FACTOR, Decimal(1) / denominator)
