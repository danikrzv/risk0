"""Gather the solvency model's per-market inputs from the live providers.

For each market: Q from Morpho borrower positions (`liquidation_zone`), the
oracle's `price()` as P_contract, and one Uniswap quote selling Q collateral
for the loan asset. The quote gives both R, as the constant-product depth
implied by its price impact `i` (`R = Q * (1 - i) / i`, so that
`R / (R + Q) = 1 - i`), and a Uniswap spot price (the marginal rate gross of
pool fees), the second P_real source. The Morpho oracle and the Uniswap spot
are the price sources in scope; an independent Chainlink feed would need a
collateral-to-feed mapping and is not used yet.

Provider failures are logged by type only and become missing inputs (and so
an unavailable result), never a guessed value. Every source is reduced with
`public_source` so credentials in a URL never reach the graph.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_UP, Decimal, localcontext

from app.adapters.morpho import MarketPositions
from app.analysis.graph_builder import public_source
from app.analysis.solvency_graph import CollateralExit, MarketSolvencyInputs, Observed
from app.contracts.models import FreshnessStatus, MetricProvenance, MetricProvenanceKind, RiskNodeType
from app.domain.exit import ExitQuote, QuoteRequest
from app.domain.graph import OracleObservation, node_id
from app.domain.position import quantize_token_amount
from app.domain.snapshots import DataEnvelope, MarketSnapshot
from app.domain.solvency import PRECISION, liquidation_zone


logger = logging.getLogger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)

PositionsFetcher = Callable[[str], DataEnvelope[MarketPositions] | None]
QuoteFetcher = Callable[[QuoteRequest], DataEnvelope[ExitQuote]]


def provenance(envelope: DataEnvelope) -> MetricProvenance:
    return MetricProvenance(
        kind=MetricProvenanceKind.DATA,
        source=public_source(envelope.source),
        fetched_at=envelope.fetched_at,
        freshness=envelope.freshness,
        block_number=envelope.block_number,
    )


def gather_solvency_inputs(
    markets: Sequence[MarketSnapshot],
    *,
    positions: PositionsFetcher,
    oracles: Mapping[str, DataEnvelope[OracleObservation]],
    quote: QuoteFetcher | None,
) -> dict[str, MarketSolvencyInputs]:
    return {
        market.market_id.lower(): _market_inputs(market, positions, oracles, quote)
        for market in markets
    }


def _market_inputs(
    market: MarketSnapshot,
    positions: PositionsFetcher,
    oracles: Mapping[str, DataEnvelope[OracleObservation]],
    quote: QuoteFetcher | None,
) -> MarketSolvencyInputs:
    warnings: list[str] = []
    name = f"{market.collateral_asset.symbol}/{market.loan_asset.symbol}"

    p_contract = None
    oracle = oracles.get(market.oracle_address.lower())
    if oracle is not None and oracle.value.price is not None and oracle.value.price > ZERO:
        p_contract = Observed(oracle.value.price, provenance(oracle))

    zone = None
    zone_provenance = None
    envelope = positions(market.market_id.lower())
    if envelope is not None and envelope.freshness is not FreshnessStatus.EXPIRED:
        zone_provenance = provenance(envelope)
        zone = liquidation_zone(
            (
                (item.collateral_assets, item.borrow_assets, item.health_factor)
                for item in envelope.value.positions
            ),
            lltv=market.lltv,
            complete=envelope.value.complete,
            total_borrow=market.total_borrow_assets,
            p_contract=p_contract.value if p_contract is not None else None,
        )
        if zone is None:
            warnings.append(
                f"Solvency Q for {name} cannot be bounded: the borrower list is incomplete and "
                "the oracle price or total borrow needed to price the rest is unknown."
            )

    exit_ = None
    if zone is not None and zone.collateral > ZERO:
        exit_ = _collateral_exit(market, zone.collateral, quote, warnings)
    return MarketSolvencyInputs(
        market_id=market.market_id.lower(),
        zone=zone,
        zone_provenance=zone_provenance,
        p_contract=p_contract,
        exit=exit_,
        warnings=tuple(warnings),
    )


def _collateral_exit(
    market: MarketSnapshot, q: Decimal, quote: QuoteFetcher | None, warnings: list[str]
) -> CollateralExit | None:
    collateral, loan = market.collateral_asset, market.loan_asset
    name = f"{collateral.symbol} -> {loan.symbol}"
    if quote is None:
        warnings.append(f"Solvency R for {name} is unknown: no Uniswap quote provider is configured.")
        return None
    # Round Q up to the token's smallest unit: a larger sale can only show more impact.
    amount = quantize_token_amount(q, collateral.decimals, ROUND_UP)
    try:
        envelope = quote(QuoteRequest(token_in=collateral, token_out=loan, amount_in=amount))
    except Exception as error:  # provider boundary: never leak provider text
        logger.info("solvency collateral quote unavailable: %s", type(error).__name__)
        warnings.append(f"Solvency R for {name} is unknown: the Uniswap quote could not be obtained.")
        return None
    result = envelope.value
    source = provenance(envelope)
    with localcontext() as context:
        context.prec = PRECISION
        depth = None
        impact = result.price_impact
        if impact is not None and ZERO < impact < ONE:
            depth = Observed(amount * (ONE - impact) / impact, source)
        else:
            warnings.append(
                f"Solvency R for {name} is unknown: the quote's price impact "
                f"({impact if impact is not None else 'unavailable'}) does not imply a depth."
            )
        spot = None
        if result.marginal_amount_out is not None and result.fee_fraction < ONE:
            spot = Observed(
                result.marginal_amount_out / (ONE - result.fee_fraction) / result.amount_in, source
            )
    return CollateralExit(
        key=f"collateral-exit-{collateral.address.lower()}-{loan.address.lower()}",
        label=f"{name} collateral exit (Uniswap)",
        route=tuple(result.route.symbols),
        depth=depth,
        spot_price=spot,
        token_ids=tuple(node_id(RiskNodeType.ASSET, address) for address in result.route.path),
    )
