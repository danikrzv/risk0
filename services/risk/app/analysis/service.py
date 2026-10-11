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
allocations (vault entries), a live observation of every oracle in the graph
(the entry's market oracle and each resolved allocated market's oracle: no
provider, a provider error, or no observation all leave it missing), and the
exit estimate. An oracle that is not a Morpho Chainlink oracle (V1/V2), or that
reads a feed of unknown kind, has no observation and stays missing. An oracle
whose third-party feed has no published or inferable heartbeat IS observed: the
feed's age is reported, its staleness is not judged (no live staleness
trigger), deviation is still judged, the graph carries a completeness warning
per such feed, and the recommendation confidence is capped at LOW. A quote older than 30 s or more than 20 blocks from the
snapshot is refreshed once; if it is still expired the exit estimate is expired.

Probabilities (`HybridProbabilityProvider`):
- stablecoin depeg and collateral shock: empirical rolling 30-day windows over
  hourly Chainlink history (Task 7, read round by round) for each shocked asset
  that has a feed: USDC -> USDC/USD, WETH -> ETH/USD, wstETH -> stETH/USD (the
  wstETH/stETH exchange-rate caveat is in the explanation), WBTC -> WBTC/BTC x
  BTC/USD. Assets without a feed (EURCV has no EURCV/EUR feed) make the estimate
  UNAVAILABLE; it is never approximated by an unrelated feed.
- protocol impairment: model-estimated from protocol evidence (Task 8) and the
  empirical DeFiLlama base rate for large lending protocols
  (`app.evidence.defillama`). With both, the higher estimate is used
  (`combine_conservative`); with only the base rate (no DeFiScan or DeFiSafety
  review of Morpho exists), it is used alone at LOW confidence.
- oracle failure: empirical, from exact Chainlink round timestamps of every feed
  the graph's decoded oracles read. A 30-day window is an event when a gap
  between consecutive updates exceeds the feed's heartbeat + 30 minutes (the
  live trigger's threshold); rolling windows over 365 days with a Wilson
  interval. Feeds of one oracle are combined with `combine_conservative`, and
  the highest oracle estimate is used. Feeds that compute their answer when
  read (exchange-rate adapters, computed-on-read feeds, constant scalers)
  cannot be stale and are excluded; an oracle that was not decoded makes it
  UNAVAILABLE. A third-party feed with a heartbeat (published or inferred) and
  readable round history gets the same empirical estimate; one without gets an
  explained industry prior (the DeFiLlama rate of material oracle-related
  exploits among large Lending/CDP protocol-years, LOW confidence).
  Live oracle breaches still act as hard triggers.
- liquidity collapse: UNAVAILABLE; no pool depth history is collected.

Probabilities are estimated per selection, i.e. per (scenario id, severity), and
each selection is propagated with its own estimate, so a scenario selected at
two severities never pairs one severity's probability with the other's impact.

Live oracle health (`ChainlinkOracleHealth`): see that class. Spec § 13 says an
oracle "stale for 30 minutes" is a hard trigger; Chainlink feeds update on a
heartbeat (ETH/USD 1 h, USDC/USD and EUR/USD 24 h), so a literal 30-minute age
would fire on healthy feeds. Risk0 interprets it as *older than the feed's
heartbeat plus 30 minutes*: the observation's `age_seconds` is the staleness
beyond the heartbeat, which the engine and the live-trigger check compare with
the unchanged 1800 s threshold.
"""

import logging
import threading
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
    CarryEstimate,
    LiveTrigger,
    estimate_carry,
    make_recommendation,
)
from app.analysis.scenario_engine import ScenarioEngine
from app.catalog.assets import AssetRegistry, default_asset_registry
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
    RiskNode,
    RiskNodeType,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.bad_debt import BorrowerDistribution, build_distribution
from app.domain.exit import NUMERAIRE_PLACES, ExitQuote, GasAssumptions, QuoteRequest
from app.domain.graph import (
    GraphConstructionError,
    GraphContext,
    OracleFeedObservation,
    OracleObservation,
    node_id,
)
from app.domain.position import PositionValidationError, ValidatedPosition, validate_position
from app.domain.protocol import ProtocolTvlSnapshot
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
# Asset -> catalog feeds (data/catalog/feeds.ethereum.json). EURCV is deliberately
# absent (EUR/USD prices the euro, not EURCV). wstETH uses stETH/USD (the feed's
# catalog note states the exchange-rate caveat); WBTC uses the composite
# WBTC/USD = WBTC/BTC x BTC/USD, so both the wrapper peg and BTC itself count.
DEFAULT_STABLECOIN_FEEDS: Mapping[str, tuple[str, ...]] = {"USDC": ("USDC/USD",)}
DEFAULT_COLLATERAL_FEEDS: Mapping[str, tuple[str, ...]] = {
    "WETH": ("ETH/USD",),
    "wstETH": ("stETH/USD",),
    "WBTC": ("WBTC/USD",),
}
# Heartbeats from Chainlink's Ethereum mainnet feed configuration
# (data.chain.link): the longest interval between answers of a healthy feed.
# This table overrides the heartbeat inferred from round history (the 99th
# percentile of update gaps, `app.history.staleness`); on 2026-10-11 the
# inference over 90 days returned the same value for every entry.
ETH_USD_HEARTBEAT_SECONDS = 3600
USDC_USD_HEARTBEAT_SECONDS = 86400
EUR_USD_HEARTBEAT_SECONDS = 86400
STETH_USD_HEARTBEAT_SECONDS = 3600
BTC_USD_HEARTBEAT_SECONDS = 3600
WBTC_BTC_HEARTBEAT_SECONDS = 86400
BTC_ETH_HEARTBEAT_SECONDS = 86400
CHAINLINK_HEARTBEATS: Mapping[str, int] = {
    "ETH/USD": ETH_USD_HEARTBEAT_SECONDS,
    "USDC/USD": USDC_USD_HEARTBEAT_SECONDS,
    "EUR/USD": EUR_USD_HEARTBEAT_SECONDS,
    "stETH/USD": STETH_USD_HEARTBEAT_SECONDS,
    "BTC/USD": BTC_USD_HEARTBEAT_SECONDS,
    "WBTC/BTC": WBTC_BTC_HEARTBEAT_SECONDS,
    "BTC/ETH": BTC_ETH_HEARTBEAT_SECONDS,
}
# Heartbeats for price history (forward-fill limits): a composite answer stands
# until either component updates, so its heartbeat is the shortest component's.
HISTORY_HEARTBEATS: Mapping[str, int] = {
    **CHAINLINK_HEARTBEATS,
    "WBTC/USD": min(WBTC_BTC_HEARTBEAT_SECONDS, BTC_USD_HEARTBEAT_SECONDS),
}
# Round history used to infer the heartbeat of a feed missing from the table.
HEARTBEAT_INFERENCE_LOOKBACK = timedelta(days=90)
HEARTBEAT_MEMO_TTL = timedelta(days=1)
# An inferred heartbeat (no documented value) tolerates one missed cycle.
INFERRED_HEARTBEAT_TOLERANCE_CYCLES = 2
INFERRED_HEARTBEAT_SOURCE_PREFIX = "inferred"
# Third-party feeds (`app.adapters.morpho_oracle`): a heartbeat from the
# version-controlled registry of published policies, or none at all.
PUBLISHED_HEARTBEAT_SOURCE_PREFIX = "published"
UNKNOWN_HEARTBEAT_SOURCE = (
    "unknown: no published heartbeat and no inferable round history; staleness not judged"
)
NEVER_STALE_SOURCES: Mapping[str, str] = {
    "exchange_rate_adapter": "computed on read; cannot be stale",
    "computed_on_read": "computed on read; cannot be stale",
    "constant_scaler": "constant scaler (exact power of ten); no price risk, cannot be stale",
}
# A Morpho oracle's feeds are immutable: a decoded configuration is kept for the
# process lifetime; an oracle that could not be decoded is retried after this.
ORACLE_DECODE_RETRY = timedelta(minutes=10)
# Chainlink reads are memoized this long, so one analysis (and concurrent ones)
# reads each feed once; ages are still computed against the request time.
CHAINLINK_MEMO_TTL = timedelta(seconds=60)
# Spec § 9: risk evidence refreshes daily.
DEFISCAN_TTL = timedelta(days=1)
DEFISCAN_FAILURE_TTL = timedelta(minutes=5)
PROTOCOL_TVL_UNAVAILABLE = (
    "Morpho TVL concentration is unknown: DeFiLlama TVL data is unavailable."
)

SNAPSHOT_SOURCE = "Morpho entity snapshot"
ORACLE_SOURCE = "live oracle"
ALLOCATIONS_SOURCE = "Morpho vault allocations"
EXIT_SOURCE = "exit estimate"
BORROWER_POSITIONS_SOURCE = "Morpho borrower positions"
# Lender bad-debt estimate: borrower positions are fetched for at most this many
# collateral-shocked markets (most exposed first); the rest use the worst-case bound.
MAX_BAD_DEBT_MARKETS = 12


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
    """Protocol evidence scores. A source may also offer an empirical base rate as
    `impairment_base_rate(protocol, *, now) -> ProbabilityEstimate`."""

    def evidence(self, protocol: str, *, now: datetime) -> CombinedEvidence: ...


class ProtocolTvlProvider(Protocol):
    def protocol_tvl(
        self, protocol: str, *, now: datetime
    ) -> DataEnvelope[ProtocolTvlSnapshot] | None: ...


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
        asset_registry: AssetRegistry | None = None,
        protocol_tvl: ProtocolTvlProvider | None = None,
    ) -> None:
        self._protocol_tvl = protocol_tvl
        self._catalog = catalog
        self._asset_registry = asset_registry or default_asset_registry()
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

        addresses, oracles = self._observe_oracles(snapshots, now)
        for address in addresses:
            label = f"{ORACLE_SOURCE} {address}"
            critical.append(label)
            observation = oracles.get(address)
            if observation is not None:
                sources.append((label, Freshness(observation.freshness.value)))

        graph = self._graph(position, entry, snapshots, oracles, now)
        borrowers: dict[str, BorrowerDistribution] = {}
        if position.mode is PositionMode.LENDER:
            borrowers = self._borrower_distributions(graph, request.scenarios, sources)
        conflicts: list[DataConflict] = []
        scenarios_out = []
        try:
            for selection in request.scenarios:
                # The engine keys probabilities by scenario id, so each selection
                # is propagated on its own with the estimate for its severity.
                key = selection.scenario_id.value
                assessment = self._assess(selection, graph, position, now)
                conflicts.extend(assessment.conflicts)
                sources.append(
                    (
                        f"probability {key}@{selection.severity}",
                        Freshness(assessment.estimate.freshness.value),
                    )
                )
                scenarios_out.extend(
                    self._engine.run(
                        position,
                        graph,
                        (selection,),
                        {key: assessment.estimate},
                        borrowers=borrowers,
                    )
                )
            scenarios = tuple(scenarios_out)
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
            carry=_carry(position, snapshots),
            confidence_caps=_unjudged_oracle_caps(oracles),
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
    ) -> tuple[tuple[str, ...], dict[str, DataEnvelope[OracleObservation]]]:
        """Every graph oracle address (all critical) and the live observations obtained."""
        markets = [snapshots.market] if snapshots.market is not None else []
        markets.extend(snapshots.allocation_markets[key] for key in sorted(snapshots.allocation_markets))
        addresses: list[str] = []
        observed: dict[str, DataEnvelope[OracleObservation]] = {}
        for envelope in markets:
            market = envelope.value
            key = market.oracle_address.lower()
            if key in addresses:
                continue
            addresses.append(key)
            if self._oracles is None:
                continue
            try:
                observation = self._oracles.observe(market, now=now)
            except Exception as error:  # provider boundary: never leak provider text
                logger.warning("oracle health unavailable: %s", type(error).__name__)
                continue
            if observation is None:
                logger.info("no live oracle observation for a graph oracle")
                continue
            if observation.value.oracle_address.lower() != key:
                logger.warning("oracle observation does not match the requested oracle")
                continue
            observed[key] = observation
        return tuple(addresses), observed

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
            asset_registry=self._asset_registry,
            protocol_tvl=self._protocol_tvl_envelope(now),
        )
        try:
            graph = build_risk_graph(position, context, clock=lambda: now)
        except GraphConstructionError as error:
            logger.warning("graph construction failed: %s", type(error).__name__)
            raise AnalysisProviderError(
                "Provider data for this entry is inconsistent; analysis is unavailable."
            ) from None
        if self._protocol_tvl is not None and context.protocol_tvl is None:
            graph = _with_warning(graph, PROTOCOL_TVL_UNAVAILABLE)
        return _mark_unresolved_markets(graph, snapshots.unresolved_markets)

    def _borrower_distributions(
        self,
        graph: RiskGraph,
        selections: Sequence[ScenarioSelection],
        sources: list[tuple[str, Freshness]],
    ) -> dict[str, BorrowerDistribution]:
        """Borrower debt distributions of the markets a lender's collateral shock reaches.

        Positions come from Morpho (`get_market_positions`, cached with the other
        Morpho snapshots). A market that cannot be read, or beyond
        `MAX_BAD_DEBT_MARKETS`, is left out and the engine prices it at the
        worst-case bound. Raw positions never reach the response.
        """
        fetch = getattr(self._morpho, "get_market_positions", None)
        if not callable(fetch):
            return {}
        try:
            wanted = self._engine.collateral_markets(graph, selections)
        except ValueError as error:
            logger.warning("bad-debt market selection failed: %s", type(error).__name__)
            return {}
        lltvs: dict[str, Decimal] = {}
        for node in graph.nodes:
            market_id = node.metrics.get("market_id")
            lltv = node.metrics.get("lltv")
            if node.node_type is RiskNodeType.MARKET and isinstance(market_id, str):
                if isinstance(lltv, Decimal) and lltv > ZERO:
                    lltvs[market_id.lower()] = lltv
        distributions: dict[str, BorrowerDistribution] = {}
        for market_id in wanted[:MAX_BAD_DEBT_MARKETS]:
            lltv = lltvs.get(market_id)
            if lltv is None:
                continue
            try:
                envelope = fetch(market_id)
                positions = envelope.value
                if positions.market_id.lower() != market_id:
                    logger.warning("borrower positions do not match the requested market")
                    continue
            except Exception as error:  # provider boundary: never leak provider text
                logger.info("borrower positions unavailable: %s", type(error).__name__)
                continue
            if envelope.freshness is FreshnessStatus.EXPIRED:
                continue
            sources.append(
                (f"{BORROWER_POSITIONS_SOURCE} {market_id[:10]}", Freshness(envelope.freshness.value))
            )
            distributions[market_id] = build_distribution(
                market_id,
                ((item.borrow_assets, item.health_factor) for item in positions.positions),
                lltv,
                complete=positions.complete,
            )
        return distributions

    def _protocol_tvl_envelope(self, now: datetime) -> DataEnvelope[ProtocolTvlSnapshot] | None:
        """Morpho TVL for the protocol node; a provider gap is a completeness warning, never fatal."""
        if self._protocol_tvl is None:
            return None
        try:
            return self._protocol_tvl.protocol_tvl(MORPHO_PROTOCOL, now=now)
        except Exception as error:  # provider boundary: never leak provider text
            logger.warning("protocol tvl unavailable: %s", type(error).__name__)
            return None

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
    ) -> tuple[LiveTrigger, ...]:
        triggers: list[LiveTrigger] = []
        for address in sorted(oracles):
            envelope = oracles[address]
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
                triggers.append(
                    LiveTrigger(
                        LIVE_ORACLE_FAILURE,
                        node_id(RiskNodeType.ORACLE, address),
                        _freshness_confidence(envelope.freshness),
                    )
                )
        try:
            if self._incidents.active_incidents(MORPHO_PROTOCOL):
                triggers.append(
                    LiveTrigger(PROTOCOL_EMERGENCY, node_id(RiskNodeType.PROTOCOL, MORPHO_PROTOCOL))
                )
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
            return self._oracle_failure(graph, now)
        reason = (
            "No historical exit-liquidity probability: Uniswap depth history is not "
            "collected in the MVP."
        )
        return ProbabilityAssessment(unavailable_estimate(now, reason))

    def _oracle_failure(self, graph: RiskGraph | None, now: datetime) -> ProbabilityAssessment:
        """Oracle-failure probability over the graph's decoded oracles, per feed kind.

        Per feed:
        - Chainlink: the empirical staleness rate. The share of rolling 30-day
          windows (365-day lookback; the inference window for an inferred
          heartbeat) holding a gap between consecutive updates longer than
          heartbeat + 1800 s, from exact round timestamps, with a Wilson interval.
          Missing history makes the whole estimate UNAVAILABLE.
        - Exchange-rate adapters, computed-on-read feeds and constant scalers:
          excluded; they cannot be stale (structural zero when nothing else is read).
        - Third-party feed with a heartbeat (published or inferred) and readable
          round history: the same empirical staleness rate, read without
          Chainlink phases.
        - Third-party feed without both: an explained industry prior, the annual
          rate of material oracle-related exploits among large Lending/CDP
          protocol-years (`app.evidence.defillama.oracle_exploit_estimate`, LOW
          confidence). It is not a feed-specific estimate.
        Per oracle: its feed estimates via `combine_conservative` (the feeds are
        different sources of one failure, so no data conflict is reported, but
        confidence is lowered when they differ). Across oracles: the highest
        estimate at the lowest confidence.
        """
        if graph is None:
            return ProbabilityAssessment(unavailable_estimate(now, "No graph for oracle-failure."))
        oracles = _graph_oracles(graph)
        if not oracles:
            return ProbabilityAssessment(
                unavailable_estimate(now, "No oracle is in this position's graph.")
            )
        undecoded = [label for label, feeds in oracles.items() if feeds is None]
        if undecoded:
            return ProbabilityAssessment(
                unavailable_estimate(
                    now,
                    "No oracle-failure probability: oracle "
                    f"{', '.join(undecoded)} was not decoded into its price feeds.",
                )
            )
        estimate_staleness = getattr(self._history, "estimate_staleness", None)
        end = now.astimezone(UTC)
        start = end - self._lookback
        per_oracle: list[tuple[str, ProbabilityAssessment]] = []
        adapter_only: list[str] = []
        prior_feeds: list[str] = []
        prior: ProbabilityEstimate | None = None
        for label in sorted(oracles):
            feeds = oracles[label] or ()
            judged = [
                feed
                for feed in feeds
                if feed.metrics.get("feed_kind") not in NEVER_STALE_SOURCES
            ]
            if not judged:
                adapter_only.append(label)
                continue
            estimates: list[ProbabilityEstimate] = []
            unprioritised: list[str] = []
            for feed in judged:
                name = _feed_name(feed)
                third_party = feed.metrics.get("feed_kind") == "third_party"
                heartbeat = feed.metrics.get("heartbeat_seconds")
                readable = feed.metrics.get("round_history_readable") is True
                if third_party and (not isinstance(heartbeat, Decimal) or not readable):
                    unprioritised.append(
                        f"{name} ({'no readable round history' if isinstance(heartbeat, Decimal) else 'no published or inferable heartbeat'})"
                    )
                    continue
                if not callable(estimate_staleness):
                    estimates.append(
                        unavailable_estimate(now, "Feed round history is not configured.")
                    )
                    continue
                estimate = self._staleness_estimate(
                    estimate_staleness, feed, name, start, end, now, third_party=third_party
                )
                if third_party and estimate.central is None:
                    unprioritised.append(f"{name} (round history insufficient: {estimate.explanation})")
                    continue
                estimates.append(estimate)
            if unprioritised:
                if prior is None:
                    prior = self._oracle_prior(now)
                prior_feeds.extend(unprioritised)
                estimates.append(
                    prior.model_copy(
                        update={
                            "explanation": (
                                "Industry prior, not a feed-specific estimate, for third-party "
                                f"feed(s) {'; '.join(unprioritised)} of oracle {label}: "
                                f"{prior.explanation}"
                            )
                        }
                    )
                )
            missing = [item for item in estimates if item.central is None]
            if missing:
                return ProbabilityAssessment(
                    unavailable_estimate(
                        now,
                        f"Oracle-failure evidence for oracle {label} is unavailable or "
                        "insufficient: " + "; ".join(item.explanation for item in missing),
                    )
                )
            combined = combine_conservative(f"probability:oracle-failure:{label}", estimates)
            per_oracle.append((label, ProbabilityAssessment(combined.estimate)))
        if not per_oracle:
            return ProbabilityAssessment(
                structural_zero_estimate(
                    now,
                    "Oracle failure is measured as feed staleness, and every oracle "
                    f"({', '.join(adapter_only)}) reads only feeds that compute their answer "
                    "when read (exchange-rate adapters, computed-on-read feeds, constant "
                    "scalers), which cannot be stale. A wrong but fresh answer is not part of "
                    "this event.",
                )
            )
        label, best = max(per_oracle, key=lambda item: item[1].estimate.central or ZERO)
        lowest = min(
            (item.estimate.confidence for _, item in per_oracle), key=CONFIDENCE_ORDER.index
        )
        notes = [
            f"Oracle failure is measured as feed staleness (the live trigger's event), with an "
            f"industry oracle-exploit prior for third-party feeds without a heartbeat and round "
            f"history; the highest of {len(per_oracle)} oracle estimate(s) is used (oracle {label}).",
        ]
        if adapter_only:
            notes.append(
                f"Oracle(s) {', '.join(adapter_only)} read only feeds that compute their answer "
                "when read, which cannot be stale."
            )
        notes.append("A wrong but fresh answer is not part of the staleness event.")
        return ProbabilityAssessment(
            best.estimate.model_copy(
                update={
                    "confidence": lowest,
                    "explanation": f"{best.estimate.explanation} {' '.join(notes)}",
                }
            )
        )

    def _staleness_estimate(
        self,
        estimate_staleness,
        feed: RiskNode,
        name: str,
        start: datetime,
        end: datetime,
        now: datetime,
        *,
        third_party: bool,
    ) -> ProbabilityEstimate:
        heartbeat = feed.metrics.get("heartbeat_seconds")
        address = feed.metrics.get("address")
        if not isinstance(heartbeat, Decimal) or not isinstance(address, str):
            return unavailable_estimate(now, f"The heartbeat of {name} is unknown.")
        source = feed.metrics.get("heartbeat_source")
        inferred = isinstance(source, str) and source.startswith(INFERRED_HEARTBEAT_SOURCE_PREFIX)
        # An inferred cadence only describes the period it was inferred from;
        # judging older gaps against it would count a feed's earlier, slower
        # cadence as failures.
        feed_start = max(start, end - HEARTBEAT_INFERENCE_LOOKBACK) if inferred else start
        if third_party:
            evidence = estimate_staleness(
                address, int(heartbeat), feed_start, end, now=now, label=name, require_phase=False
            )
        else:
            evidence = estimate_staleness(address, int(heartbeat), feed_start, end, now=now, label=name)
        estimate = evidence.probability
        if inferred and estimate.central is not None:
            estimate = estimate.model_copy(
                update={
                    "confidence": min(
                        estimate.confidence, ConfidenceLevel.LOW, key=CONFIDENCE_ORDER.index
                    ),
                    "explanation": (
                        f"{estimate.explanation} Heartbeat {source}; staleness is "
                        f"judged over the same {HEARTBEAT_INFERENCE_LOOKBACK.days} days."
                    ),
                }
            )
        return estimate

    def _oracle_prior(self, now: datetime) -> ProbabilityEstimate:
        prior = getattr(self._evidence, "oracle_exploit_base_rate", None)
        if not callable(prior):
            return unavailable_estimate(
                now,
                "No industry oracle-exploit prior is configured (DeFiLlama is not available).",
            )
        return prior(now=now)

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
        scored = map_score_to_probability(score)
        conflicts = tuple(score.quality.conflicts)
        base_rate = getattr(self._evidence, "impairment_base_rate", None)
        if base_rate is None:
            return ProbabilityAssessment(scored, conflicts)
        return combine_protocol_estimates(scored, base_rate(MORPHO_PROTOCOL, now=now), conflicts)


def combine_protocol_estimates(
    scored: ProbabilityEstimate,
    base_rate: ProbabilityEstimate,
    conflicts: tuple[DataConflict, ...] = (),
) -> ProbabilityAssessment:
    """Evidence-score estimate and empirical base rate for protocol impairment.

    Both usable: the conservative merge of spec § 15. Only the base rate: it is
    used alone at LOW confidence and the explanation says why the evidence score
    is missing. Only the score: unchanged. Neither: UNAVAILABLE with both reasons.
    """
    if scored.central is not None and base_rate.central is not None:
        merged = combine_conservative("probability:protocol-impairment", (scored, base_rate))
        return ProbabilityAssessment(merged.estimate, (*conflicts, *merged.conflicts))
    if base_rate.central is not None:
        return ProbabilityAssessment(
            base_rate.model_copy(
                update={
                    "confidence": ConfidenceLevel.LOW,
                    "explanation": (
                        f"{base_rate.explanation} Used alone because protocol evidence "
                        f"scores are unavailable: {scored.explanation}"
                    ),
                }
            ),
            conflicts,
        )
    if scored.central is not None:
        return ProbabilityAssessment(scored, conflicts)
    return ProbabilityAssessment(
        scored.model_copy(
            update={
                "explanation": f"{scored.explanation} Empirical base rate: {base_rate.explanation}"
            }
        ),
        conflicts,
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


def structural_zero_estimate(now: datetime, reason: str) -> ProbabilityEstimate:
    """The event cannot occur by construction: probability 0, not an empirical rate."""
    return ProbabilityEstimate(
        method=ProbabilityMethod.STRUCTURAL,
        low=ZERO,
        central=ZERO,
        high=ZERO,
        confidence=ConfidenceLevel.MEDIUM,
        horizon_days=30,
        source="risk0:structural",
        retrieved_at=now,
        freshness=FreshnessStatus.FRESH,
        explanation=reason,
    )


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


def _unjudged_oracle_caps(
    oracles: Mapping[str, DataEnvelope[OracleObservation]],
) -> tuple[tuple[ConfidenceLevel, str], ...]:
    """LOW confidence when any live oracle reads a third-party feed of unjudged staleness."""
    names = [
        feed.description or feed.address.lower()
        for address in sorted(oracles)
        for feed in oracles[address].value.unjudged_feeds
    ]
    if not names:
        return ()
    return (
        (
            ConfidenceLevel.LOW,
            "Live oracle staleness is not judged for "
            f"{', '.join(dict.fromkeys(names))} (third-party feed(s) with no published "
            "heartbeat and no inferable round history; their age is reported), so "
            "confidence is at most low.",
        ),
    )


def _freshness_confidence(freshness: FreshnessStatus) -> ConfidenceLevel:
    if freshness is FreshnessStatus.FRESH:
        return ConfidenceLevel.HIGH
    if freshness is FreshnessStatus.STALE_USABLE:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.UNAVAILABLE


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


def _with_warning(graph: RiskGraph, warning: str) -> RiskGraph:
    if warning in graph.completeness_warnings:
        return graph
    return graph.model_copy(
        update={"completeness_warnings": (*graph.completeness_warnings, warning)}
    )


def _feed_name(feed: RiskNode) -> str:
    address = feed.metrics.get("address")
    description = feed.metrics.get("description")
    return f"{description} ({address})" if description else str(address)


def _graph_oracles(graph: RiskGraph) -> dict[str, tuple[RiskNode, ...] | None]:
    """Market oracles (address -> their feed nodes; None when not decoded)."""
    nodes = {node.node_id: node for node in graph.nodes}
    children: dict[str, list[RiskNode]] = {}
    market_oracles: list[str] = []
    for edge in graph.edges:
        if edge.relationship is not RiskEdgeType.USES_ORACLE:
            continue
        source = nodes[edge.source]
        target = nodes[edge.target]
        if source.node_type is RiskNodeType.MARKET and target.node_type is RiskNodeType.ORACLE:
            market_oracles.append(edge.target)
        elif source.node_type is RiskNodeType.ORACLE and target.node_type is RiskNodeType.ORACLE:
            children.setdefault(edge.source, []).append(target)
    result: dict[str, tuple[RiskNode, ...] | None] = {}
    for identifier in dict.fromkeys(market_oracles):
        node = nodes[identifier]
        address = node.metrics.get("address")
        label = address if isinstance(address, str) else identifier
        decoded = node.metrics.get("decomposed") is True
        feeds = sorted(children.get(identifier, ()), key=lambda item: item.node_id)
        result[label] = tuple(feeds) if decoded else None
    return result


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


def _carry(position: ValidatedPosition, snapshots: _Snapshots) -> CarryEstimate:
    """The yield exiting gives up: the vault's APY for a vault, the market's otherwise."""
    snapshot = snapshots.primary.value
    if isinstance(snapshot, VaultSnapshot):
        apy, source = snapshot.apy, "Morpho vault APY (net of fees when reported)"
    else:
        apy, source = snapshot.supply_apy, "Morpho market supply APY (net when reported)"
    return estimate_carry(position.mode, position.amount, apy, apy_source=source)


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


class ChainlinkLatest:
    """Latest Chainlink answer per catalog feed from `latestRoundData()`, memoized briefly.

    One `eth_call` per feed (per component for a composite feed), memoized for
    `ttl`, so prices and oracle health read each feed once per analysis. A feed
    with no round (a zero round id or update time) returns None.
    """

    def __init__(
        self,
        client,
        *,
        ttl: timedelta = CHAINLINK_MEMO_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self._ttl = ttl
        self.clock = clock
        self._memo: dict[str, tuple[datetime, Observation | None]] = {}
        self._lock = threading.Lock()

    def latest(self, feed: str) -> Observation | None:
        now = self.clock()
        with self._lock:
            cached = self._memo.get(feed)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
        latest = self.client.get_latest_observation(feed)
        with self._lock:
            self._memo[feed] = (now, latest)
        return latest


class ChainlinkPrices:
    """Latest Chainlink answer, for gas and stablecoin valuation."""

    FEEDS: Mapping[str, str] = {"weth": "ETH/USD", "eth": "ETH/USD", "usdc": "USDC/USD"}

    def __init__(self, reader: ChainlinkLatest) -> None:
        self.reader = reader

    def price_usd(self, symbol: str) -> Decimal | None:
        feed = self.FEEDS.get(symbol.casefold())
        if feed is None:
            return None
        latest = self.reader.latest(feed)
        return latest.value if latest is not None else None


class ChainlinkOracleHealth:
    """`OracleHealthProvider` that decodes a Morpho market's oracle into its feeds.

    The oracle contract is decoded on-chain (`app.adapters.morpho_oracle`): its
    BASE/QUOTE feeds, vaults and scale factor, then each feed's description,
    decimals and kind. Each observation then costs one multicall: the oracle's
    `price()`, every feed's `latestRoundData()` and the block timestamp.

    - `age_seconds` is the staleness *beyond the heartbeat*: max(0, now - feed
      update time - heartbeat), the largest over the oracle's feeds whose
      staleness is judged. The engine's unchanged 1800 s threshold therefore
      fires when a feed is older than its heartbeat plus 30 minutes (spec § 13
      interpretation; see the module docstring).
    - Heartbeats. Chainlink feeds: `CHAINLINK_HEARTBEATS` for catalog feeds,
      otherwise inferred from the feed's last 90 days of rounds (99th-percentile
      update gap, `app.history.staleness`, one missed cycle tolerated).
      Third-party feeds, in this order: (a) the published heartbeat in
      `ThirdPartyFeedRegistry` (address and description must match), (b) the
      same inference when `getRoundData` history is readable, (c) unknown.
    - Feeds that compute their answer when read (exchange-rate adapters,
      computed-on-read feeds, constant scalers) have zero staleness. Their
      `updatedAt` must still be 0 or the read block's timestamp; otherwise the
      feed no longer behaves as classified and the oracle is unobservable.
    - A third-party feed with an unknown heartbeat is *observed*, not missing:
      its age (`last_update_age_seconds`) and provider are reported, its
      staleness is not judged (`staleness_seconds` None), so it never fires the
      live staleness trigger: a guessed threshold could force a false EXIT on a
      feed that updates monthly by design (an RWA NAV feed). When every feed of
      the oracle is unjudged, `age_seconds` is None.
    - `deviation` is |oracle price - reference| / reference, where the oracle
      price is `price()` as collateral per loan token and the reference is the
      Morpho snapshot's collateral USD price over its loan USD price. Without a
      reference it is None and only staleness is evaluated. Deviation is judged
      for every oracle, whatever its feeds.

    Returns None (explicitly unknown, never healthy) when the oracle is not a
    Morpho Chainlink oracle, reads a feed of unknown kind, a feed has no
    answer, or a Chainlink feed's heartbeat is neither documented nor inferable.
    Provider errors propagate; the service records only their type.
    """

    def __init__(
        self,
        reader: ChainlinkLatest,
        *,
        oracles=None,
        heartbeats: Mapping[str, int] = CHAINLINK_HEARTBEATS,
        inference_lookback: timedelta = HEARTBEAT_INFERENCE_LOOKBACK,
        third_party=None,
    ) -> None:
        from app.adapters.morpho_oracle import MorphoOracleReader, ThirdPartyFeedRegistry

        self.reader = reader
        self._client = reader.client
        self._oracles = oracles or MorphoOracleReader(reader.client)
        self._heartbeats = dict(heartbeats)
        self._lookback = inference_lookback
        self._third_party = third_party if third_party is not None else ThirdPartyFeedRegistry.load()
        self._decoded: dict[str, tuple[datetime | None, object | None]] = {}
        self._inferred: dict[str, tuple[datetime, tuple[int, str] | None]] = {}
        self._lock = threading.Lock()

    def observe(
        self, market: MarketSnapshot, *, now: datetime
    ) -> DataEnvelope[OracleObservation] | None:
        from app.adapters.morpho_oracle import NEVER_STALE_KINDS, FeedKind

        oracle = self._decode(market.oracle_address, now)
        if oracle is None or not oracle.decodable:
            return None
        state = self._oracles.read_state(oracle)
        feeds: list[OracleFeedObservation] = []
        stalenesses: list[Decimal] = []
        unjudged = 0
        for feed, latest in zip(oracle.feeds, state.latest, strict=True):
            if latest is None or feed.decimals is None:
                return None
            answer = Decimal(latest.answer).scaleb(-feed.decimals)
            common = {
                "role": feed.role,
                "address": feed.address,
                "kind": feed.kind.value,
                "description": feed.description,
                "answer": answer,
                "provider": feed.provider,
            }
            if feed.kind in NEVER_STALE_KINDS:
                if feed.kind is not FeedKind.EXCHANGE_RATE_ADAPTER and not _computed_now(
                    latest.updated_at, state.block_timestamp
                ):
                    logger.warning("a computed-on-read feed returned a stored update time")
                    return None
                if feed.kind is FeedKind.CONSTANT_SCALER and latest.answer != feed.constant_answer:
                    logger.warning("a constant scaler feed changed its answer")
                    return None
                feeds.append(
                    OracleFeedObservation(
                        **common,
                        heartbeat_source=NEVER_STALE_SOURCES[feed.kind.value],
                        staleness_seconds=ZERO,
                    )
                )
                stalenesses.append(ZERO)
                continue
            if latest.updated_at <= 0:
                return None
            age = Decimal(max(0, int(now.timestamp()) - latest.updated_at))
            if feed.kind is FeedKind.THIRD_PARTY:
                heartbeat = self._third_party_heartbeat(feed, now)
                common["history_readable"] = feed.history_readable
                if heartbeat is None:
                    unjudged += 1
                    feeds.append(
                        OracleFeedObservation(
                            **common,
                            last_update_age_seconds=age,
                            heartbeat_source=UNKNOWN_HEARTBEAT_SOURCE,
                        )
                    )
                    continue
            else:
                heartbeat = self._heartbeat(feed.address, now)
                if heartbeat is None:
                    return None
            seconds, source = heartbeat
            staleness = max(ZERO, age - Decimal(seconds))
            stalenesses.append(staleness)
            feeds.append(
                OracleFeedObservation(
                    **common,
                    last_update_age_seconds=age,
                    heartbeat_seconds=Decimal(seconds),
                    heartbeat_source=source,
                    staleness_seconds=staleness,
                )
            )
        names = "+".join(feed.description or feed.address for feed in oracle.feeds) or "no feeds"
        note = (
            f"; staleness of {unjudged} third-party feed(s) with no published heartbeat is not judged"
            if unjudged
            else ""
        )
        return DataEnvelope[OracleObservation](
            value=OracleObservation(
                oracle_address=market.oracle_address,
                age_seconds=max(stalenesses) if stalenesses else (None if unjudged else ZERO),
                deviation=_oracle_deviation(state.price, market),
                oracle_type=oracle.oracle_type,
                feeds=tuple(feeds),
            ),
            source=(
                f"chainlink:{oracle.oracle_type}({names}) (age_seconds is staleness beyond "
                f"the feed heartbeat{note}; deviation of price() versus the Morpho price)"
            ),
            fetched_at=now,
            freshness=FreshnessStatus.FRESH,
        )

    def _decode(self, address: str, now: datetime):
        key = address.lower()
        with self._lock:
            cached = self._decoded.get(key)
        if cached is not None and (cached[0] is None or now < cached[0]):
            return cached[1]
        oracle = self._oracles.decode(key)
        expires = None if oracle is not None else now + ORACLE_DECODE_RETRY
        with self._lock:
            self._decoded[key] = (expires, oracle)
        return oracle

    def _third_party_heartbeat(self, feed, now: datetime) -> tuple[int, str] | None:
        """Published heartbeat, else inferred from readable round history, else None."""
        published = self._third_party.lookup(feed.address, feed.description)
        if published is not None:
            return (
                published.heartbeat_seconds,
                f"{PUBLISHED_HEARTBEAT_SOURCE_PREFIX} by {published.provider} "
                f"({published.source_url})",
            )
        if not feed.history_readable:
            return None
        from app.adapters.ethereum import UnsupportedFeedError

        try:
            return self._infer(feed.address, now, require_phase=False)
        except UnsupportedFeedError:
            return None

    def _heartbeat(self, address: str, now: datetime) -> tuple[int, str] | None:
        feed_by_address = getattr(self._client, "feed_by_address", None)
        definition = feed_by_address(address) if callable(feed_by_address) else None
        if definition is not None and definition.feed_id in self._heartbeats:
            return self._heartbeats[definition.feed_id], f"documented ({definition.feed_id})"
        return self._infer(address, now)

    def _infer(self, address: str, now: datetime, *, require_phase: bool = True) -> tuple[int, str] | None:
        key = address.lower()
        with self._lock:
            cached = self._inferred.get(key)
        if cached is not None and now - cached[0] < HEARTBEAT_MEMO_TTL:
            return cached[1]
        from app.history.staleness import HEARTBEAT_PERCENTILE, infer_heartbeat

        if require_phase:
            updates = self._client.get_feed_updates(address, now - self._lookback, now)
        else:
            updates = self._client.get_feed_updates(
                address, now - self._lookback, now, require_phase=False
            )
        inferred = infer_heartbeat([item.timestamp for item in updates])
        # An inferred cadence is not a contractual heartbeat: about 1% of gaps
        # exceed the percentile by construction, so a feed counts as late only
        # after one full missed cycle (live trigger and staleness history alike).
        result = (
            (
                inferred * INFERRED_HEARTBEAT_TOLERANCE_CYCLES,
                f"{INFERRED_HEARTBEAT_SOURCE_PREFIX} (p{HEARTBEAT_PERCENTILE} of {len(updates) - 1} "
                f"update gaps over {self._lookback.days} days is {inferred} s; one missed cycle "
                "is tolerated)",
            )
            if inferred is not None
            else None
        )
        with self._lock:
            self._inferred[key] = (now, result)
        return result


def _computed_now(updated_at: int, block_timestamp: int | None) -> bool:
    """A computed-on-read feed reports no update time, or the read block's own time."""
    return updated_at == 0 or (block_timestamp is not None and updated_at == block_timestamp)


def _oracle_deviation(price: int | None, market: MarketSnapshot) -> Decimal | None:
    """|price() as collateral per loan token - Morpho USD ratio| / Morpho USD ratio."""
    collateral = market.collateral_asset.price_usd
    loan = market.loan_asset.price_usd
    if price is None or price <= 0 or collateral is None or loan is None:
        return None
    if not (collateral.is_finite() and loan.is_finite()) or collateral <= ZERO or loan <= ZERO:
        return None
    exponent = 36 + market.loan_asset.decimals - market.collateral_asset.decimals
    with localcontext() as context:
        context.prec = 60
        oracle_price = Decimal(price).scaleb(-exponent)
        reference = collateral / loan
        return abs(oracle_price - reference) / reference


class FileAndDefiScanEvidence:
    """Task 8 evidence: the version-controlled DeFiSafety snapshot plus a live DeFiScan review.

    DeFiScan reviews are cached in process per protocol slug for a day (spec § 9:
    risk evidence refreshes daily); a failed fetch is retried after five minutes.
    The bundle's own `retrieved_at` is kept, so scoring still sees its true age.
    An in-process cache is used because `EvidenceBundle` is a nested dataclass
    with no serializer for `SQLiteCache`.

    With a `DefiLlamaClient`, it also offers `impairment_base_rate`: the empirical
    annual rate of material exploits at large lending protocols
    (`app.evidence.defillama`) as a 30-day probability, and
    `oracle_exploit_base_rate`: the same rate restricted to oracle-related
    exploits, the prior for third-party oracle feeds without measurable history. DeFiLlama data is cached
    daily by the client; the computed estimate is memoized per data version.
    """

    def __init__(
        self,
        defisafety_path: Path,
        defiscan=None,
        *,
        defillama=None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = defisafety_path
        self._defiscan = defiscan
        self._defillama = defillama
        self._clock = clock
        self._reviews: dict[str, tuple[datetime, object]] = {}
        self._base_rates: dict[tuple, ProbabilityEstimate] = {}
        self._lock = threading.Lock()

    def impairment_base_rate(self, protocol: str, *, now: datetime) -> ProbabilityEstimate:
        from app.evidence.defillama import base_rate_estimate

        return self._defillama_rate(
            ("impairment", protocol),
            now,
            lambda hacks, protocols: base_rate_estimate(protocol, hacks, protocols, now=now),
            "DeFiLlama lists no lending protocol-years for the base rate.",
        )

    def oracle_exploit_base_rate(self, *, now: datetime) -> ProbabilityEstimate:
        """The industry oracle-exploit prior (`app.evidence.defillama.oracle_exploit_estimate`)."""
        from app.evidence.defillama import oracle_exploit_estimate

        return self._defillama_rate(
            ("oracle",),
            now,
            lambda hacks, protocols: oracle_exploit_estimate(hacks, protocols, now=now),
            "DeFiLlama lists no lending protocol-years for the oracle-exploit prior.",
        )

    def _defillama_rate(self, kind: tuple, now: datetime, compute, empty: str) -> ProbabilityEstimate:
        from app.evidence.defillama import observation_window

        if self._defillama is None:
            return unavailable_estimate(now, "No DeFiLlama source is configured.")
        try:
            hacks = self._defillama.hacks()
            protocols = self._defillama.protocols()
        except Exception as error:  # provider boundary: never leak provider text
            logger.warning("defillama unavailable: %s", type(error).__name__)
            return unavailable_estimate(
                now, "The DeFiLlama exploit and protocol data could not be retrieved."
            )
        key = (
            *kind,
            observation_window(now),
            hacks.fetched_at,
            hacks.freshness,
            protocols.fetched_at,
            protocols.freshness,
        )
        with self._lock:
            cached = self._base_rates.get(key)
        if cached is not None:
            return cached
        estimate = compute(hacks, protocols) or unavailable_estimate(now, empty)
        with self._lock:
            # Keep only the current data version of each kind of rate.
            self._base_rates = {
                item: value for item, value in self._base_rates.items() if item[: len(kind)] != kind
            }
            self._base_rates[key] = estimate
        return estimate

    def _review(self, protocol: str):
        """The cached review, a fresh one, or None after a (cached) failure."""
        with self._lock:
            cached = self._reviews.get(protocol)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        try:
            review = self._defiscan.get_review(protocol)
            expires = self._clock() + DEFISCAN_TTL
        except Exception as error:  # provider boundary: never leak provider text
            logger.warning("defiscan unavailable: %s", type(error).__name__)
            review = None
            expires = self._clock() + DEFISCAN_FAILURE_TTL
        with self._lock:
            self._reviews[protocol] = (expires, review)
        return review

    def evidence(self, protocol: str, *, now: datetime) -> CombinedEvidence:
        from app.adapters.defiscan import DEFISCAN_METHODOLOGY_URL
        from app.evidence.models import EvidenceBundle, EvidenceSource
        from app.evidence.scoring import load_defisafety_evidence

        bundles = [load_defisafety_evidence(self._path, protocol)]
        if self._defiscan is not None:
            review = self._review(protocol)
            if review is not None:
                bundles.append(review)
            else:
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


class DefiLlamaProtocolTvl:
    """Protocol TVL and chain TVL share from DeFiLlama `/protocols` and `/v2/chains`."""

    def __init__(self, client) -> None:
        self._client = client

    def protocol_tvl(
        self, protocol: str, *, now: datetime
    ) -> DataEnvelope[ProtocolTvlSnapshot] | None:
        from app.evidence.defillama import protocol_tvl_snapshot

        protocols = self._client.protocols()
        chains = self._client.chains()
        snapshot = protocol_tvl_snapshot(protocol, protocols.value, chains.value)
        if snapshot is None:
            return None
        order = (FreshnessStatus.FRESH, FreshnessStatus.STALE_USABLE, FreshnessStatus.EXPIRED)
        return DataEnvelope[ProtocolTvlSnapshot](
            value=snapshot,
            source=f"{protocols.source}; {chains.source}",
            fetched_at=min(protocols.fetched_at, chains.fetched_at),
            freshness=max((protocols.freshness, chains.freshness), key=order.index),
        )
