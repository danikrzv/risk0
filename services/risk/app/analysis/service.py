"""Analysis orchestration: catalog -> snapshots -> position -> graph -> scenarios -> exit -> rule.

`AnalysisService.analyze` runs the whole pipeline for one request. Every external
datum comes through an injectable interface below, so tests use fakes and need
no network. Outcomes:

- Request problems (unknown entity, inactive entry, unsupported mode, invalid
  severity, a position that fails Task 5 validation, an entity Morpho no longer
  resolves) raise `AnalysisValidationError` -> HTTP 422 with a sanitized message.
  An inactive or unresolvable entry is a 422 rather than an UNAVAILABLE
  response: no graph can be built for it, and the catalog already shows it as
  analysis-disabled.
- The entity's own Morpho snapshot being unavailable (no usable cache) or
  internally inconsistent raises `AnalysisProviderError` -> HTTP 503. Nothing can
  be analysed without it.
- Every other provider gap (allocations, allocated markets, live oracle health,
  history, protocol evidence, gas, prices, Uniswap quote) degrades into an
  explicit UNAVAILABLE probability, an unknown-exposure node, or a missing
  critical source, and the response is a full 200 contract whose recommendation
  is UNAVAILABLE whenever the gap matters. Missing data never becomes STAY.

Provider exception text is never forwarded: logs record only the exception type.

Critical sources (absent or expired -> UNAVAILABLE): the entity snapshot, vault
allocations (vault entries), every live oracle observation that was supplied,
and the exit estimate. A quote older than 30 s or more than 20 blocks from the
snapshot is refreshed once; if it is still expired the exit estimate is expired.

Probabilities (`HybridProbabilityProvider`):
- stablecoin depeg and collateral shock: empirical rolling 30-day windows over
  hourly Chainlink history (Task 7) for each shocked asset that has a feed:
  USDC -> USDC/USD, WETH -> ETH/USD. Assets without a feed (EURCV has no EURCV/EUR
  feed, wstETH's risk is its ETH ratio, WBTC has no BTC feed in the catalog) make
  the estimate UNAVAILABLE; it is never approximated by an unrelated feed.
- protocol impairment: model-estimated from protocol evidence (Task 8).
- oracle failure and liquidity collapse: UNAVAILABLE. Hourly history cannot
  resolve the 30-minute staleness threshold, and no comparison-market or pool
  depth history is available. Live oracle breaches still act as hard triggers.
"""

import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.adapters.morpho import MorphoDataConflictError, MorphoError, MorphoNotFoundError
from app.analysis.exit_cost import (
    USD_NUMERAIRES,
    USD_STABLE_SYMBOLS,
    ExitCostService,
    ExitCostUnavailableError,
)
from app.analysis.graph_builder import build_risk_graph
from app.analysis.recommendation import (
    CONFIDENCE_ORDER,
    LIVE_ORACLE_FAILURE,
    PROTOCOL_EMERGENCY,
    make_recommendation,
)
from app.analysis.scenario_engine import ScenarioEngine
from app.contracts.models import (
    AnalysisRequest,
    AnalysisResponse,
    CatalogEntityType,
    CatalogEntry,
    ConfidenceLevel,
    ExitEstimate,
    FreshnessStatus,
    PositionMode,
    ProbabilityEstimate,
    ProbabilityMethod,
    RiskEdgeType,
    RiskGraph,
    RiskNodeType,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.exit import NUMERAIRE_PLACES, ExitQuote, GasAssumptions, QuoteRequest
from app.domain.graph import GraphConstructionError, GraphContext, OracleObservation, node_id
from app.domain.position import PositionValidationError, ValidatedPosition, validate_position
from app.domain.scenario import (
    ORACLE_DEVIATION_THRESHOLD,
    ORACLE_STALE_THRESHOLD_SECONDS,
    SCENARIO_DEFINITIONS,
    ScenarioSeverityError,
    validate_selection,
)
from app.domain.snapshots import (
    AllocationSnapshot,
    AssetInfo,
    DataEnvelope,
    MarketSnapshot,
    VaultSnapshot,
)
from app.evidence.models import CombinedEvidence
from app.history.estimator import EmpiricalProbabilityEvidence, EventPredicate
from app.history.normalization import HOUR, Observation
from app.infrastructure.freshness import DataConflict, DataQuality, Freshness


logger = logging.getLogger("risk0.analysis")

ZERO = Decimal(0)
ONE = Decimal(1)
MORPHO_PROTOCOL = "morpho"
UNAVAILABLE_SOURCE = "risk0:analysis"
HISTORY_LOOKBACK = timedelta(days=365)
COLLATERAL_SHOCK_HOURS = 24
# Collateral sold for a borrower exit: enough to repay the debt plus this margin
# for fees and slippage, capped at the collateral held.
BORROWER_SALE_MARGIN = Decimal("0.01")
USDC_ADDRESS = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ETH_SYMBOLS = frozenset({"weth", "eth"})
DEFAULT_STABLECOIN_FEEDS: Mapping[str, tuple[str, ...]] = {"USDC": ("USDC/USD",)}
DEFAULT_COLLATERAL_FEEDS: Mapping[str, tuple[str, ...]] = {"WETH": ("ETH/USD",)}

SNAPSHOT_SOURCE = "Morpho entity snapshot"
ALLOCATIONS_SOURCE = "Morpho vault allocations"
EXIT_SOURCE = "exit estimate"


# Errors


class AnalysisError(Exception):
    """Base error whose `public_message` is safe to return to a client."""

    status_code = 422

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class AnalysisValidationError(AnalysisError):
    status_code = 422


class AnalysisProviderError(AnalysisError):
    status_code = 503


# Provider interfaces


class CatalogSource(Protocol):
    def list_entries(self) -> tuple[CatalogEntry, ...]: ...


class MorphoSource(Protocol):
    def get_market(self, market_id: str) -> DataEnvelope[MarketSnapshot]: ...

    def get_vault(self, address: str, version: str) -> DataEnvelope[VaultSnapshot]: ...

    def get_allocations(
        self, address: str, version: str
    ) -> DataEnvelope[tuple[AllocationSnapshot, ...]]: ...


@dataclass(frozen=True, slots=True)
class ProbabilityAssessment:
    estimate: ProbabilityEstimate
    conflicts: tuple[DataConflict, ...] = ()


class ProbabilityProvider(Protocol):
    def assess(
        self,
        selection: ScenarioSelection,
        graph: RiskGraph | None,
        position: ValidatedPosition | None,
        *,
        now: datetime,
    ) -> ProbabilityAssessment: ...


class QuoteProvider(Protocol):
    def quote(self, request: QuoteRequest, *, refresh: bool) -> DataEnvelope[ExitQuote]: ...


class GasPriceProvider(Protocol):
    def gas_price_wei(self) -> Decimal: ...


class PriceProvider(Protocol):
    def price_usd(self, symbol: str) -> Decimal | None: ...


class OracleHealthProvider(Protocol):
    def observe(
        self, market: MarketSnapshot, *, now: datetime
    ) -> DataEnvelope[OracleObservation] | None: ...


class IncidentProvider(Protocol):
    def active_incidents(self, protocol: str) -> tuple[str, ...]: ...


class EmpiricalHistory(Protocol):
    """`HistoricalProbabilityService.estimate` (Task 7)."""

    def estimate(
        self,
        feed: str,
        start: datetime,
        end: datetime,
        event: EventPredicate,
        *,
        now: datetime,
    ) -> EmpiricalProbabilityEvidence: ...


class ProtocolEvidenceSource(Protocol):
    def evidence(self, protocol: str, *, now: datetime) -> CombinedEvidence: ...


class NoIncidents:
    """MVP default: no live incident feed exists, so no emergency is reported."""

    def active_incidents(self, protocol: str) -> tuple[str, ...]:
        return ()


# Service


@dataclass
class _Snapshots:
    primary: DataEnvelope[MarketSnapshot] | DataEnvelope[VaultSnapshot]
    market: DataEnvelope[MarketSnapshot] | None
    vault: DataEnvelope[VaultSnapshot] | None
    allocations: DataEnvelope[tuple[AllocationSnapshot, ...]] | None
    allocation_markets: dict[str, DataEnvelope[MarketSnapshot]]
    unresolved_markets: tuple[str, ...]


class AnalysisService:
    def __init__(
        self,
        catalog: CatalogSource,
        morpho: MorphoSource,
        *,
        probabilities: ProbabilityProvider,
        quotes: QuoteProvider | None,
        gas: GasPriceProvider | None,
        prices: PriceProvider | None = None,
        oracles: OracleHealthProvider | None = None,
        incidents: IncidentProvider | None = None,
        engine: ScenarioEngine | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._catalog = catalog
        self._morpho = morpho
        self._probabilities = probabilities
        self._quotes = quotes
        self._gas = gas
        self._prices = prices
        self._oracles = oracles
        self._incidents = incidents or NoIncidents()
        self._engine = engine or ScenarioEngine()
        self._clock = clock
        self._exit_costs = ExitCostService(clock=clock)

    def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        now = self._clock()
        entry = self._entry(request)
        for selection in request.scenarios:
            try:
                validate_selection(selection)
            except ScenarioSeverityError as error:
                raise AnalysisValidationError(str(error)) from None

        snapshots = self._snapshots(entry)
        position = self._validate(request, entry, snapshots, now)
        sources: list[tuple[str, Freshness]] = [
            (SNAPSHOT_SOURCE, Freshness(snapshots.primary.freshness.value))
        ]
        critical = [SNAPSHOT_SOURCE, EXIT_SOURCE]
        if snapshots.vault is not None:
            critical.append(ALLOCATIONS_SOURCE)
            if snapshots.allocations is not None:
                sources.append(
                    (ALLOCATIONS_SOURCE, Freshness(snapshots.allocations.freshness.value))
                )

        oracles = self._observe_oracles(snapshots, now)
        for address, observation in oracles.items():
            label = f"live oracle {address}"
            critical.append(label)
            sources.append((label, Freshness(observation.freshness.value)))

        graph = self._graph(position, entry, snapshots, oracles, now)
        conflicts: list[DataConflict] = []
        probabilities: dict[str, ProbabilityEstimate] = {}
        for selection in request.scenarios:
            # The engine takes one probability per scenario id; when an id is
            # selected at several severities the highest estimate is kept.
            key = selection.scenario_id.value
            assessment = self._assess(selection, graph, position, now)
            current = probabilities.get(key)
            if current is None or _higher(assessment.estimate, current):
                probabilities[key] = assessment.estimate
            conflicts.extend(assessment.conflicts)
        for key, probability in probabilities.items():
            sources.append((f"probability {key}", Freshness(probability.freshness.value)))

        try:
            scenarios = self._engine.run(position, graph, request.scenarios, probabilities)
        except (ValueError, ScenarioSeverityError) as error:
            logger.warning("scenario propagation failed: %s", type(error).__name__)
            raise AnalysisProviderError(
                "Provider data could not be propagated through the risk graph."
            ) from None

        exit_estimate = self._exit(position, snapshots, now)
        if exit_estimate.source != UNAVAILABLE_SOURCE:
            sources.append((EXIT_SOURCE, Freshness(exit_estimate.freshness.value)))

        quality = DataQuality.from_sources(sources, critical, conflicts)
        recommendation = make_recommendation(
            scenarios,
            exit_estimate,
            quality,
            live_triggers=self._live_triggers(oracles),
            numeraire=position.numeraire,
        )
        return AnalysisResponse(
            graph=graph,
            scenario_results=scenarios,
            exit_estimate=exit_estimate,
            recommendation=recommendation,
            analyzed_at=now,
        )

    # Catalog and snapshots

    def _entry(self, request: AnalysisRequest) -> CatalogEntry:
        entity_id = request.position.entity_id
        entry = next(
            (item for item in self._catalog.list_entries() if item.entity_id == entity_id),
            None,
        )
        if entry is None:
            raise AnalysisValidationError("Unknown entity_id: it is not in the curated catalog.")
        if not entry.supported:
            raise AnalysisValidationError(
                "This catalog entry is inactive; analysis is disabled until the catalog is updated."
            )
        if request.position.mode not in entry.supported_modes:
            raise AnalysisValidationError(
                f"Mode {request.position.mode.value!r} is not supported by this catalog entry."
            )
        return entry

    def _snapshots(self, entry: CatalogEntry) -> _Snapshots:
        identifier = entry.address_or_market_id
        try:
            if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
                market = self._morpho.get_market(identifier)
                return _Snapshots(market, market, None, None, {}, ())
            version = "v2" if entry.entity_type is CatalogEntityType.MORPHO_VAULT_V2 else "v1"
            vault = self._morpho.get_vault(identifier, version)
        except MorphoNotFoundError:
            raise AnalysisValidationError(
                "Morpho no longer resolves this catalog entry; analysis is disabled."
            ) from None
        except MorphoDataConflictError:
            logger.warning("morpho snapshot conflict for %s", entry.entity_id)
            raise AnalysisProviderError(
                "Market data sources conflict for this entry; analysis is unavailable."
            ) from None
        except MorphoError as error:
            logger.warning("morpho snapshot unavailable: %s", type(error).__name__)
            raise AnalysisProviderError(
                "Market data provider is unavailable and no usable cached data exists."
            ) from None

        try:
            allocations = self._morpho.get_allocations(identifier, version)
        except MorphoError as error:
            logger.warning("morpho allocations unavailable: %s", type(error).__name__)
            allocations = None

        markets: dict[str, DataEnvelope[MarketSnapshot]] = {}
        unresolved: list[str] = []
        if allocations is not None:
            wanted = sorted(
                {
                    row.market_id.lower()
                    for row in allocations.value
                    if row.market_id is not None and row.weight > ZERO
                }
            )
            for market_id in wanted:
                try:
                    markets[market_id] = self._morpho.get_market(market_id)
                except MorphoError as error:
                    logger.info("allocated market unavailable: %s", type(error).__name__)
                    unresolved.append(market_id)
        return _Snapshots(vault, None, vault, allocations, markets, tuple(unresolved))

    def _validate(
        self,
        request: AnalysisRequest,
        entry: CatalogEntry,
        snapshots: _Snapshots,
        now: datetime,
    ) -> ValidatedPosition:
        try:
            return validate_position(
                request.position,
                entry,
                snapshots.market.value if snapshots.market is not None else None,
                vault=snapshots.vault.value if snapshots.vault is not None else None,
                now=now,
            )
        except PositionValidationError as error:
            raise AnalysisValidationError(f"{error.field}: {error.message}") from None

    def _observe_oracles(
        self, snapshots: _Snapshots, now: datetime
    ) -> dict[str, DataEnvelope[OracleObservation]]:
        if self._oracles is None:
            return {}
        markets = [snapshots.market] if snapshots.market is not None else []
        markets.extend(snapshots.allocation_markets.values())
        observed: dict[str, DataEnvelope[OracleObservation]] = {}
        for envelope in markets:
            market = envelope.value
            key = market.oracle_address.lower()
            if key in observed:
                continue
            try:
                observation = self._oracles.observe(market, now=now)
            except Exception as error:  # provider boundary: never leak provider text
                logger.warning("oracle health unavailable: %s", type(error).__name__)
                continue
            if observation is not None:
                observed[key] = observation
        return observed

    def _graph(
        self,
        position: ValidatedPosition,
        entry: CatalogEntry,
        snapshots: _Snapshots,
        oracles: Mapping[str, DataEnvelope[OracleObservation]],
        now: datetime,
    ) -> RiskGraph:
        context = GraphContext(
            entry=entry,
            market=snapshots.market,
            vault=snapshots.vault,
            allocations=snapshots.allocations,
            allocation_markets=snapshots.allocation_markets,
            oracles=oracles,
        )
        try:
            graph = build_risk_graph(position, context, clock=lambda: now)
        except GraphConstructionError as error:
            logger.warning("graph construction failed: %s", type(error).__name__)
            raise AnalysisProviderError(
                "Provider data for this entry is inconsistent; analysis is unavailable."
            ) from None
        return _mark_unresolved_markets(graph, snapshots.unresolved_markets)

    def _assess(
        self,
        selection: ScenarioSelection,
        graph: RiskGraph,
        position: ValidatedPosition,
        now: datetime,
    ) -> ProbabilityAssessment:
        try:
            return self._probabilities.assess(selection, graph, position, now=now)
        except Exception as error:  # provider boundary: never leak provider text
            logger.warning("probability estimate failed: %s", type(error).__name__)
            return ProbabilityAssessment(
                unavailable_estimate(
                    now, f"The probability provider failed for {selection.scenario_id.value}."
                )
            )

    # Exit

    def _exit(
        self, position: ValidatedPosition, snapshots: _Snapshots, now: datetime
    ) -> ExitEstimate:
        try:
            return self._estimate_exit(position, snapshots, now)
        except ExitCostUnavailableError as error:
            return _unavailable_exit(now, f"Exit cost unavailable: {error}.")
        except _ExitProviderError as error:
            return _unavailable_exit(now, error.public_message)

    def _estimate_exit(
        self, position: ValidatedPosition, snapshots: _Snapshots, now: datetime
    ) -> ExitEstimate:
        envelope = snapshots.primary
        assets = _known_assets(snapshots)
        request = self._quote_request(position, envelope.value, assets)
        quote = self._quote(request, refresh=False) if request is not None else None
        gas = self._gas_assumptions(position.numeraire, assets, quote)
        estimate = self._estimate_with(position, envelope, quote, gas)
        if request is not None and estimate.freshness is FreshnessStatus.EXPIRED:
            quote = self._quote(request, refresh=True)
            refreshed = self._estimate_with(position, envelope, quote, gas)
            note = (
                "The quote was expired or too far from the snapshot block and was refreshed once"
                + ("." if refreshed.freshness is not FreshnessStatus.EXPIRED else "; it is still expired.")
            )
            estimate = refreshed.model_copy(update={"warnings": (*refreshed.warnings, note)})
        return estimate

    def _estimate_with(
        self,
        position: ValidatedPosition,
        envelope: DataEnvelope[MarketSnapshot] | DataEnvelope[VaultSnapshot],
        quote: DataEnvelope[ExitQuote] | None,
        gas: GasAssumptions,
    ) -> ExitEstimate:
        if position.mode is PositionMode.LENDER:
            return self._exit_costs.estimate_lender(position, envelope, quote, gas)
        if not isinstance(envelope.value, MarketSnapshot):
            raise ExitCostUnavailableError("borrower exits require a market snapshot")
        return self._exit_costs.estimate_borrower(position, envelope, quote, gas)

    def _quote_request(
        self,
        position: ValidatedPosition,
        snapshot: MarketSnapshot | VaultSnapshot,
        assets: Mapping[str, AssetInfo],
    ) -> QuoteRequest | None:
        eth_usd = self._usd_price("WETH", assets)
        try:
            if position.mode is PositionMode.LENDER:
                return self._lender_request(position, snapshot, assets, eth_usd)
            if not isinstance(snapshot, MarketSnapshot):
                raise ExitCostUnavailableError("borrower exits require a market snapshot")
            return _borrower_request(position, snapshot, eth_usd)
        except ValidationError:
            raise ExitCostUnavailableError("the exit swap could not be expressed as a quote") from None

    def _lender_request(
        self,
        position: ValidatedPosition,
        snapshot: MarketSnapshot | VaultSnapshot,
        assets: Mapping[str, AssetInfo],
        eth_usd: Decimal | None,
    ) -> QuoteRequest | None:
        asset = snapshot.asset if isinstance(snapshot, VaultSnapshot) else snapshot.loan_asset
        numeraire = position.numeraire
        if _reported_in(asset, numeraire):
            return None
        if numeraire.casefold() not in USD_NUMERAIRES:
            raise ExitCostUnavailableError(f"no conversion route from {asset.symbol} to {numeraire}")
        price = asset.price_usd
        if price is None or not price.is_finite() or price <= ZERO:
            raise ExitCostUnavailableError(f"cannot value {asset.symbol} in {numeraire}")
        token_out = assets.get("usdc")
        if token_out is None or token_out.price_usd is None:
            usdc_price = self._usd_price("USDC", assets)
            if usdc_price is None:
                raise ExitCostUnavailableError("no USDC price is available for the exit swap")
            token_out = AssetInfo(
                address=USDC_ADDRESS, symbol="USDC", decimals=6, price_usd=usdc_price
            )
        with localcontext() as context:
            context.prec = 60
            requested = (position.amount / price).quantize(
                Decimal(1).scaleb(-asset.decimals), rounding=ROUND_DOWN
            )
        amount_in = min(requested, snapshot.liquidity_assets).quantize(
            Decimal(1).scaleb(-asset.decimals), rounding=ROUND_DOWN
        )
        if amount_in <= ZERO:
            raise ExitCostUnavailableError("no liquidity can be withdrawn immediately to swap")
        return QuoteRequest(
            token_in=asset, token_out=token_out, amount_in=amount_in, eth_price_usd=eth_usd
        )

    def _quote(self, request: QuoteRequest, *, refresh: bool) -> DataEnvelope[ExitQuote]:
        if self._quotes is None:
            raise _ExitProviderError("Exit cost unavailable: no Uniswap quote provider is configured.")
        try:
            return self._quotes.quote(request, refresh=refresh)
        except Exception as error:  # provider boundary: never leak provider text
            logger.warning("uniswap quote unavailable: %s", type(error).__name__)
            raise _ExitProviderError(
                "Exit cost unavailable: the Uniswap quote could not be obtained."
            ) from None

    def _gas_assumptions(
        self,
        numeraire: str,
        assets: Mapping[str, AssetInfo],
        quote: DataEnvelope[ExitQuote] | None,
    ) -> GasAssumptions:
        gas_price: Decimal | None = None
        if self._gas is not None:
            try:
                gas_price = self._gas.gas_price_wei()
            except Exception as error:  # provider boundary
                logger.warning("gas price unavailable: %s", type(error).__name__)
        if gas_price is None and quote is not None:
            gas_price = quote.value.gas_price_wei
        if gas_price is None:
            raise _ExitProviderError("Exit cost unavailable: no gas price is available.")
        eth_price = self._eth_in_numeraire(numeraire, assets)
        if eth_price is None:
            raise _ExitProviderError(f"Exit cost unavailable: gas cannot be valued in {numeraire}.")
        return GasAssumptions(gas_price_wei=gas_price, eth_price=eth_price)

    def _eth_in_numeraire(self, numeraire: str, assets: Mapping[str, AssetInfo]) -> Decimal | None:
        if numeraire.casefold() in ETH_SYMBOLS:
            return ONE
        eth_usd = self._usd_price("WETH", assets)
        if eth_usd is None:
            return None
        if numeraire.casefold() in USD_NUMERAIRES:
            return eth_usd
        unit = assets.get(numeraire.casefold())
        unit_usd = unit.price_usd if unit is not None else None
        if unit_usd is None or not unit_usd.is_finite() or unit_usd <= ZERO:
            return None
        with localcontext() as context:
            context.prec = 60
            return eth_usd / unit_usd

    def _usd_price(self, symbol: str, assets: Mapping[str, AssetInfo]) -> Decimal | None:
        asset = assets.get(symbol.casefold())
        if asset is not None and asset.price_usd is not None and asset.price_usd > ZERO:
            return asset.price_usd
        if self._prices is None:
            return None
        try:
            price = self._prices.price_usd(symbol)
        except Exception as error:  # provider boundary
            logger.warning("price unavailable: %s", type(error).__name__)
            return None
        if price is None or not price.is_finite() or price <= ZERO:
            return None
        return price

    # Live triggers

    def _live_triggers(
        self, oracles: Mapping[str, DataEnvelope[OracleObservation]]
    ) -> tuple[str, ...]:
        triggers: list[str] = []
        for envelope in oracles.values():
            if envelope.freshness is FreshnessStatus.EXPIRED:
                continue  # reported as missing critical data instead
            observation = envelope.value
            stale = (
                observation.age_seconds is not None
                and observation.age_seconds >= ORACLE_STALE_THRESHOLD_SECONDS
            )
            deviated = (
                observation.deviation is not None
                and abs(observation.deviation) >= ORACLE_DEVIATION_THRESHOLD
            )
            if stale or deviated:
                triggers.append(LIVE_ORACLE_FAILURE)
        try:
            if self._incidents.active_incidents(MORPHO_PROTOCOL):
                triggers.append(PROTOCOL_EMERGENCY)
        except Exception as error:  # provider boundary
            logger.warning("incident feed unavailable: %s", type(error).__name__)
        return tuple(dict.fromkeys(triggers))


class _ExitProviderError(Exception):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


# Probabilities


class HybridProbabilityProvider:
    """Empirical history for price events, model-estimated evidence for protocols."""

    def __init__(
        self,
        history: EmpiricalHistory | None,
        evidence: ProtocolEvidenceSource | None,
        *,
        stablecoin_feeds: Mapping[str, tuple[str, ...]] = DEFAULT_STABLECOIN_FEEDS,
        collateral_feeds: Mapping[str, tuple[str, ...]] = DEFAULT_COLLATERAL_FEEDS,
        lookback: timedelta = HISTORY_LOOKBACK,
    ) -> None:
        self._history = history
        self._evidence = evidence
        self._stablecoin_feeds = {key.casefold(): value for key, value in stablecoin_feeds.items()}
        self._collateral_feeds = {key.casefold(): value for key, value in collateral_feeds.items()}
        self._lookback = lookback

    def assess(
        self,
        selection: ScenarioSelection,
        graph: RiskGraph | None,
        position: ValidatedPosition | None,
        *,
        now: datetime,
    ) -> ProbabilityAssessment:
        scenario = selection.scenario_id
        if scenario is ScenarioId.PROTOCOL_IMPAIRMENT:
            return self._protocol(now)
        if scenario is ScenarioId.STABLECOIN_DEPEG:
            return self._empirical(
                selection, graph, now, self._stablecoin_feeds, DepegEvent(selection.severity)
            )
        if scenario is ScenarioId.COLLATERAL_SHOCK:
            return self._empirical(
                selection, graph, now, self._collateral_feeds, lambda: PriceShockEvent(selection.severity)
            )
        if scenario is ScenarioId.ORACLE_FAILURE:
            reason = (
                "No historical oracle-failure probability: hourly Chainlink history cannot "
                "resolve the 30-minute staleness threshold and no comparison-market history "
                "is available. Live oracle breaches still act as hard triggers."
            )
        else:
            reason = (
                "No historical exit-liquidity probability: Uniswap depth history is not "
                "collected in the MVP."
            )
        return ProbabilityAssessment(unavailable_estimate(now, reason))

    def _empirical(
        self,
        selection: ScenarioSelection,
        graph: RiskGraph | None,
        now: datetime,
        feeds: Mapping[str, tuple[str, ...]],
        event: EventPredicate | Callable[[], EventPredicate],
    ) -> ProbabilityAssessment:
        scenario = selection.scenario_id.value
        if graph is None:
            return ProbabilityAssessment(unavailable_estimate(now, f"No graph for {scenario}."))
        symbols = _shocked_symbols(graph, selection.scenario_id)
        if not symbols:
            return ProbabilityAssessment(
                unavailable_estimate(
                    now, f"No asset affected by {scenario} is in this position's graph."
                )
            )
        missing = [symbol for symbol in symbols if not feeds.get(symbol.casefold())]
        if missing:
            return ProbabilityAssessment(
                unavailable_estimate(
                    now,
                    f"No price-history feed for {', '.join(missing)}; the {scenario} probability "
                    "is unavailable rather than approximated by an unrelated feed.",
                )
            )
        if self._history is None:
            return ProbabilityAssessment(
                unavailable_estimate(now, "Ethereum price history is not configured.")
            )
        end = now.astimezone(UTC)
        start = end - self._lookback
        per_asset: list[ProbabilityAssessment] = []
        for symbol in symbols:
            estimates: list[ProbabilityEstimate] = []
            for feed in feeds[symbol.casefold()]:
                predicate = event() if _is_factory(event) else event
                evidence = self._history.estimate(feed, start, end, predicate, now=now)
                estimates.append(evidence.probability)
            per_asset.append(combine_conservative(f"probability:{scenario}:{symbol}", estimates))
        if any(item.estimate.central is None for item in per_asset):
            return ProbabilityAssessment(
                unavailable_estimate(
                    now,
                    f"Price history for {scenario} is unavailable or insufficient: "
                    + "; ".join(item.estimate.explanation for item in per_asset),
                ),
                tuple(conflict for item in per_asset for conflict in item.conflicts),
            )
        # Several shocked assets: the chance that any one is hit is at least the
        # largest single estimate; take that conservative bound at the lowest confidence.
        best = max(per_asset, key=lambda item: item.estimate.central or ZERO)
        lowest = min(
            (item.estimate.confidence for item in per_asset), key=CONFIDENCE_ORDER.index
        )
        return ProbabilityAssessment(
            best.estimate.model_copy(update={"confidence": lowest}),
            tuple(conflict for item in per_asset for conflict in item.conflicts),
        )

    def _protocol(self, now: datetime) -> ProbabilityAssessment:
        if self._evidence is None:
            return ProbabilityAssessment(
                unavailable_estimate(now, "No protocol evidence source is configured.")
            )
        from app.evidence.scoring import map_score_to_probability, score_evidence

        score = score_evidence(self._evidence.evidence(MORPHO_PROTOCOL, now=now))
        return ProbabilityAssessment(
            map_score_to_probability(score), tuple(score.quality.conflicts)
        )


def combine_conservative(
    field: str, estimates: Sequence[ProbabilityEstimate]
) -> ProbabilityAssessment:
    """Merge estimates of one event from several sources (spec § 15).

    The highest central estimate is used (with the widest upper bound), both
    values are kept in a `DataConflict` when they disagree, and confidence drops
    one level below the lowest source confidence. Unavailable sources are
    ignored while at least one source is usable, at a confidence cost.
    """
    if not estimates:
        raise ValueError("at least one estimate is required")
    usable = [item for item in estimates if item.central is not None]
    if not usable:
        return ProbabilityAssessment(estimates[0])
    best = max(usable, key=lambda item: (item.central, item.high or ZERO))
    confidence = min((item.confidence for item in usable), key=CONFIDENCE_ORDER.index)
    conflicts: tuple[DataConflict, ...] = ()
    centrals = {item.central for item in usable}
    if len(centrals) > 1:
        conflicts = (
            DataConflict(
                field=field,
                values=tuple(f"{item.central:f}" for item in usable),
                sources=tuple(item.source for item in usable),
            ),
        )
    if len(centrals) > 1 or len(usable) < len(estimates):
        confidence = _lower_confidence(confidence)
    highs = [item.high for item in usable if item.high is not None]
    lows = [item.low for item in usable if item.low is not None]
    explanation = best.explanation
    if conflicts:
        explanation += (
            f" Sources disagree ({', '.join(f'{item.source}={item.central:f}' for item in usable)}); "
            "the higher estimate is used."
        )
    merged = best.model_copy(
        update={
            "high": max(highs) if highs else best.high,
            "low": max(lows) if lows else best.low,
            "confidence": confidence,
            "explanation": explanation,
        }
    )
    return ProbabilityAssessment(merged, conflicts)


class DepegEvent:
    """The window contains a price at or below `1 - severity` of the $1 / reference peg."""

    def __init__(self, severity: Decimal) -> None:
        self.threshold = ONE - severity
        self.description = f"price at or below {self.threshold} of its reference value"

    def __call__(self, window: Sequence[Observation]) -> bool:
        threshold = self.threshold
        return any(item.value <= threshold for item in window)


class PriceShockEvent:
    """The window contains a fall of at least `severity` within 24 hours.

    The estimator calls the predicate on overlapping windows in start order. For
    each hour the latest earlier hour (at most 24 h before) from which the price
    fell by the severity is memoized, so a window contains the event exactly when
    some hour in it has such a start inside the same window.
    """

    def __init__(self, severity: Decimal, horizon_hours: int = COLLATERAL_SHOCK_HOURS) -> None:
        self.factor = ONE - severity
        self.horizon_hours = horizon_hours
        self.description = f"price falls at least {severity} within {horizon_hours} hours"
        self._values: dict[datetime, Decimal] = {}
        self._starts: dict[datetime, datetime | None] = {}

    def __call__(self, window: Sequence[Observation]) -> bool:
        if not window:
            return False
        window_start = window[0].timestamp
        for item in window:
            self._values.setdefault(item.timestamp, item.value)
        for item in window:
            start = self._latest_start(item.timestamp)
            if start is not None and start >= window_start:
                return True
        return False

    def _latest_start(self, timestamp: datetime) -> datetime | None:
        if timestamp in self._starts:
            return self._starts[timestamp]
        value = self._values[timestamp]
        found: datetime | None = None
        for offset in range(1, self.horizon_hours + 1):
            earlier = timestamp - offset * HOUR
            prior = self._values.get(earlier)
            if prior is not None and value <= prior * self.factor:
                found = earlier
                break
        self._starts[timestamp] = found
        return found


def unavailable_estimate(now: datetime, reason: str) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE,
        low=None,
        central=None,
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE,
        horizon_days=30,
        source=UNAVAILABLE_SOURCE,
        retrieved_at=now,
        freshness=FreshnessStatus.EXPIRED,
        explanation=reason,
    )


# Helpers


def _higher(candidate: ProbabilityEstimate, current: ProbabilityEstimate) -> bool:
    if candidate.central is None or current.central is None:
        return candidate.central is None and current.central is not None
    return candidate.central > current.central


def _lower_confidence(level: ConfidenceLevel) -> ConfidenceLevel:
    index = CONFIDENCE_ORDER.index(level)
    return CONFIDENCE_ORDER[max(1, index - 1)] if index > 0 else level


def _is_factory(event: object) -> bool:
    return not hasattr(event, "description")


def _shocked_symbols(graph: RiskGraph, scenario: ScenarioId) -> tuple[str, ...]:
    """Symbols of asset nodes the scenario shocks, mirroring the scenario engine's targets."""
    definition = SCENARIO_DEFINITIONS[scenario]
    wanted = {symbol.casefold() for symbol in definition.affected_assets}
    nodes = {node.node_id: node for node in graph.nodes}
    position = next(node.node_id for node in graph.nodes if node.node_type is RiskNodeType.POSITION)
    outgoing: dict[str, list[str]] = {}
    collateral: set[str] = set()
    for edge in graph.edges:
        if edge.relationship is RiskEdgeType.EXITS_THROUGH:
            continue
        if edge.exposure_weight is not None and edge.exposure_weight <= ZERO:
            continue
        outgoing.setdefault(edge.source, []).append(edge.target)
        if edge.relationship is RiskEdgeType.USES_COLLATERAL:
            collateral.add(edge.target)
    reached = {position}
    queue = deque([position])
    while queue:
        for target in outgoing.get(queue.popleft(), ()):
            if target not in reached:
                reached.add(target)
                queue.append(target)
    symbols: dict[str, str] = {}
    for item in sorted(reached):
        node = nodes[item]
        if node.node_type is not RiskNodeType.ASSET:
            continue
        if scenario is ScenarioId.COLLATERAL_SHOCK and item not in collateral:
            continue
        symbol = node.metrics.get("symbol")
        label = symbol if isinstance(symbol, str) and symbol else node.label
        if label.casefold() in wanted:
            symbols.setdefault(label.casefold(), label)
    return tuple(symbols[key] for key in sorted(symbols))


def _mark_unresolved_markets(graph: RiskGraph, market_ids: Iterable[str]) -> RiskGraph:
    """Flag allocated markets without a snapshot as unknown exposure, never zero exposure."""
    unresolved = {node_id(RiskNodeType.MARKET, market_id) for market_id in market_ids}
    if not unresolved:
        return graph
    nodes = tuple(
        node.model_copy(update={"metrics": {**node.metrics, "exposure_unknown": True}})
        if node.node_id in unresolved
        else node
        for node in graph.nodes
    )
    return graph.model_copy(update={"nodes": nodes})


def _known_assets(snapshots: _Snapshots) -> dict[str, AssetInfo]:
    assets: dict[str, AssetInfo] = {}
    markets = [snapshots.market] if snapshots.market is not None else []
    markets.extend(snapshots.allocation_markets[key] for key in sorted(snapshots.allocation_markets))
    candidates: list[AssetInfo] = []
    for envelope in markets:
        candidates.extend((envelope.value.loan_asset, envelope.value.collateral_asset))
    if snapshots.vault is not None:
        candidates.insert(0, snapshots.vault.value.asset)
    for asset in candidates:
        key = asset.symbol.casefold()
        if key not in assets or assets[key].price_usd is None:
            assets[key] = asset
    return assets


def _reported_in(asset: AssetInfo, numeraire: str) -> bool:
    """Mirror of the exit-cost swap rule: no swap for the numeraire itself or USD stables."""
    symbol = asset.symbol.casefold()
    key = numeraire.casefold()
    return symbol == key or (key in USD_NUMERAIRES and symbol in USD_STABLE_SYMBOLS)


def _borrower_request(
    position: ValidatedPosition, market: MarketSnapshot, eth_usd: Decimal | None
) -> QuoteRequest | None:
    collateral_asset = market.collateral_asset
    if collateral_asset.address.lower() == market.loan_asset.address.lower():
        return None
    collateral = position.collateral_amount
    collateral_value = position.collateral_value
    debt_value = position.debt_value
    if not collateral or not collateral_value or debt_value is None or collateral <= ZERO:
        raise ExitCostUnavailableError(
            "borrower exits require positive collateral and debt amounts and values"
        )
    with localcontext() as context:
        context.prec = 60
        price = collateral_value / collateral
        needed = debt_value / price * (ONE + BORROWER_SALE_MARGIN)
    quantum = Decimal(1).scaleb(-collateral_asset.decimals)
    amount_in = min(collateral, needed).quantize(quantum, rounding=ROUND_DOWN)
    if amount_in <= ZERO:
        raise ExitCostUnavailableError("the collateral sale for the exit rounds to zero")
    return QuoteRequest(
        token_in=collateral_asset,
        token_out=market.loan_asset,
        amount_in=amount_in,
        eth_price_usd=eth_usd,
    )


def _unavailable_exit(now: datetime, reason: str) -> ExitEstimate:
    return ExitEstimate(
        withdrawable=False,
        route=(),
        slippage=ZERO,
        fees=ZERO,
        gas=ZERO,
        price_impact=ZERO,
        total_cost=ZERO.quantize(NUMERAIRE_PLACES),
        quote_age_seconds=0,
        quote_block=None,
        quoted_at=now,
        source=UNAVAILABLE_SOURCE,
        retrieved_at=now,
        freshness=FreshnessStatus.EXPIRED,
        warnings=(reason, "The exit cost is unknown, not zero."),
    )


# Default (live) adapters, used by the API wiring.


class UniswapQuotes:
    """`QuoteProvider` over Task 9's `UniswapClient`; `refresh` bypasses the quote cache."""

    def __init__(self, cached, uncached) -> None:
        self._cached = cached
        self._uncached = uncached

    def quote(self, request: QuoteRequest, *, refresh: bool) -> DataEnvelope[ExitQuote]:
        client = self._uncached if refresh else self._cached
        return client.quote_exact_input(request)


class EthereumGas:
    def __init__(self, client) -> None:
        self._client = client

    def gas_price_wei(self) -> Decimal:
        return self._client.get_gas_price()


class ChainlinkPrices:
    """Latest Chainlink answer within a recent window, for gas and stablecoin valuation."""

    FEEDS: Mapping[str, str] = {"weth": "ETH/USD", "eth": "ETH/USD", "usdc": "USDC/USD"}

    def __init__(
        self,
        client,
        *,
        window: timedelta = timedelta(hours=6),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._window = window
        self._clock = clock

    def price_usd(self, symbol: str) -> Decimal | None:
        feed = self.FEEDS.get(symbol.casefold())
        if feed is None:
            return None
        end = self._clock()
        observations = self._client.get_chainlink_observations(feed, end - self._window, end)
        return observations[-1].value if observations else None


class FileAndDefiScanEvidence:
    """Task 8 evidence: the version-controlled DeFiSafety snapshot plus a live DeFiScan review."""

    def __init__(self, defisafety_path: Path, defiscan=None) -> None:
        self._path = defisafety_path
        self._defiscan = defiscan

    def evidence(self, protocol: str, *, now: datetime) -> CombinedEvidence:
        from app.adapters.defiscan import DEFISCAN_METHODOLOGY_URL
        from app.evidence.models import EvidenceBundle, EvidenceSource
        from app.evidence.scoring import load_defisafety_evidence

        bundles = [load_defisafety_evidence(self._path, protocol)]
        if self._defiscan is not None:
            try:
                bundles.append(self._defiscan.get_review(protocol))
            except Exception as error:  # provider boundary: never leak provider text
                logger.warning("defiscan unavailable: %s", type(error).__name__)
                bundles.append(
                    EvidenceBundle.unavailable(
                        protocol=protocol,
                        source=EvidenceSource.DEFISCAN,
                        source_url=DEFISCAN_METHODOLOGY_URL,
                        methodology_url=DEFISCAN_METHODOLOGY_URL,
                        retrieved_at=now,
                        reason="DeFiScan review could not be retrieved",
                    )
                )
        return CombinedEvidence(protocol=protocol, bundles=tuple(bundles), evaluated_at=now)
