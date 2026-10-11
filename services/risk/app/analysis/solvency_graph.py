"""Annotate a lender's risk graph with the deterministic solvency model (`app.domain.solvency`).

The annotation runs after the scenario engine, so nothing here changes scenario
impacts or the STAY/EXIT/UNAVAILABLE recommendation: `exposure_weight` is left
untouched and the solvency propagation is carried by `RiskEdge.loss_share`.

Node metrics (all prefixed `solvency_`):
- market: `q` (collateral-token units in the liquidation zone), `q_bound`,
  `borrowers_in_zone`, `q_over_half_r`, `debt_issued`, `recovered_value`,
  `slippage_factor`, `bad_debt` (loan-token units), `supplier_loss_fraction`,
  `scenario` and `missing` (comma-separated inputs that made it unavailable);
  LLTV is the existing `lltv` metric.
- oracle: `p_contract`, `p_real`, `price_sources` (comma-separated) and
  `price_deviation` = (P_contract - P_real) / P_contract.
- pool `pool:1:<exit key>` (one per market with a collateral exit quote): `r`
  (collateral-token units), `spot_price` and `route`.
- vault: `bad_debt` (vault-asset units), `bad_debt_share`, `expected_loss`
  (the position's, in its numeraire) and `scenario`.
- position: `w_pool`, `expected_loss` (numeraire), `vulnerable`, `scenario`,
  `r_panic` (an assumption) and `model`; a borrower gets only
  `model = not_applicable` and a completeness warning.

Edge `loss_share`: position -> vault/market = W_pool; vault -> market
(`allocated_to`) = the vault's share of the market's supply; market -> oracle
and market -> pool = 1 when that price or depth is an input of the result.

Every external input keeps its own `MetricProvenance` (source, fetch time,
freshness, block); computed values are `derived` and R_panic is an
`assumption`. An expired input is treated as missing.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext

from app.contracts.models import (
    FreshnessStatus,
    MetricProvenance,
    MetricProvenanceKind,
    MetricValue,
    PositionMode,
    RiskEdge,
    RiskEdgeType,
    RiskGraph,
    RiskNode,
    RiskNodeType,
)
from app.domain.graph import CHAIN_ID
from app.domain.position import quantize_value
from app.domain.solvency import (
    PRECISION,
    R_PANIC,
    LenderSolvency,
    LiquidationZone,
    MarketSolvency,
    SolvencyScenario,
    VaultSolvency,
    lender_solvency,
    market_solvency,
    vault_solvency,
)


ZERO = Decimal(0)
ONE = Decimal(1)
MODEL_NAME = "lender_bad_debt"
NOT_APPLICABLE = "not_applicable"
MODEL_SOURCE = "risk0 solvency model"
ORACLE_SOURCE_NAME = "morpho_oracle"
UNISWAP_SOURCE_NAME = "uniswap_spot"
DEPTH_MODEL = "constant-product depth implied by the quote's price impact"
BORROWER_WARNING = (
    "The solvency (bad-debt) model is lender-only; it is not applicable to borrower positions."
)
DERIVED = MetricProvenance(
    kind=MetricProvenanceKind.DERIVED,
    source=f"{MODEL_SOURCE}: Q, R, P_contract, P_real and LLTV",
)
ASSUMPTION = MetricProvenance(
    kind=MetricProvenanceKind.ASSUMPTION,
    source=f"{MODEL_SOURCE}: recovery rate in a systemic collapse (configurable)",
)


@dataclass(frozen=True)
class Observed:
    """An external value with where it was read."""

    value: Decimal
    provenance: MetricProvenance

    @property
    def usable(self) -> bool:
        return self.value.is_finite() and self.provenance.freshness is not FreshnessStatus.EXPIRED


@dataclass(frozen=True)
class CollateralExit:
    """A Uniswap quote selling Q collateral for the loan asset.

    `depth` is R in collateral-token units (`Q * (1 - impact) / impact`) and
    `spot_price` the quote's marginal price gross of fees, loan per collateral.
    `token_ids` are the graph asset nodes the route trades.
    """

    key: str
    label: str
    route: tuple[str, ...]
    depth: Observed | None
    spot_price: Observed | None
    token_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketSolvencyInputs:
    market_id: str
    zone: LiquidationZone | None
    zone_provenance: MetricProvenance | None
    p_contract: Observed | None
    exit: CollateralExit | None
    warnings: tuple[str, ...] = ()


@dataclass
class _Market:
    result: MarketSolvency
    total_supply: Decimal | None


@dataclass
class _Annotations:
    metrics: dict[str, dict[str, MetricValue]] = field(default_factory=dict)
    provenance: dict[str, dict[str, MetricProvenance]] = field(default_factory=dict)
    loss_shares: dict[tuple[str, str, RiskEdgeType], Decimal | None] = field(default_factory=dict)
    nodes: list[RiskNode] = field(default_factory=list)
    edges: list[RiskEdge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def set(self, node_id: str, name: str, value: MetricValue, provenance: MetricProvenance | None = None) -> None:
        self.metrics.setdefault(node_id, {})[name] = value
        if provenance is not None:
            self.provenance.setdefault(node_id, {})[name] = provenance

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def annotate_solvency(
    graph: RiskGraph,
    inputs: Mapping[str, MarketSolvencyInputs],
    *,
    x_in_pool_units: Decimal | None,
    r_panic: Decimal = R_PANIC,
) -> RiskGraph:
    """Return `graph` with solvency metrics, provenance, loss shares and exit pool nodes.

    `x_in_pool_units` is the position amount in the asset of the pool it lends to
    (vault asset or market loan asset); None when it cannot be converted, which
    leaves W_pool unknown but not the expected loss (`X * loss_fraction`).
    """
    nodes = {node.node_id: node for node in graph.nodes}
    position = next(node for node in graph.nodes if node.node_type is RiskNodeType.POSITION)
    notes = _Annotations()
    if position.metrics.get("mode") != PositionMode.LENDER.value:
        notes.set(position.node_id, "solvency_model", NOT_APPLICABLE)
        notes.warn(BORROWER_WARNING)
        return _apply(graph, notes)

    keyed = {key.lower(): value for key, value in inputs.items()}
    markets: dict[str, _Market] = {}
    for node in graph.nodes:
        market_id = node.metrics.get("market_id")
        if node.node_type is RiskNodeType.MARKET and isinstance(market_id, str):
            markets[node.node_id] = _annotate_market(
                graph, nodes, node, keyed.get(market_id.lower()), r_panic, notes
            )

    x = position.metrics.get("amount")
    if not isinstance(x, Decimal):
        notes.warn("Solvency model unavailable: the position amount is unknown.")
        notes.set(position.node_id, "solvency_scenario", SolvencyScenario.UNAVAILABLE.value)
        return _apply(graph, notes)
    target = next(
        (edge for edge in graph.edges if edge.source == position.node_id and edge.relationship is RiskEdgeType.LENDS),
        None,
    )
    lender: LenderSolvency
    if target is not None and target.target in markets:
        market = markets[target.target]
        lender = lender_solvency(
            x=x,
            x_in_pool_units=x_in_pool_units,
            pool_total=market.total_supply,
            loss_fraction=market.result.supplier_loss_fraction,
            scenario=market.result.scenario,
        )
    elif target is not None and nodes[target.target].node_type is RiskNodeType.VAULT:
        vault = _annotate_vault(graph, nodes, nodes[target.target], markets, notes)
        lender = lender_solvency(
            x=x,
            x_in_pool_units=x_in_pool_units,
            pool_total=vault.total_assets,
            loss_fraction=vault.loss_fraction,
            scenario=vault.scenario,
        )
        notes.set(target.target, "solvency_expected_loss", _loss(lender.expected_loss), DERIVED)
    else:
        lender = lender_solvency(
            x=x, x_in_pool_units=None, pool_total=None, loss_fraction=None,
            scenario=SolvencyScenario.UNAVAILABLE,
        )
    if target is not None:
        notes.loss_shares[(target.source, target.target, target.relationship)] = _share(lender.w_pool)
    _annotate_position(position, lender, r_panic, x_in_pool_units, notes)
    return _apply(graph, notes)


# Markets


def _annotate_market(
    graph: RiskGraph,
    nodes: Mapping[str, RiskNode],
    node: RiskNode,
    inputs: MarketSolvencyInputs | None,
    r_panic: Decimal,
    notes: _Annotations,
) -> _Market:
    lltv = _decimal(node.metrics.get("lltv"))
    total_supply = _decimal(node.metrics.get("total_supply_assets"))
    zone = None
    p_contract = None
    exit_ = None
    if inputs is not None:
        for warning in inputs.warnings:
            notes.warn(warning)
        if inputs.zone is not None and (
            inputs.zone_provenance is None or inputs.zone_provenance.freshness is not FreshnessStatus.EXPIRED
        ):
            zone = inputs.zone
        p_contract = inputs.p_contract if inputs.p_contract is not None and inputs.p_contract.usable else None
        exit_ = inputs.exit
    depth = exit_.depth if exit_ is not None and exit_.depth is not None and exit_.depth.usable else None
    sources = tuple(
        (name, observed)
        for name, observed in (
            (ORACLE_SOURCE_NAME, p_contract),
            (UNISWAP_SOURCE_NAME, exit_.spot_price if exit_ is not None else None),
        )
        if observed is not None and observed.usable and observed.value > ZERO
    )
    p_real = min((observed.value for _, observed in sources), default=None)
    result = market_solvency(
        q=zone.collateral if zone is not None else None,
        r=depth.value if depth is not None else None,
        lltv=lltv,
        p_contract=p_contract.value if p_contract is not None else None,
        p_real=p_real,
        total_supply=total_supply,
        r_panic=r_panic,
    )

    label = node.label
    nid = node.node_id
    notes.set(nid, "solvency_scenario", result.scenario.value, DERIVED)
    notes.set(
        nid,
        "solvency_q",
        _value(result.q),
        inputs.zone_provenance if inputs is not None and zone is not None else None,
    )
    notes.set(nid, "solvency_q_bound", zone.bound if zone is not None else None)
    notes.set(nid, "solvency_borrowers_in_zone", Decimal(zone.borrowers_in_zone) if zone is not None else None)
    for name, value in (
        ("solvency_q_over_half_r", _value(result.q_over_half_r)),
        ("solvency_debt_issued", _loss(result.debt_issued)),
        ("solvency_recovered_value", _value(result.recovered_value)),
        ("solvency_slippage_factor", _value(result.slippage_factor)),
        ("solvency_bad_debt", _loss(result.bad_debt)),
        ("solvency_supplier_loss_fraction", _loss(result.supplier_loss_fraction)),
    ):
        notes.set(nid, name, value, DERIVED)
    notes.set(nid, "solvency_missing", ",".join(result.missing) or None)
    if not result.available:
        notes.warn(
            f"Solvency model unavailable for {label}: missing or expired {', '.join(result.missing)}."
        )
    if zone is not None and zone.bound:
        notes.warn(
            f"Solvency Q for {label} includes {quantize_value(zone.uncovered_debt)} of borrower debt "
            "not covered by the position list, priced at the worst case at LLTV."
        )

    prices_used = result.scenario is SolvencyScenario.B_DEGRADED and (result.q or ZERO) > ZERO
    oracle_id = _oracle_node(graph, nodes, node)
    if oracle_id is not None:
        if p_contract is not None:
            notes.set(oracle_id, "solvency_p_contract", _value(p_contract.value), p_contract.provenance)
        if p_real is not None:
            notes.set(oracle_id, "solvency_p_real", _value(p_real), DERIVED)
            notes.set(oracle_id, "solvency_price_sources", ",".join(name for name, _ in sources))
        if p_contract is not None and p_real is not None and p_contract.value > ZERO:
            with localcontext() as context:
                context.prec = PRECISION
                deviation = (p_contract.value - p_real) / p_contract.value
            notes.set(oracle_id, "solvency_price_deviation", _value(deviation), DERIVED)
        notes.loss_shares[(nid, oracle_id, RiskEdgeType.USES_ORACLE)] = ONE if prices_used else None
    if len(sources) == 1 and prices_used:
        notes.warn(
            f"Solvency P_real for {label} rests on one price source ({sources[0][0]}); low confidence."
        )
    if exit_ is not None:
        _add_exit_pool(nid, exit_, depth is not None and (result.q or ZERO) > ZERO, nodes, notes)
    return _Market(result, total_supply)


def _oracle_node(graph: RiskGraph, nodes: Mapping[str, RiskNode], market: RiskNode) -> str | None:
    address = market.metrics.get("oracle_address")
    for edge in graph.edges:
        if edge.source == market.node_id and edge.relationship is RiskEdgeType.USES_ORACLE:
            target = nodes[edge.target]
            if target.node_type is RiskNodeType.ORACLE and target.metrics.get("address") == address:
                return edge.target
    return None


def _add_exit_pool(
    market_id: str,
    exit_: CollateralExit,
    used: bool,
    nodes: Mapping[str, RiskNode],
    notes: _Annotations,
) -> None:
    pool_id = f"{RiskNodeType.POOL.value}:{CHAIN_ID}:{exit_.key.lower()}"
    if pool_id not in nodes and all(node.node_id != pool_id for node in notes.nodes):
        anchor = exit_.depth or exit_.spot_price
        provenance = anchor.provenance if anchor is not None else None
        metrics: dict[str, MetricValue] = {
            "solvency_r": _value(exit_.depth.value) if exit_.depth is not None else None,
            "solvency_spot_price": _value(exit_.spot_price.value) if exit_.spot_price is not None else None,
            "solvency_route": " -> ".join(exit_.route),
            "solvency_depth_model": DEPTH_MODEL,
        }
        metric_provenance = {
            name: observed.provenance
            for name, observed in (("solvency_r", exit_.depth), ("solvency_spot_price", exit_.spot_price))
            if observed is not None
        }
        notes.nodes.append(
            RiskNode(
                node_id=pool_id,
                node_type=RiskNodeType.POOL,
                label=exit_.label,
                metrics=metrics,
                provenance=provenance.source if provenance is not None else MODEL_SOURCE,
                freshness=(provenance.freshness if provenance is not None and provenance.freshness else FreshnessStatus.EXPIRED),
                observed_at=_observed_at(provenance, nodes[market_id]),
                metric_provenance=metric_provenance,
            )
        )
        for token_id in exit_.token_ids:
            if token_id in nodes:
                notes.edges.append(
                    RiskEdge(source=pool_id, target=token_id, relationship=RiskEdgeType.EXITS_THROUGH)
                )
    notes.edges.append(
        RiskEdge(
            source=market_id,
            target=pool_id,
            relationship=RiskEdgeType.EXITS_THROUGH,
            loss_share=ONE if used else None,
        )
    )


def _observed_at(provenance: MetricProvenance | None, fallback: RiskNode):
    if provenance is not None and provenance.fetched_at is not None:
        return provenance.fetched_at
    return fallback.observed_at


# Vault and position


def _annotate_vault(
    graph: RiskGraph,
    nodes: Mapping[str, RiskNode],
    vault: RiskNode,
    markets: Mapping[str, _Market],
    notes: _Annotations,
) -> VaultSolvency:
    holdings: list[tuple[Decimal, MarketSolvency]] = []
    unmodelled = False
    allocations = [
        edge
        for edge in graph.edges
        if edge.source == vault.node_id and edge.relationship is RiskEdgeType.ALLOCATED_TO
    ]
    if not allocations:
        unmodelled = True
        notes.warn(f"Solvency model unavailable for {vault.label}: its allocations are unknown.")
    for edge in allocations:
        market = markets.get(edge.target)
        supply = _decimal(nodes[edge.target].metrics.get("allocated_assets"))
        if market is None or supply is None:
            if edge.exposure_weight is None or edge.exposure_weight > ZERO:
                unmodelled = True
                notes.warn(
                    f"Solvency model unavailable for {vault.label}: allocation to "
                    f"{nodes[edge.target].label} is not a modelled Morpho market."
                )
            continue
        holdings.append((supply, market.result))
        share = None
        if market.total_supply is not None and market.total_supply > ZERO:
            with localcontext() as context:
                context.prec = PRECISION
                share = supply / market.total_supply
            if share > ONE:
                notes.warn(
                    f"{vault.label} supplies {supply} to {nodes[edge.target].label}, more than its "
                    f"reported total supply {market.total_supply}; its loss share was capped at 1."
                )
        notes.loss_shares[(edge.source, edge.target, edge.relationship)] = _share(share)
    result = vault_solvency(
        total_assets=_decimal(vault.metrics.get("total_assets")),
        holdings=holdings,
        unmodelled=unmodelled,
    )
    notes.set(vault.node_id, "solvency_scenario", result.scenario.value, DERIVED)
    notes.set(vault.node_id, "solvency_bad_debt", _loss(result.bad_debt), DERIVED)
    notes.set(vault.node_id, "solvency_bad_debt_share", _loss(result.loss_fraction), DERIVED)
    return result


def _annotate_position(
    position: RiskNode,
    lender: LenderSolvency,
    r_panic: Decimal,
    x_in_pool_units: Decimal | None,
    notes: _Annotations,
) -> None:
    nid = position.node_id
    notes.set(nid, "solvency_model", MODEL_NAME)
    notes.set(nid, "solvency_scenario", lender.scenario.value, DERIVED)
    notes.set(nid, "solvency_w_pool", _value(lender.w_pool), DERIVED)
    notes.set(nid, "solvency_expected_loss", _loss(lender.expected_loss), DERIVED)
    notes.set(nid, "solvency_vulnerable", lender.vulnerable, DERIVED)
    notes.set(nid, "solvency_r_panic", r_panic, ASSUMPTION)
    if lender.scenario is SolvencyScenario.UNAVAILABLE:
        notes.warn(
            "Solvency model unavailable for this position: a market input is missing or "
            "expired (see the market nodes)."
        )
    if x_in_pool_units is None:
        notes.warn(
            "W_pool is unknown: the position amount could not be expressed in the pool's asset."
        )


# Helpers


def _apply(graph: RiskGraph, notes: _Annotations) -> RiskGraph:
    nodes = []
    for node in graph.nodes:
        metrics = notes.metrics.get(node.node_id)
        if metrics is None:
            nodes.append(node)
            continue
        nodes.append(
            node.model_copy(
                update={
                    "metrics": {**node.metrics, **metrics},
                    "metric_provenance": {
                        **node.metric_provenance,
                        **notes.provenance.get(node.node_id, {}),
                    },
                }
            )
        )
    edges = [
        edge.model_copy(update={"loss_share": notes.loss_shares[key]})
        if (key := (edge.source, edge.target, edge.relationship)) in notes.loss_shares
        else edge
        for edge in graph.edges
    ]
    existing = {(edge.source, edge.target, edge.relationship) for edge in edges}
    for edge in notes.edges:
        key = (edge.source, edge.target, edge.relationship)
        if key not in existing:
            existing.add(key)
            edges.append(edge)
    return graph.model_copy(
        update={
            "nodes": (*nodes, *notes.nodes),
            "edges": tuple(edges),
            "completeness_warnings": tuple(
                dict.fromkeys([*graph.completeness_warnings, *notes.warnings])
            ),
        }
    )


def _decimal(value: MetricValue) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _value(value: Decimal | None) -> Decimal | None:
    return None if value is None else quantize_value(value, ROUND_DOWN)


def _loss(value: Decimal | None) -> Decimal | None:
    """Losses round up so the reported figure never understates the model."""
    return None if value is None else quantize_value(value, ROUND_UP)


def _share(value: Decimal | None) -> Decimal | None:
    return None if value is None else min(ONE, quantize_value(value, ROUND_DOWN))
