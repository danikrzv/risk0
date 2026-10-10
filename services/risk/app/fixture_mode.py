"""Deterministic fixture mode: `RISK0_DATA_MODE=fixture`.

Fixture mode runs the unchanged FastAPI routes, `CatalogService`,
`AnalysisService`, `MorphoClient`, `SQLiteCache`, graph builder, scenario
engine, exit-cost service, and recommendation rule. Only the outermost data
sources are swapped, through the same dependency-injection points the live
wiring uses (`get_catalog_service`, `get_default_service`):

- Morpho: the real `MorphoClient` over an in-process `httpx.MockTransport` that
  serves the recorded, sanitized responses in `tests/fixtures/morpho/` (the same
  data the API tests use). No socket is opened.
  `belem-allocated-markets.json` holds the public responses for the markets
  the Belem RWA EURCV vault allocates to, so its graph has no unknown exposure.
- Uniswap quotes, gas, ETH/USD and USDC/USD prices, live oracle health, and
  30-day probabilities: small deterministic providers below, implementing the
  `AnalysisService` provider protocols. Their sources are labelled `fixture:*`,
  so a fixture response can never be mistaken for live data. Probabilities are
  illustrative fixture values (`FIXTURE_PROBABILITIES`) for all five scenarios,
  because live mode has no probability for oracle failure or liquidity collapse
  and no feed for EURCV or wstETH, which would make every default run
  UNAVAILABLE.

One entry is deliberately degraded so the `UNAVAILABLE` path can be demonstrated
deterministically: the WBTC/USDC market (`DEGRADED_MARKET_ID`). Its live oracle
observation is EXPIRED, and every quote that sells WBTC is two minutes old, so
the single refresh returns an expired quote too. Every other entry sees healthy
oracles (60 s past heartbeat, 0.1% deviation) and quotes at the snapshot block.

Nothing here is used in live mode, which is the default.
"""

import importlib.util
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import httpx

from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
    ScenarioId,
)


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
DEFAULT_FIXTURE_ROOT = SERVICE_ROOT / "tests" / "fixtures"
# Recorded from https://api.morpho.org; served under a reserved `.invalid` host
# so every fixture source URL is visibly not live data and cannot resolve.
MORPHO_BASE_URL = "https://morpho.fixture.invalid"

DATA_MODE_ENV = "RISK0_DATA_MODE"
LIVE = "live"
FIXTURE = "fixture"
FIXTURE_RADAR_CATALOG_VAULT = "vault:1:0x1111111111111111111111111111111111111111"
FIXTURE_RADAR_ORACLE = "oracle:1:0x4444444444444444444444444444444444444444"
FIXTURE_RADAR_RULE = "fixture-oracle-v1"

# Recorded fixture snapshot block (tests/fixtures/morpho/*.json "last_indexed_block").
SNAPSHOT_BLOCK = 26161049
GAS_PRICE_WEI = Decimal("20000000000")
# Prices recorded alongside the Morpho fixtures.
ETH_USD = Decimal("2492.8486759318967")
USDC_USD = Decimal("0.9997496425860051")

DEGRADED_MARKET_ID = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
DEGRADED_SYMBOL = "WBTC"
DEGRADED_QUOTE_AGE_SECONDS = 120

# Fixture 30-day central probabilities, keyed by scenario id. Illustrative
# values chosen for a reproducible demo, not measured; every estimate is
# labelled `fixture:probabilities`. Protocol impairment keeps its
# model-estimated method label (its scenario's method) with a fixture model id.
FIXTURE_PROBABILITIES: dict[ScenarioId, Decimal] = {
    ScenarioId.STABLECOIN_DEPEG: Decimal("0.0001"),
    ScenarioId.COLLATERAL_SHOCK: Decimal("0.001"),
    ScenarioId.ORACLE_FAILURE: Decimal("0.0001"),
    ScenarioId.LIQUIDITY_COLLAPSE: Decimal("0.0001"),
    ScenarioId.PROTOCOL_IMPAIRMENT: Decimal("0.0001"),
}
FIXTURE_PROTOCOL_MODEL = "fixture-protocol-v1"


class DataModeError(ValueError):
    pass


def data_mode() -> str:
    """`live` (default) or `fixture`; anything else is a configuration error."""
    value = os.environ.get(DATA_MODE_ENV, LIVE).strip().lower() or LIVE
    if value not in (LIVE, FIXTURE):
        raise DataModeError(f"{DATA_MODE_ENV} must be 'live' or 'fixture'")
    return value


def fixture_root() -> Path:
    return Path(os.environ.get("RISK0_FIXTURE_DIR", DEFAULT_FIXTURE_ROOT))


def _fixture_cache_path() -> Path:
    """A per-process cache, never the live cache: fixture snapshots must not leak into live mode."""
    configured = os.environ.get("RISK0_FIXTURE_CACHE_PATH")
    if configured:
        return Path(configured)
    return Path(tempfile.mkdtemp(prefix="risk0-fixture-")) / "risk0-fixture.sqlite3"


# Morpho: recorded responses behind the real client.


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordedMorphoApi:
    """Serves the recorded Morpho REST and GraphQL fixtures; unknown paths are 404."""

    def __init__(self, root: Path) -> None:
        morpho = root / "morpho"
        self.rest: dict[str, object] = {}
        self.graphql: dict[str, object] = {}
        for name in (
            "market-state.json",
            "vault-state.json",
            "vault-allocations.json",
            "belem-allocated-markets.json",
        ):
            recorded = json.loads((morpho / name).read_text())
            self.rest.update(recorded.get("rest", {}))
            self.graphql.update(recorded.get("graphql", {}))
        rockawayx = _load_module(morpho / "rockawayx.py", "risk0_fixture_rockawayx")
        self._graphql_key = rockawayx.graphql_key
        for vault in rockawayx.ROCKAWAYX_VAULTS:
            self.rest.update(vault.rest())
            self.graphql.update(vault.graphql())

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/graphql":
            payload = json.loads(request.content)
            body = self.graphql.get(self._key(payload["query"], payload["variables"]))
        else:
            body = self.rest.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"message": "not found", "statusCode": 404})
        return httpx.Response(200, json=body)

    def _key(self, query: str, variables: dict[str, object]) -> str:
        chain_id = variables["chainId"]
        if "marketById" in query:
            return self._graphql_key("market", variables["marketId"], chain_id)
        if "vaultV2ByAddress" in query:
            return self._graphql_key("vault_v2", variables["address"], chain_id)
        return self._graphql_key("vault_v1", variables["address"], chain_id)


@lru_cache(maxsize=1)
def fixture_morpho_client():
    from app.adapters.morpho import MorphoClient
    from app.infrastructure.cache import SQLiteCache

    api = RecordedMorphoApi(fixture_root())
    return MorphoClient(
        MORPHO_BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(api.handler)),
        cache=SQLiteCache(_fixture_cache_path()),
        sleep=lambda _: None,
    )


# Analysis providers.


class FixtureQuotes:
    """`QuoteProvider` shaped like QuoterV2: 0.05% fee, 0.1% price impact, 0.5% slippage buffer.

    Quotes are at the snapshot block and current, except quotes selling the
    degraded symbol, which are always `DEGRADED_QUOTE_AGE_SECONDS` old (an
    expired quote that a refresh cannot fix).
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def quote(self, request, *, refresh: bool):
        from app.domain.exit import ExitQuote, ExitRoute
        from app.domain.snapshots import DataEnvelope

        token_in, token_out = request.token_in, request.token_out
        if token_in.price_usd is None or token_out.price_usd is None:
            raise ValueError("fixture quotes need USD prices for both tokens")
        degraded = token_in.symbol.upper() == DEGRADED_SYMBOL
        quoted_at = self._clock() - timedelta(seconds=DEGRADED_QUOTE_AGE_SECONDS if degraded else 0)
        unit = Decimal(1).scaleb(-token_out.decimals)
        marginal = (request.amount_in * token_in.price_usd / token_out.price_usd).quantize(unit)
        fee = (marginal * Decimal("0.0005")).quantize(unit)
        impact = (marginal * Decimal("0.001")).quantize(unit)
        out = marginal - fee - impact
        quote = ExitQuote(
            request=request,
            route=ExitRoute(
                path=(token_in.address, token_out.address),
                fee_tiers=(500,),
                symbols=(token_in.symbol, token_out.symbol),
            ),
            amount_in=request.amount_in,
            amount_out=out,
            minimum_amount_out=(out * Decimal("0.995")).quantize(unit),
            marginal_amount_out=marginal,
            fee_fraction=Decimal("0.0005"),
            fee_cost_out=fee,
            price_impact=Decimal("0.001"),
            price_impact_cost_out=impact,
            gas_estimate=120_000,
            gas_price_wei=GAS_PRICE_WEI,
            gas_cost_eth=Decimal("0.0024"),
            gas_cost_out=None,
            net_amount_out=out,
            block_number=SNAPSHOT_BLOCK,
            quoted_at=quoted_at,
            candidates=1,
            reverted=0,
        )
        return DataEnvelope[ExitQuote](
            value=quote,
            source="fixture:uniswap-v3-quoter",
            fetched_at=quoted_at,
            freshness=FreshnessStatus.FRESH,
            block_number=SNAPSHOT_BLOCK,
        )


class FixtureGas:
    def gas_price_wei(self) -> Decimal:
        return GAS_PRICE_WEI


class FixturePrices:
    PRICES = {"WETH": ETH_USD, "ETH": ETH_USD, "USDC": USDC_USD}

    def price_usd(self, symbol: str) -> Decimal | None:
        return self.PRICES.get(symbol.upper())


class FixtureOracles:
    """`OracleHealthProvider`: healthy everywhere except the degraded market (EXPIRED)."""

    def observe(self, market, *, now: datetime):
        from app.domain.graph import OracleObservation
        from app.domain.snapshots import DataEnvelope

        degraded = market.market_id.lower() == DEGRADED_MARKET_ID
        if degraded:
            observed_at = now - timedelta(hours=6)
            return DataEnvelope[OracleObservation](
                value=OracleObservation(
                    oracle_address=market.oracle_address, age_seconds=Decimal(21600)
                ),
                source="fixture:oracle-health (simulated outage, refresh failed)",
                fetched_at=observed_at,
                freshness=FreshnessStatus.EXPIRED,
            )
        return DataEnvelope[OracleObservation](
            value=OracleObservation(
                oracle_address=market.oracle_address,
                age_seconds=Decimal(60),
                deviation=Decimal("0.001"),
            ),
            source="fixture:oracle-health",
            fetched_at=now,
            freshness=FreshnessStatus.FRESH,
        )


class FixtureProbabilities:
    """`ProbabilityProvider` returning `FIXTURE_PROBABILITIES` for every scenario."""

    def assess(self, selection, graph, position, *, now: datetime):
        from app.analysis.service import ProbabilityAssessment

        scenario = selection.scenario_id
        central = FIXTURE_PROBABILITIES[scenario]
        protocol = scenario is ScenarioId.PROTOCOL_IMPAIRMENT
        explanation = (
            f"Fixture-mode probability for {scenario.value} at severity {selection.severity}; "
            "illustrative, not estimated from live data."
        )
        if protocol:
            explanation = (
                f"model-estimated protocol impairment probability ({FIXTURE_PROTOCOL_MODEL}); "
                + explanation
            )
        return ProbabilityAssessment(
            ProbabilityEstimate(
                method=(
                    ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
                    if protocol
                    else ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
                ),
                low=central / 2,
                central=central,
                high=central * 2,
                confidence=ConfidenceLevel.MEDIUM,
                horizon_days=30,
                source="fixture:probabilities",
                retrieved_at=now,
                freshness=FreshnessStatus.FRESH,
                explanation=explanation,
            )
        )


def build_fixture_catalog_service():
    from app.catalog.repository import CatalogRepository
    from app.catalog.service import CatalogService

    return CatalogService(CatalogRepository(_catalog_path()), fixture_morpho_client())


def build_fixture_analysis_service():
    from app.analysis.service import AnalysisService, NoIncidents
    from app.catalog.repository import CatalogRepository

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    return AnalysisService(
        CatalogRepository(_catalog_path()),
        fixture_morpho_client(),
        probabilities=FixtureProbabilities(),
        quotes=FixtureQuotes(clock),
        gas=FixtureGas(),
        prices=FixturePrices(),
        oracles=FixtureOracles(),
        incidents=NoIncidents(),
        clock=clock,
    )


def seed_fixture_radar(context) -> None:
    """Seed a deterministic healthy topology for UI and E2E demos."""
    from app.radar.contracts import (
        HealthState,
        RadarEntity,
        RadarRelationship,
        SimilarityRecord,
        StateSnapshot,
    )

    observed_at = datetime(2026, 10, 10, 12, tzinfo=UTC)
    chain = "chain:1:ethereum"
    protocol = "protocol:1:morpho"
    catalog_vault = FIXTURE_RADAR_CATALOG_VAULT
    visible_neighbor = "vault:1:0x2222222222222222222222222222222222222222"
    hidden_neighbor = "vault:1:0x3333333333333333333333333333333333333333"
    market = "market:1:0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    oracle = FIXTURE_RADAR_ORACLE
    asset = "asset:1:0x5555555555555555555555555555555555555555"
    if context.topology.get_entity(catalog_vault) is not None:
        return
    entities = (
        RadarEntity(entity_id=chain, entity_type="chain", chain_id=1, address_or_key="ethereum", label="Ethereum", source="fixture:radar"),
        RadarEntity(entity_id=protocol, entity_type="protocol", chain_id=1, address_or_key="morpho", label="Morpho", source="fixture:radar"),
        RadarEntity(entity_id=catalog_vault, entity_type="vault", chain_id=1, address_or_key=catalog_vault.rsplit(":", 1)[1], label="Belem Sentinel USDC", source="fixture:radar", catalog=True, metadata={"curator": "belem"}),
        RadarEntity(entity_id=visible_neighbor, entity_type="vault", chain_id=1, address_or_key=visible_neighbor.rsplit(":", 1)[1], label="Institutional USDC Peer", source="fixture:radar", visible=True, metadata={"curator": "peer"}),
        RadarEntity(entity_id=hidden_neighbor, entity_type="vault", chain_id=1, address_or_key=hidden_neighbor.rsplit(":", 1)[1], label="Hidden 48% Neighbor", source="fixture:radar", visible=False, metadata={"curator": "peer"}),
        RadarEntity(entity_id=market, entity_type="market", chain_id=1, address_or_key=market.rsplit(":", 1)[1], label="WETH / USDC 86%", source="fixture:radar", metadata={"lltv": Decimal("0.86")}),
        RadarEntity(entity_id=oracle, entity_type="oracle", chain_id=1, address_or_key=oracle.rsplit(":", 1)[1], label="WETH / USDC Oracle", source="fixture:radar"),
        RadarEntity(entity_id=asset, entity_type="asset", chain_id=1, address_or_key=asset.rsplit(":", 1)[1], label="USDC", source="fixture:radar"),
    )
    relationships = (
        RadarRelationship(source_entity_id=catalog_vault, target_entity_id=market, relationship_type="allocated_to", weight=Decimal("0.60")),
        RadarRelationship(source_entity_id=visible_neighbor, target_entity_id=market, relationship_type="allocated_to", weight=Decimal("0.45")),
        RadarRelationship(source_entity_id=hidden_neighbor, target_entity_id=market, relationship_type="allocated_to", weight=Decimal("0.48")),
        RadarRelationship(source_entity_id=market, target_entity_id=oracle, relationship_type="uses_oracle"),
        RadarRelationship(source_entity_id=market, target_entity_id=asset, relationship_type="lends"),
        RadarRelationship(source_entity_id=market, target_entity_id=protocol, relationship_type="deployed_on"),
        RadarRelationship(source_entity_id=protocol, target_entity_id=chain, relationship_type="deployed_on"),
    )
    context.topology.upsert_entities(entities, seen_at=observed_at)
    context.topology.upsert_relationships(relationships, seen_at=observed_at)
    context.topology.replace_similarities(
        catalog_vault,
        (
            SimilarityRecord(catalog_entity_id=catalog_vault, candidate_entity_id=visible_neighbor, score=Decimal("0.72"), components={"shared_markets": Decimal("0.35"), "asset_overlap": Decimal("0.20"), "same_chain": Decimal("0.05"), "lltv_proximity": Decimal("0.08"), "oracle_overlap": Decimal("0.04")}, stored=True, normally_visible=True, computed_at=observed_at),
            SimilarityRecord(catalog_entity_id=catalog_vault, candidate_entity_id=hidden_neighbor, score=Decimal("0.48"), components={"shared_markets": Decimal("0.25"), "asset_overlap": Decimal("0.15"), "same_chain": Decimal("0.05"), "lltv_proximity": Decimal("0.03")}, stored=True, normally_visible=False, computed_at=observed_at),
        ),
    )
    for entity in entities:
        context.states.append_snapshot(
            StateSnapshot(
                snapshot_id=f"{entity.entity_id}|fixture|{observed_at.isoformat()}",
                entity_id=entity.entity_id,
                state_type=entity.entity_type,
                observed_at=observed_at,
                retrieved_at=observed_at,
                source="fixture:radar",
                source_identity="fixture:radar",
                freshness="fresh",
                metrics={"status": "HEALTHY"},
            )
        )
        context.states.set_health(
            HealthState(
                entity_id=entity.entity_id,
                status="HEALTHY",
                score=Decimal("1"),
                reasons=("fixture measurements normal",),
                missing_fields=(),
                evaluated_at=observed_at,
                rule_version="fixture-health-v1",
            )
        )


def open_fixture_radar_incident(context, broker, now: datetime | None = None):
    from app.radar.analysis import ExposureImpactEstimator, PropagationEngine
    from app.radar.contracts import Incident, IncidentSeverity, IncidentStatus, Notification

    seed_fixture_radar(context)
    observed_at = now or datetime.now(UTC)
    active = context.incidents.active(source_entity_id=FIXTURE_RADAR_ORACLE)
    if active:
        incident = active[0]
    else:
        incident = Incident(
            incident_id=f"incident:fixture:oracle-deviation:{int(observed_at.timestamp())}",
            source_entity_id=FIXTURE_RADAR_ORACLE,
            severity=IncidentSeverity.CRITICAL,
            status=IncidentStatus.OPEN,
            trigger_metrics={"deviation": Decimal("0.08")},
            rule_version=FIXTURE_RADAR_RULE,
            first_observed_at=observed_at,
            last_observed_at=observed_at,
            reasons=("oracle deviated 8% from peer consensus",),
        )
        incident = context.incidents.upsert_active(incident).incident
        notification = Notification(
            notification_id=f"notification:{incident.incident_id}",
            incident_id=incident.incident_id,
            severity=incident.severity,
            title="Critical oracle deviation",
            message="WETH / USDC oracle deviated 8% from peer consensus.",
            created_at=observed_at,
        )
        if context.incidents.add_notification(notification):
            broker.publish("notification.created", notification.notification_id, {"incident_id": incident.incident_id, "severity": incident.severity.value})
        broker.publish("incident.opened", incident.incident_id, {"source_entity_id": incident.source_entity_id, "severity": incident.severity.value})

    relationships = context.topology.relationships()
    impacts = PropagationEngine().propagate(
        incident,
        context.topology.entities(),
        relationships,
        context.topology.similarities(),
    )
    context.incidents.replace_impacts(incident.incident_id, impacts)
    estimator = ExposureImpactEstimator()
    for exposure in context.exposures.list_exposures():
        result = estimator.estimate(exposure, incident, impacts, relationships)
        context.exposures.replace_impacts(exposure.exposure_id, result.impacts)
    broker.publish("graph.updated", "graph", {"fixture": True, "incident_id": incident.incident_id})
    return incident


def resolve_fixture_radar_incident(context, broker, now: datetime | None = None):
    active = context.incidents.active(source_entity_id=FIXTURE_RADAR_ORACLE)
    if not active:
        return None
    resolved = context.incidents.resolve(active[0].incident_id, now or datetime.now(UTC))
    if resolved is not None:
        broker.publish("incident.resolved", resolved.incident_id, {"source_entity_id": resolved.source_entity_id})
        broker.publish("graph.updated", "graph", {"fixture": True, "incident_id": resolved.incident_id})
    return resolved


def _catalog_path() -> Path:
    return Path(
        os.environ.get("RISK0_CATALOG_PATH", REPOSITORY_ROOT / "data" / "catalog" / "ethereum.json")
    )
