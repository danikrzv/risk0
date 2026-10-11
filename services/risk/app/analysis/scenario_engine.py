"""Propagate scenario shocks through a position's risk graph into impacts.

Traversal rules:
- Loss propagation follows every edge except `exits_through` (exit pools only
  matter for liquidity). Edges with `exposure_weight == 0` are never traversed.
- Exposure of a node to the shocked set is 1 if the node is shocked; otherwise the
  max over unweighted (full) dependencies and the sum over weighted (allocation)
  edges, capped at 1. Capital reaching a shocked dependency by several paths is
  therefore counted once per scenario.
- `affected_path` holds every node on some position-to-shocked-node path, ordered
  by breadth-first discovery (ties broken by node id), shocked nodes last.
- A node with metric `exposure_unknown=True` (the graph builder's "Unmodelled
  allocation") hides an unknown share of capital. For asset and oracle scenarios
  that share cannot be priced, so when it is reachable with non-zero weight and
  the known exposure is not already full, the scenario's confidence is
  UNAVAILABLE: the impact on the known exposure is reported but is incomplete.
- A borrower's collateral or debt asset is shocked by a price event when the
  event's asset is the asset itself or anything it is `backed_by` (registry
  look-through: a wrapper such as wstETH moves with stETH). Lender exposure
  already follows `backed_by` edges as loss edges.
- Lender price events (depeg, collateral shock) split into two parts:
  * held claim: exposure to the shocked asset over loss edges that never cross
    `uses_collateral` (the loan asset the lender holds, and what backs it) loses
    the severity 1:1, as before;
  * collateral: every reachable market whose collateral (or what backs it) is
    shocked costs its suppliers only bad debt (`app.domain.bad_debt`): an
    instantaneous gap move with no liquidation executed, priced from the
    market's borrower distribution when one is supplied to `run`, otherwise at
    the worst-case bound (all debt at LLTV, times utilization), which lowers the
    scenario confidence one level. The shock is relative to the loan asset: the
    loan asset falls too when it is (or is backed by) a shocked asset, or, for a
    market-price shock, when it shares an underlying asset with the collateral
    (WETH debt against wstETH collateral both track ETH); then only the ratio of
    the two moves counts. A depeg is issuer-specific: a shared reference does
    not co-move. The lender loses
    `sum_m amount * exposure_to_m * supplier_loss_fraction_m`.
- Every result carries `stressed_values["impact_complete"]`: False when the
  impact rests on missing, expired, or unmodelled data and must not be used as
  a complete loss figure.
"""

from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    MetricValue,
    PositionMode,
    ProbabilityEstimate,
    ProbabilityMethod,
    RiskEdge,
    RiskEdgeType,
    RiskGraph,
    RiskNode,
    RiskNodeType,
    ScenarioResult,
    ScenarioSelection,
)
from app.domain.bad_debt import (
    METHOD_LLTV_UNKNOWN,
    METHOD_POSITIONS,
    BadDebtEstimate,
    BorrowerDistribution,
    estimate_bad_debt,
)
from app.domain.scenario import (
    ASSET_IMPAIRMENT,
    IMPACT_COMPONENTS,
    LIQUIDATION_INCENTIVE,
    LIQUIDATION_TRIGGER,
    LIQUIDITY_SHORTFALL,
    ORACLE_DEVIATION_THRESHOLD,
    ORACLE_FAILURE_TRIGGER,
    ORACLE_STALE_THRESHOLD_SECONDS,
    PERMANENT_LOSS_COMPONENTS,
    PROTOCOL_IMPAIRMENT,
    WITHDRAWAL_SHORTFALL_TRIGGER,
    WITHDRAWAL_TRIGGER_RATIO,
    EventFamily,
    ScenarioDefinition,
    ScenarioPosition,
    liquidation_incentive_factor,
    validate_selection,
)


# RiskNode.metrics keys read by the engine. Task 5's graph builder must emit these
# as Decimal values (symbol as str) for the engine to use them.
SYMBOL_KEY = "symbol"  # asset nodes; falls back to the node label
LLTV_KEY = "lltv"  # market nodes; used when the position carries no lltv
AVAILABLE_LIQUIDITY_KEY = "available_liquidity"  # vault/market nodes, position units
POOL_DEPTH_KEY = "liquidity_depth"  # pool nodes, executable depth in numeraire units
ORACLE_AGE_SECONDS_KEY = "age_seconds"  # oracle nodes, seconds since last update
ORACLE_DEVIATION_KEY = "deviation"  # oracle nodes, fractional deviation vs comparison
EXPOSURE_UNKNOWN_KEY = "exposure_unknown"  # unmodelled allocation nodes (True)
IMPACT_COMPLETE_KEY = "impact_complete"
UNMODELLED_EXPOSURE_KEY = "unmodelled_exposure_fraction"
# Families whose shock cannot be priced through an unmodelled allocation.
UNMODELLED_SENSITIVE_FAMILIES = frozenset(
    {EventFamily.PRICE_DEPEG, EventFamily.PRICE_SHOCK, EventFamily.ORACLE_FAILURE}
)

MARKET_ID_KEY = "market_id"  # market nodes, lowercase hex id
TOTAL_SUPPLY_KEY = "total_supply_assets"  # market nodes, loan-asset units
TOTAL_BORROW_KEY = "total_borrow_assets"  # market nodes, loan-asset units
UTILIZATION_KEY = "utilization"  # market nodes, total borrow / total supply
PRICE_FAMILIES = frozenset({EventFamily.PRICE_DEPEG, EventFamily.PRICE_SHOCK})
MAX_EXPLAINED_MARKETS = 3

ZERO = Decimal(0)
ONE = Decimal(1)
CAPITAL_NODE_TYPES = frozenset({RiskNodeType.VAULT, RiskNodeType.MARKET})
ALL_EDGES = frozenset(RiskEdgeType)
LOSS_EDGES = ALL_EDGES - {RiskEdgeType.EXITS_THROUGH}
LOAN_ASSET_EDGES = frozenset({RiskEdgeType.BORROWS, RiskEdgeType.LENDS})
# A lender's held claim: everything it lends into, never a market's collateral
# (collateral moves reach a lender only as bad debt).
HELD_CLAIM_EDGES = LOSS_EDGES - {RiskEdgeType.USES_COLLATERAL}
UNAVAILABLE_PROBABILITY_SOURCE = "risk0:scenario-engine"
_LOWER_CONFIDENCE = {
    ConfidenceLevel.HIGH: ConfidenceLevel.MEDIUM,
    ConfidenceLevel.MEDIUM: ConfidenceLevel.LOW,
    ConfidenceLevel.LOW: ConfidenceLevel.LOW,
    ConfidenceLevel.UNAVAILABLE: ConfidenceLevel.UNAVAILABLE,
}
_CONFIDENCE_RANK = {
    ConfidenceLevel.UNAVAILABLE: 0,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.HIGH: 3,
}


class _GraphView:
    def __init__(self, graph: RiskGraph) -> None:
        nodes: dict[str, RiskNode] = {}
        for node in graph.nodes:
            if node.node_id in nodes:
                raise ValueError(f"duplicate risk graph node {node.node_id}")
            nodes[node.node_id] = node
        outgoing: dict[str, list[RiskEdge]] = {node_id: [] for node_id in nodes}
        warnings: list[str] = []
        for edge in graph.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError(f"risk graph edge {edge.source} -> {edge.target} is dangling")
            weight = edge.exposure_weight
            if weight is not None and not ZERO <= weight <= ONE:
                raise ValueError(f"exposure weight {weight} is outside [0, 1]")
            if weight is None and edge.relationship == RiskEdgeType.ALLOCATED_TO:
                warnings.append(
                    f"Allocation weight to {edge.target} is missing; treated as full exposure."
                )
            outgoing[edge.source].append(edge)
        positions = [
            node.node_id for node in nodes.values() if node.node_type == RiskNodeType.POSITION
        ]
        if len(positions) != 1:
            raise ValueError("risk graph must contain exactly one position node")
        self.nodes = nodes
        self.position_id = positions[0]
        self.warnings = tuple(sorted(warnings))
        self._outgoing = {
            node_id: tuple(sorted(edges, key=lambda e: (e.target, e.relationship.value)))
            for node_id, edges in outgoing.items()
        }

    def edges_from(
        self, node_id: str, relationships: Collection[RiskEdgeType]
    ) -> tuple[RiskEdge, ...]:
        return tuple(
            edge
            for edge in self._outgoing[node_id]
            if edge.relationship in relationships
            and (edge.exposure_weight is None or edge.exposure_weight > ZERO)
        )

    def reachable(self, relationships: Collection[RiskEdgeType]) -> dict[str, int]:
        order = {self.position_id: 0}
        queue = deque([self.position_id])
        while queue:
            for edge in self.edges_from(queue.popleft(), relationships):
                if edge.target not in order:
                    order[edge.target] = len(order)
                    queue.append(edge.target)
        return order

    def backing_closure(self, assets: Iterable[str]) -> frozenset[str]:
        """The given nodes plus every node reachable from them over `backed_by` edges."""
        seen = set(assets)
        queue = deque(seen)
        while queue:
            for edge in self.edges_from(queue.popleft(), {RiskEdgeType.BACKED_BY}):
                if edge.target not in seen:
                    seen.add(edge.target)
                    queue.append(edge.target)
        return frozenset(seen)

    def nodes_of_type(self, node_type: RiskNodeType, within: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            node_id for node_id in within if self.nodes[node_id].node_type == node_type
        )

    def path_to(
        self, targets: Collection[str], relationships: Collection[RiskEdgeType]
    ) -> tuple[str, ...]:
        order = self.reachable(relationships)
        reverse: dict[str, list[str]] = {}
        for source in order:
            for edge in self.edges_from(source, relationships):
                reverse.setdefault(edge.target, []).append(source)
        on_path = {target for target in targets if target in order}
        queue = deque(on_path)
        while queue:
            for source in reverse.get(queue.popleft(), ()):
                if source not in on_path:
                    on_path.add(source)
                    queue.append(source)
        return tuple(
            sorted(on_path, key=lambda node_id: (node_id in targets, order[node_id]))
        )

    def exposure(
        self, targets: Collection[str], relationships: Collection[RiskEdgeType] = LOSS_EDGES
    ) -> Decimal:
        memo: dict[str, Decimal] = {}
        visiting: set[str] = set()

        def visit(node_id: str) -> Decimal:
            if node_id in targets:
                return ONE
            if node_id in memo:
                return memo[node_id]
            if node_id in visiting:
                raise ValueError(f"risk graph contains a dependency cycle at {node_id}")
            visiting.add(node_id)
            full = ZERO
            split = ZERO
            for edge in self.edges_from(node_id, relationships):
                child = visit(edge.target)
                if edge.exposure_weight is None:
                    full = max(full, child)
                else:
                    split += edge.exposure_weight * child
            visiting.discard(node_id)
            memo[node_id] = min(ONE, max(full, split))
            return memo[node_id]

        return visit(self.position_id)

    def decimal_metric(self, node_id: str, key: str) -> Decimal | None:
        value = self.nodes[node_id].metrics.get(key)
        if isinstance(value, Decimal) and value.is_finite():
            return value
        return None

    def symbol(self, node_id: str) -> str:
        node = self.nodes[node_id]
        value = node.metrics.get(SYMBOL_KEY)
        return value if isinstance(value, str) and value else node.label


@dataclass
class _Outcome:
    path: tuple[str, ...] = ()
    components: dict[str, Decimal] = field(
        default_factory=lambda: {name: ZERO for name in IMPACT_COMPONENTS}
    )
    stressed_values: dict[str, MetricValue] = field(default_factory=dict)
    hard_triggers: list[str] = field(default_factory=list)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unmodelled: Decimal = ZERO
    # Explanation notes that do not by themselves lower confidence.
    notes: list[str] = field(default_factory=list)
    # The impact rests on a worst-case bound: confidence is lowered one level.
    bounded: bool = False


class ScenarioEngine:
    def run(
        self,
        position: ScenarioPosition,
        graph: RiskGraph,
        selections: tuple[ScenarioSelection, ...],
        probabilities: Mapping[str, ProbabilityEstimate],
        *,
        borrowers: Mapping[str, BorrowerDistribution] | None = None,
    ) -> tuple[ScenarioResult, ...]:
        """Propagate each selection; `borrowers` maps a market id to its borrower distribution.

        A lender's loss from a collateral price fall is the bad debt of the markets
        it supplies (see `app.domain.bad_debt`); a market without a distribution is
        priced at the worst-case bound and lowers the scenario confidence.
        """
        definitions = tuple(validate_selection(selection) for selection in selections)
        view = _GraphView(graph)
        books = {key.lower(): value for key, value in (borrowers or {}).items()}
        return tuple(
            self._run_one(position, graph, view, selection, definition, probabilities, books)
            for selection, definition in zip(selections, definitions, strict=True)
        )

    def collateral_markets(
        self, graph: RiskGraph, selections: Iterable[ScenarioSelection]
    ) -> tuple[str, ...]:
        """Market ids whose collateral a selected price scenario shocks, most exposed first.

        These are the markets a lender's bad-debt estimate needs borrower
        positions for; the analysis service fetches them before running.
        """
        view = _GraphView(graph)
        exposures: dict[str, Decimal] = {}
        for selection in selections:
            definition = validate_selection(selection)
            if definition.event_family not in PRICE_FAMILIES:
                continue
            targets = self._targets(view, definition)
            for market in self._collateral_shocked_markets(view, targets):
                market_id = view.nodes[market].metrics.get(MARKET_ID_KEY)
                if isinstance(market_id, str) and market_id:
                    exposures[market_id.lower()] = view.exposure({market})
        return tuple(sorted(exposures, key=lambda key: (-exposures[key], key)))

    def _run_one(
        self,
        position: ScenarioPosition,
        graph: RiskGraph,
        view: _GraphView,
        selection: ScenarioSelection,
        definition: ScenarioDefinition,
        probabilities: Mapping[str, ProbabilityEstimate],
        borrowers: Mapping[str, BorrowerDistribution],
    ) -> ScenarioResult:
        severity = selection.severity
        if position.mode == PositionMode.LENDER:
            outcome = self._lender(position, view, definition, severity, borrowers)
        else:
            outcome = self._borrower(position, view, definition, severity)
        if definition.event_family == EventFamily.ORACLE_FAILURE:
            self._check_live_oracles(view, outcome)
        if definition.event_family == EventFamily.LIQUIDITY_COLLAPSE:
            self._stress_exit_pools(view, severity, outcome)
        if definition.event_family in UNMODELLED_SENSITIVE_FAMILIES:
            self._check_unmodelled_exposure(view, definition, outcome)

        probability = probabilities.get(selection.scenario_id.value)
        if probability is None:
            probability = _unavailable_probability(selection, graph)
        impact = sum(
            (outcome.components[name] for name in PERMANENT_LOSS_COMPONENTS), ZERO
        )
        data_confidence = self._data_confidence(graph, view, outcome)
        outcome.stressed_values[IMPACT_COMPLETE_KEY] = (
            data_confidence != ConfidenceLevel.UNAVAILABLE
        )
        confidence = min(
            data_confidence,
            probability.confidence,
            key=_CONFIDENCE_RANK.__getitem__,
        )
        return ScenarioResult(
            scenario_id=selection.scenario_id.value,
            affected_path=outcome.path,
            stressed_values=outcome.stressed_values,
            hard_triggers=tuple(outcome.hard_triggers),
            impact=impact,
            impact_components=outcome.components,
            probability=probability,
            confidence=confidence,
            explanation=_explain(position, definition, severity, outcome, impact, probability),
        )

    # Targets

    def _targets(self, view: _GraphView, definition: ScenarioDefinition) -> frozenset[str]:
        family = definition.event_family
        if family in (EventFamily.PRICE_DEPEG, EventFamily.PRICE_SHOCK):
            symbols = {symbol.casefold() for symbol in definition.affected_assets}
            assets = {
                node_id
                for node_id in view.nodes_of_type(RiskNodeType.ASSET, view.nodes)
                if view.symbol(node_id).casefold() in symbols
            }
            if family == EventFamily.PRICE_SHOCK:
                collateral = {
                    edge.target
                    for node_id in view.nodes
                    for edge in view.edges_from(node_id, {RiskEdgeType.USES_COLLATERAL})
                }
                assets &= collateral
            return frozenset(assets)
        if family == EventFamily.LIQUIDITY_COLLAPSE:
            entries = {
                edge.target
                for edge in view.edges_from(view.position_id, LOSS_EDGES)
                if view.nodes[edge.target].node_type in CAPITAL_NODE_TYPES
            }
            pools = view.nodes_of_type(RiskNodeType.POOL, view.reachable(ALL_EDGES))
            return frozenset(entries.union(pools))
        return frozenset(
            node_id
            for node_type in definition.affected_node_types
            for node_id in view.nodes_of_type(node_type, view.nodes)
        )

    # Lender

    def _lender(
        self,
        position: ScenarioPosition,
        view: _GraphView,
        definition: ScenarioDefinition,
        severity: Decimal,
        borrowers: Mapping[str, BorrowerDistribution],
    ) -> _Outcome:
        outcome = _Outcome()
        family = definition.event_family
        targets = self._targets(view, definition)
        if family == EventFamily.LIQUIDITY_COLLAPSE:
            outcome.path = view.path_to(targets, ALL_EDGES)
            self._stress_withdrawable_liquidity(position, view, targets, severity, outcome)
            return outcome
        if family in PRICE_FAMILIES:
            self._lender_price_event(
                position, view, targets, severity, borrowers, family, outcome
            )
            return outcome

        if family in (EventFamily.ORACLE_FAILURE, EventFamily.PROTOCOL_IMPAIRMENT) and not (
            targets & view.reachable(LOSS_EDGES).keys()
        ):
            exposure = ONE
            outcome.warnings.append(
                f"No {definition.affected_node_types[0].value} dependency in the graph; "
                "assumed full exposure."
            )
        else:
            exposure = view.exposure(targets)
        outcome.path = view.path_to(targets, LOSS_EDGES)
        loss = position.amount * exposure * severity
        # Oracle and protocol failures are bad-debt or protocol haircuts.
        component = PROTOCOL_IMPAIRMENT
        outcome.components[component] = loss
        outcome.stressed_values["exposed_fraction"] = exposure
        outcome.stressed_values["exposed_amount"] = position.amount * exposure
        outcome.summary = (
            f"{exposure} of the {position.amount} {position.numeraire} position is exposed; "
            f"{component.replace('_', ' ')} is {loss} {position.numeraire}."
        )
        return outcome

    def _lender_price_event(
        self,
        position: ScenarioPosition,
        view: _GraphView,
        targets: frozenset[str],
        severity: Decimal,
        borrowers: Mapping[str, BorrowerDistribution],
        family: EventFamily,
        outcome: _Outcome,
    ) -> None:
        """Held-claim impairment plus the bad debt of every collateral-shocked market.

        The claim the lender holds (the loan asset, reached without crossing a
        `uses_collateral` edge) loses the severity 1:1. A market whose collateral
        is shocked costs its suppliers only bad debt: the debt left uncovered once
        the collateral falls relative to the loan asset (`app.domain.bad_debt`).
        """
        numeraire = position.numeraire
        held_exposure = view.exposure(targets, HELD_CLAIM_EDGES)
        held_loss = position.amount * held_exposure * severity
        contributions: list[tuple[str, Decimal, Decimal, BadDebtEstimate]] = []
        bad_debt_loss = ZERO
        bad_debt_exposure = ZERO
        bounded = 0
        for market in self._collateral_shocked_markets(view, targets):
            exposure = view.exposure({market})
            if exposure <= ZERO:
                continue
            estimate = self._market_bad_debt(
                view, market, targets, severity, borrowers, family
            )
            loss = position.amount * exposure * estimate.supplier_loss_fraction
            contributions.append((market, exposure, loss, estimate))
            bad_debt_loss += loss
            bad_debt_exposure += exposure
            if estimate.is_bound and estimate.shock > ZERO:
                bounded += 1

        total_exposure = view.exposure(targets)
        outcome.path = view.path_to(targets, LOSS_EDGES)
        outcome.components[ASSET_IMPAIRMENT] = held_loss + bad_debt_loss
        values = outcome.stressed_values
        values.update(
            loss_model="held claim 1:1 + collateral bad debt",
            exposed_fraction=total_exposure,
            exposed_amount=position.amount * total_exposure,
            held_claim_exposure=held_exposure,
            held_claim_impairment=held_loss,
            bad_debt_exposure=min(ONE, bad_debt_exposure),
            bad_debt_loss=bad_debt_loss,
            bad_debt_markets=Decimal(len(contributions)),
            bad_debt_bounded_markets=Decimal(bounded),
        )
        for market, exposure, loss, estimate in contributions:
            prefix = f"bad_debt.{_short_market(view, market)}"
            values[f"{prefix}.exposure"] = exposure
            values[f"{prefix}.collateral_shock"] = estimate.shock
            values[f"{prefix}.supplier_loss_fraction"] = estimate.supplier_loss_fraction
            values[f"{prefix}.bad_debt"] = estimate.bad_debt
            values[f"{prefix}.loss"] = loss
            values[f"{prefix}.method"] = estimate.method

        parts: list[str] = []
        if held_exposure > ZERO:
            parts.append(
                f"The held claim is {_share(held_exposure)} exposed and loses the severity 1:1: "
                f"{_amount(held_loss)} {numeraire}."
            )
        if contributions:
            parts.append(
                "A collateral fall costs an over-collateralised lender only bad debt "
                "(instantaneous gap move, no liquidation executed)."
            )
            ranked = sorted(contributions, key=lambda item: (-item[2], -item[1], item[0]))
            for market, exposure, loss, estimate in ranked[:MAX_EXPLAINED_MARKETS]:
                parts.append(
                    f"{view.nodes[market].label} ({_share(exposure)} of the position): "
                    f"{_describe_bad_debt(estimate, _loan_unit(view, market))}; "
                    f"loss {_amount(loss)} {numeraire}."
                )
            if len(ranked) > MAX_EXPLAINED_MARKETS:
                rest = ranked[MAX_EXPLAINED_MARKETS:]
                parts.append(
                    f"{len(rest)} more market(s) add "
                    f"{_amount(sum((item[2] for item in rest), ZERO))} {numeraire}."
                )
            parts.append(f"Bad-debt loss is {_amount(bad_debt_loss)} {numeraire}.")
        if not parts:
            parts.append("No held claim or collateral of this position is affected.")
        outcome.summary = " ".join(parts)
        if bounded:
            outcome.bounded = True
            outcome.notes.append(
                f"Worst-case bound: borrower positions unavailable for {bounded} market(s); "
                "their debt is priced as if every borrower sat exactly at LLTV, and the "
                "scenario confidence is lowered one level."
            )
        if any(
            estimate.method == METHOD_POSITIONS
            and estimate.uncovered_debt is not None
            and estimate.uncovered_debt > ZERO
            for _, _, _, estimate in contributions
        ):
            outcome.notes.append(
                "Debt not covered by the fetched borrower positions (top positions only, "
                "or borrowers without a health factor) is priced at the LLTV worst case."
            )

    def _collateral_shocked_markets(
        self, view: _GraphView, targets: frozenset[str]
    ) -> tuple[str, ...]:
        """Reachable markets whose collateral (or what backs it) is a shocked asset."""
        reach = view.reachable(LOSS_EDGES)
        markets = []
        for market in sorted(view.nodes_of_type(RiskNodeType.MARKET, reach)):
            collateral = {
                edge.target for edge in view.edges_from(market, {RiskEdgeType.USES_COLLATERAL})
            }
            if collateral and view.backing_closure(collateral) & targets:
                markets.append(market)
        return tuple(markets)

    def _market_bad_debt(
        self,
        view: _GraphView,
        market: str,
        targets: frozenset[str],
        severity: Decimal,
        borrowers: Mapping[str, BorrowerDistribution],
        family: EventFamily,
    ) -> BadDebtEstimate:
        loans = {
            edge.target
            for edge in view.edges_from(market, {RiskEdgeType.LENDS})
            if view.nodes[edge.target].node_type == RiskNodeType.ASSET
        }
        collateral = {
            edge.target for edge in view.edges_from(market, {RiskEdgeType.USES_COLLATERAL})
        }
        loan_closure = view.backing_closure(loans)
        # The collateral falls by the severity. Only the move relative to the loan
        # asset creates bad debt, so the loan asset falls too when it is (or is
        # backed by) a shocked asset, or, for a market-price shock, when it shares
        # an underlying asset with the collateral (WETH debt against wstETH
        # collateral: both are ETH, and an ETH crash moves both). A depeg is
        # issuer-specific, so a shared reference (USD reserves) does not co-move.
        co_moving = bool(loan_closure & targets)
        if family == EventFamily.PRICE_SHOCK and not co_moving:
            shared = loan_closure & view.backing_closure(collateral)
            co_moving = any(
                view.nodes[node_id].node_type == RiskNodeType.ASSET for node_id in shared
            )
        collateral_factor = ONE - severity
        loan_factor = ONE - severity if co_moving else ONE
        if loan_factor <= ZERO:
            shock = ZERO
        else:
            shock = max(ZERO, ONE - collateral_factor / loan_factor)
        market_id = view.nodes[market].metrics.get(MARKET_ID_KEY)
        distribution = (
            borrowers.get(market_id.lower()) if isinstance(market_id, str) else None
        )
        return estimate_bad_debt(
            shock,
            lltv=view.decimal_metric(market, LLTV_KEY),
            total_supply=view.decimal_metric(market, TOTAL_SUPPLY_KEY),
            total_borrow=view.decimal_metric(market, TOTAL_BORROW_KEY),
            utilization=view.decimal_metric(market, UTILIZATION_KEY),
            distribution=distribution,
        )

    def _stress_withdrawable_liquidity(
        self,
        position: ScenarioPosition,
        view: _GraphView,
        targets: frozenset[str],
        severity: Decimal,
        outcome: _Outcome,
    ) -> None:
        entries = sorted(
            node_id for node_id in targets if view.nodes[node_id].node_type in CAPITAL_NODE_TYPES
        )
        values = [view.decimal_metric(node_id, AVAILABLE_LIQUIDITY_KEY) for node_id in entries]
        outcome.stressed_values["liquidity_shortfall_is_realized_loss"] = False
        if not entries or any(value is None for value in values):
            outcome.stressed_values.update(
                available_liquidity=None,
                stressed_available_liquidity=None,
                withdrawable_amount=None,
            )
            outcome.missing.append("immediately withdrawable liquidity")
            return
        available = sum((value for value in values if value is not None), ZERO)
        stressed = available * (ONE - severity)
        withdrawable = min(position.amount, stressed)
        shortfall = position.amount - withdrawable
        outcome.components[LIQUIDITY_SHORTFALL] = shortfall
        outcome.stressed_values.update(
            available_liquidity=available,
            stressed_available_liquidity=stressed,
            withdrawable_amount=withdrawable,
        )
        if stressed < WITHDRAWAL_TRIGGER_RATIO * position.amount:
            outcome.hard_triggers.append(WITHDRAWAL_SHORTFALL_TRIGGER)
        outcome.summary = (
            f"Withdrawable liquidity falls from {available} to {stressed}; "
            f"{shortfall} {position.numeraire} could not be withdrawn immediately "
            "(reported separately, not a realized loss)."
        )

    # Borrower

    def _borrower(
        self,
        position: ScenarioPosition,
        view: _GraphView,
        definition: ScenarioDefinition,
        severity: Decimal,
    ) -> _Outcome:
        outcome = _Outcome()
        family = definition.event_family
        targets = self._targets(view, definition)
        reach = view.reachable(LOSS_EDGES)
        outcome.path = view.path_to(
            targets, ALL_EDGES if family == EventFamily.LIQUIDITY_COLLAPSE else LOSS_EDGES
        )
        collateral = position.collateral_value
        debt = position.debt_value
        lltv = self._resolve_lltv(position, view, reach, outcome)
        if collateral is None:
            outcome.missing.append("collateral value")
        if debt is None:
            outcome.missing.append("debt value")
        if collateral is not None and collateral <= ZERO:
            outcome.missing.append("positive collateral value")
        if outcome.missing or collateral is None or debt is None or lltv is None:
            outcome.stressed_values.update(stressed_ltv=None, lltv=lltv, liquidated=None)
            return outcome

        collateral_assets = {
            edge.target
            for node_id in reach
            for edge in view.edges_from(node_id, {RiskEdgeType.USES_COLLATERAL})
        }
        loan_assets = {
            edge.target
            for node_id in reach
            for edge in view.edges_from(node_id, LOAN_ASSET_EDGES)
            if view.nodes[edge.target].node_type == RiskNodeType.ASSET
        }
        collateral_factor: Decimal | None = None
        debt_factor: Decimal | None = None
        oracle_factor: Decimal | None = None
        component = ASSET_IMPAIRMENT
        if family in (EventFamily.PRICE_DEPEG, EventFamily.PRICE_SHOCK):
            if view.backing_closure(collateral_assets) & targets:
                collateral_factor = ONE - severity
            if view.backing_closure(loan_assets) & targets:
                debt_factor = ONE - severity
        elif family == EventFamily.ORACLE_FAILURE:
            oracle_factor = ONE - severity
        elif family == EventFamily.PROTOCOL_IMPAIRMENT:
            collateral_factor = ONE - severity
            component = PROTOCOL_IMPAIRMENT
        if family in (EventFamily.ORACLE_FAILURE, EventFamily.PROTOCOL_IMPAIRMENT) and not (
            targets & reach.keys()
        ):
            outcome.warnings.append(
                f"No {definition.affected_node_types[0].value} dependency in the graph; "
                "shock applied to the position anyway."
            )

        # Shock the true collateral value, then what the oracle reports for it.
        stressed_collateral = (
            collateral if collateral_factor is None else collateral * collateral_factor
        )
        oracle_collateral = (
            stressed_collateral if oracle_factor is None else stressed_collateral * oracle_factor
        )
        stressed_debt = debt if debt_factor is None else debt * debt_factor
        stressed_ltv = stressed_debt / oracle_collateral if oracle_collateral > ZERO else None
        liquidated = (
            stressed_debt > ZERO if stressed_ltv is None else stressed_ltv >= lltv
        )
        outcome.components[component] = collateral - stressed_collateral
        outcome.stressed_values.update(
            collateral_value=collateral,
            debt_value=debt,
            current_ltv=debt / collateral,
            stressed_collateral_value=stressed_collateral,
            stressed_debt_value=stressed_debt,
            stressed_ltv=stressed_ltv,
            lltv=lltv,
            liquidated=liquidated,
        )
        if oracle_factor is not None:
            outcome.stressed_values["oracle_collateral_value"] = oracle_collateral
        outcome.summary = f"Stressed LTV is {stressed_ltv} against LLTV {lltv}."
        if liquidated:
            outcome.hard_triggers.append(LIQUIDATION_TRIGGER)
            self._liquidate(
                lltv, stressed_collateral, oracle_collateral, stressed_debt, outcome
            )
        return outcome

    def _resolve_lltv(
        self,
        position: ScenarioPosition,
        view: _GraphView,
        reach: Mapping[str, int],
        outcome: _Outcome,
    ) -> Decimal | None:
        candidates = {
            value
            for node_id in view.nodes_of_type(RiskNodeType.MARKET, reach)
            if (value := view.decimal_metric(node_id, LLTV_KEY)) is not None
        }
        if position.lltv is not None:
            candidates.add(position.lltv)
        if not candidates:
            outcome.missing.append("market LLTV")
            return None
        if len(candidates) > 1:
            outcome.warnings.append("Conflicting LLTV values; the lowest was used.")
        return min(candidates)

    def _liquidate(
        self,
        lltv: Decimal,
        stressed_collateral: Decimal,
        oracle_collateral: Decimal,
        stressed_debt: Decimal,
        outcome: _Outcome,
    ) -> None:
        """Full Morpho Blue liquidation priced at the oracle's collateral value."""
        factor = liquidation_incentive_factor(lltv)
        if oracle_collateral > ZERO:
            seized_at_oracle = min(oracle_collateral, stressed_debt * factor)
            repaid = seized_at_oracle / factor
            seized = (
                seized_at_oracle
                if oracle_collateral == stressed_collateral
                else seized_at_oracle * stressed_collateral / oracle_collateral
            )
        else:
            seized = ZERO
            repaid = ZERO
        incentive = seized - repaid
        outcome.components[LIQUIDATION_INCENTIVE] = incentive
        outcome.stressed_values.update(
            liquidation_incentive_factor=factor,
            collateral_sold=seized,
            debt_repaid=repaid,
            residual_collateral=stressed_collateral - seized,
            residual_debt=stressed_debt - repaid,
        )
        outcome.summary += (
            f" Liquidation sells {seized} of collateral to repay {repaid}; "
            f"the incentive costs {incentive}."
        )

    # Shared stressed metrics

    def _check_live_oracles(self, view: _GraphView, outcome: _Outcome) -> None:
        oracles = sorted(view.nodes_of_type(RiskNodeType.ORACLE, view.reachable(LOSS_EDGES)))
        ages = [view.decimal_metric(node_id, ORACLE_AGE_SECONDS_KEY) for node_id in oracles]
        deviations = [view.decimal_metric(node_id, ORACLE_DEVIATION_KEY) for node_id in oracles]
        known_ages = [age for age in ages if age is not None]
        known_deviations = [abs(value) for value in deviations if value is not None]
        max_age = max(known_ages, default=None)
        max_deviation = max(known_deviations, default=None)
        breached = (max_age is not None and max_age >= ORACLE_STALE_THRESHOLD_SECONDS) or (
            max_deviation is not None and max_deviation >= ORACLE_DEVIATION_THRESHOLD
        )
        if not oracles or len(known_ages) < len(oracles) or len(known_deviations) < len(oracles):
            outcome.warnings.append("Live oracle age or deviation is unavailable.")
        outcome.stressed_values.update(
            oracle_max_age_seconds=max_age,
            oracle_max_deviation=max_deviation,
            oracle_live_breach=breached,
        )
        if breached:
            outcome.hard_triggers.append(ORACLE_FAILURE_TRIGGER)

    def _stress_exit_pools(self, view: _GraphView, severity: Decimal, outcome: _Outcome) -> None:
        pools = sorted(view.nodes_of_type(RiskNodeType.POOL, view.reachable(ALL_EDGES)))
        depths = [view.decimal_metric(node_id, POOL_DEPTH_KEY) for node_id in pools]
        if not pools or any(depth is None for depth in depths):
            outcome.warnings.append("Exit pool depth is unavailable.")
            outcome.stressed_values.update(exit_pool_depth=None, stressed_exit_pool_depth=None)
            return
        depth = sum((value for value in depths if value is not None), ZERO)
        outcome.stressed_values.update(
            exit_pool_depth=depth,
            stressed_exit_pool_depth=depth * (ONE - severity),
        )

    def _check_unmodelled_exposure(
        self, view: _GraphView, definition: ScenarioDefinition, outcome: _Outcome
    ) -> None:
        targets = self._targets(view, definition)
        unknown = frozenset(
            node_id
            for node_id in view.reachable(LOSS_EDGES)
            if view.nodes[node_id].metrics.get(EXPOSURE_UNKNOWN_KEY) is True
            and node_id not in targets
        )
        if not unknown or view.exposure(targets) >= ONE:
            return
        share = view.exposure(unknown)
        if share <= ZERO:
            return
        outcome.unmodelled = share
        outcome.stressed_values[UNMODELLED_EXPOSURE_KEY] = share
        outcome.warnings.append(
            f"A {share} share of the position sits in unmodelled allocations whose "
            "exposure to this event is unknown; the impact covers only the modelled "
            "exposure and is incomplete."
        )

    def _data_confidence(
        self, graph: RiskGraph, view: _GraphView, outcome: _Outcome
    ) -> ConfidenceLevel:
        if outcome.missing or outcome.unmodelled > ZERO:
            return ConfidenceLevel.UNAVAILABLE
        freshness = {view.nodes[node_id].freshness for node_id in outcome.path}
        if FreshnessStatus.EXPIRED in freshness:
            return ConfidenceLevel.UNAVAILABLE
        if outcome.warnings:
            level = ConfidenceLevel.LOW
        elif (
            graph.completeness_warnings
            or view.warnings
            or FreshnessStatus.STALE_USABLE in freshness
        ):
            level = ConfidenceLevel.MEDIUM
        else:
            level = ConfidenceLevel.HIGH
        if outcome.bounded:
            # A worst-case bound stands in for missing borrower data: one level lower.
            level = _LOWER_CONFIDENCE[level]
        return level


def _short_market(view: _GraphView, market: str) -> str:
    market_id = view.nodes[market].metrics.get(MARKET_ID_KEY)
    key = market_id if isinstance(market_id, str) and market_id else market.rsplit(":", 1)[-1]
    return key.lower()[:10]


def _fmt(value: Decimal, places: int = 4) -> str:
    """Readable figure for explanations: fixed point, at most `places` decimals."""
    return _strip(f"{value.quantize(Decimal(1).scaleb(-places)):f}")


def _strip(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sig(value: Decimal, digits: int = 3) -> str:
    """Fixed point with `digits` significant digits (no scientific notation)."""
    if value == ZERO:
        return "0"
    exponent = min(0, value.adjusted() - digits + 1)
    return _strip(f"{value.quantize(Decimal(1).scaleb(exponent)):f}")


def _amount(value: Decimal) -> str:
    """Numeraire amounts: two decimals from 1 up, three significant digits below."""
    return _fmt(value, 2) if abs(value) >= ONE else _sig(value)


def _share(fraction: Decimal) -> str:
    """A fraction of the position as a percentage."""
    return f"{_amount(fraction * 100)}%"


def _compact(value: Decimal) -> str:
    """Large amounts as 412.3M / 1.2B for explanations."""
    for scale, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "k")):
        if abs(value) >= scale:
            return f"{_fmt(value / scale, 1)}{suffix}"
    return _amount(value)


def _loan_unit(view: _GraphView, market: str) -> str:
    """Suffix naming the market's loan asset (amounts are in its units); empty when unknown."""
    symbol = view.nodes[market].metrics.get("loan_symbol")
    return f" {symbol}" if isinstance(symbol, str) and symbol else ""


def _describe_bad_debt(estimate: BadDebtEstimate, unit: str) -> str:
    if estimate.shock <= ZERO:
        return (
            "the loan asset moves with the collateral (it is shocked too, or shares its "
            "underlying), so there is no relative fall and no bad debt"
        )
    shock = f"{_fmt(estimate.shock * 100, 2)}% collateral fall"
    lltv = f"LLTV {_fmt(estimate.lltv * 100, 2)}%" if estimate.lltv is not None else "LLTV unknown"
    fraction = f"supplier loss {_share(estimate.supplier_loss_fraction)}"
    debt = (
        f"{_compact(estimate.total_borrow)}{unit} debt" if estimate.total_borrow is not None else "debt"
    )
    bad = f"{_compact(estimate.bad_debt)}{unit}" if estimate.bad_debt is not None else "unknown"
    if estimate.method == METHOD_POSITIONS:
        text = (
            f"bad debt {bad} of {debt} at a {shock} ({lltv}); "
            f"{estimate.borrowers_underwater} of {estimate.borrower_count} borrowers below "
            "1.0 collateral/debt"
        )
        if estimate.uncovered_debt is not None and estimate.uncovered_debt > ZERO:
            text += (
                f", plus {_compact(estimate.uncovered_debt)}{unit} uncovered debt at the LLTV "
                "worst case"
            )
        return f"{text}; {fraction}"
    if estimate.method == METHOD_LLTV_UNKNOWN:
        return (
            f"worst-case bound at a {shock} with LLTV unknown (every unit of debt loses "
            f"the fall); {fraction}"
        )
    return (
        f"worst-case bound: borrower positions unavailable, all {debt} assumed at "
        f"{lltv}, bad debt {bad} at a {shock}; {fraction}"
    )


def _unavailable_probability(
    selection: ScenarioSelection, graph: RiskGraph
) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE,
        low=None,
        central=None,
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE,
        horizon_days=30,
        source=UNAVAILABLE_PROBABILITY_SOURCE,
        retrieved_at=graph.generated_at,
        freshness=FreshnessStatus.EXPIRED,
        explanation=(
            f"No 30-day probability estimate was available for "
            f"{selection.scenario_id.value}; none was assumed."
        ),
    )


def _explain(
    position: ScenarioPosition,
    definition: ScenarioDefinition,
    severity: Decimal,
    outcome: _Outcome,
    impact: Decimal,
    probability: ProbabilityEstimate,
) -> str:
    parts = [f"{definition.description} (severity {severity})."]
    if outcome.missing:
        parts.append(f"Impact unavailable: missing {', '.join(outcome.missing)}.")
    elif outcome.summary:
        parts.append(outcome.summary)
    parts.append(f"Permanent-loss impact is {impact} {position.numeraire}.")
    if outcome.components[LIQUIDITY_SHORTFALL] > ZERO:
        parts.append("The liquidity shortfall is not counted as a realized loss.")
    if outcome.hard_triggers:
        parts.append(f"Hard triggers: {', '.join(outcome.hard_triggers)}.")
    parts.extend(outcome.warnings)
    parts.extend(outcome.notes)
    if probability.method == ProbabilityMethod.UNAVAILABLE:
        parts.append("Probability unavailable.")
    return " ".join(parts)
