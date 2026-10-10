"""A synthetic, live-sized radar for scale tests.

`build_synthetic_radar` writes ~20k entities, ~50k relationships, and ~20k open
incidents through the real repositories. It backs the graph-payload budget test
(`tests/api/test_radar_graph.py`) and the browser-level scale suite
(`apps/web/e2e-scale`, seeded by `scripts/seed_synthetic_radar.py`).

The data is load-shaped, not realistic: every entity is WARNING and incidents
are written directly rather than opened by the health rules. Nothing in the
service imports this module.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.radar.contracts import (
    HealthState,
    HealthStatus,
    Incident,
    IncidentImpact,
    IncidentSeverity,
    IncidentStatus,
    Notification,
    RadarEntity,
    RadarEntityType,
    RadarRelationship,
    SimilarityRecord,
)
from app.radar.repositories import (
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)


SYNTHETIC_OBSERVED_AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
SYNTHETIC_SOURCE = "synthetic:radar"
# The stablecoin hub's open incident is propagated to this many vaults. It is
# not among the incidents the pruned graph lists; a notification points to it.
HUB_INCIDENT_REACH = 5_000
HUB_INCIDENT_TITLE = "Stablecoin hub incident"


class SyntheticRadarTarget(Protocol):
    """The repositories a synthetic radar is written through (e.g. a `RadarContext`)."""

    @property
    def database(self) -> RadarDatabase | None: ...

    @property
    def topology(self) -> TopologyRepository: ...

    @property
    def states(self) -> StateRepository: ...

    @property
    def incidents(self) -> IncidentRepository: ...


def build_synthetic_radar(
    context: SyntheticRadarTarget,
    *,
    markets: int = 8_000,
    vaults: int = 11_700,
    incidents: int = 20_000,
    now: datetime = SYNTHETIC_OBSERVED_AT,
) -> dict[str, tuple[str, ...]]:
    """A live-sized radar: ~20k entities, ~50k relationships, ~20k incidents.

    Shaped like Morpho: a few hubs (chain, protocol, a stablecoin, an IRM),
    a catalog of 15 vaults whose dependencies are visible, and one open
    incident per entity with propagated paths (one incident reaches 5,000
    targets, announced by the radar's only notification).
    """
    if context.database is None:
        raise ValueError("a synthetic radar is written in one database transaction")
    chain = "chain:1:ethereum"
    protocol = "protocol:1:morpho"
    assets = [f"asset:1:asset-{index:03d}" for index in range(200)]
    oracles = [f"oracle:1:oracle-{index:03d}" for index in range(150)]
    irms = [f"irm:1:irm-{index}" for index in range(5)]
    market_ids = [f"market:1:market-{index:05d}" for index in range(markets)]
    vault_ids = [f"vault:1:vault-{index:05d}" for index in range(vaults)]
    catalog = vault_ids[:15]
    # Visible peers the first catalog vault is similar to.
    peers = vault_ids[15:25]
    # Each catalog vault allocates to three markets; those and their
    # dependencies are visible, as discovery marks them.
    catalog_markets = [market_ids[index * 37 % markets] for index in range(45)]

    def market_dependencies(index: int) -> tuple[str, str, str, str]:
        # asset-000 is the stablecoin hub every fourth market lends.
        loan = assets[0] if index % 4 == 0 else assets[index % len(assets)]
        return loan, assets[(index * 7 + 1) % len(assets)], oracles[index % len(oracles)], irms[index % len(irms)]

    visible = {chain, protocol, *catalog, *peers, *catalog_markets}
    for index, market in enumerate(market_ids):
        if market in catalog_markets:
            visible.update(market_dependencies(index))

    def made(entity_id: str) -> RadarEntity:
        return _entity(entity_id, catalog=entity_id in catalog, visible=entity_id in visible)

    all_ids = [chain, protocol, *assets, *oracles, *irms, *market_ids, *vault_ids]
    relationships = [_relationship(protocol, chain, "deployed_on")]
    for index, market in enumerate(market_ids):
        loan, collateral, oracle, irm = market_dependencies(index)
        relationships += [
            _relationship(market, loan, "lends"),
            _relationship(market, collateral, "uses_collateral"),
            _relationship(market, oracle, "uses_oracle"),
            _relationship(market, irm, "uses_irm"),
            _relationship(market, protocol, "deployed_on"),
        ]
    for index, vault in enumerate(vault_ids):
        allocations = (
            catalog_markets[index * 3 : index * 3 + 3]
            if vault in catalog
            else [market_ids[index % markets]]
        )
        relationships += [
            _relationship(vault, market, "allocated_to", Decimal("0.3"))
            for market in allocations
        ]

    with context.database.transaction():
        context.topology.upsert_entities(map(made, all_ids), seen_at=now)
        context.topology.upsert_relationships(relationships, seen_at=now)
        context.topology.replace_similarities(
            catalog[0],
            tuple(_similarity(vault, catalog[0], now) for vault in vault_ids[15:200]),
        )
        for entity_id in all_ids:
            context.states.set_health(_warning(entity_id, now))
        sources = all_ids[:incidents]
        for index, source in enumerate(sources):
            opened = _incident(
                f"incident-{index:05d}",
                source,
                IncidentSeverity.CRITICAL if index % 1_000 == 0 else IncidentSeverity.WARNING,
                now + timedelta(seconds=index),
            )
            context.incidents.upsert_active(opened)
            reach = HUB_INCIDENT_REACH if source == assets[0] else 2
            context.incidents.replace_impacts(
                opened.incident_id,
                tuple(
                    IncidentImpact(
                        incident_id=opened.incident_id,
                        target_entity_id=target,
                        path=(source, target),
                        distance=1,
                        confidence=Decimal("0.7"),
                        estimated_affected_amount=None,
                        reasons=("synthetic",),
                        revealed=target.startswith("vault:"),
                    )
                    for target in _impact_targets(index, source, reach, vault_ids)
                ),
            )
            if reach == HUB_INCIDENT_REACH:
                context.incidents.add_notification(
                    Notification(
                        notification_id=f"notification-{opened.incident_id}",
                        incident_id=opened.incident_id,
                        severity=opened.severity,
                        title=HUB_INCIDENT_TITLE,
                        message=f"Propagated to {reach:,} vaults.",
                        created_at=opened.first_observed_at,
                    )
                )
    return {
        "catalog": tuple(catalog),
        "visible": tuple(sorted(visible)),
        "peers": tuple(peers),
    }


def _entity(entity_id: str, *, catalog: bool, visible: bool) -> RadarEntity:
    entity_type, chain_id, key = entity_id.split(":", 2)
    return RadarEntity(
        entity_id=entity_id,
        entity_type=RadarEntityType(entity_type),
        chain_id=int(chain_id),
        address_or_key=key,
        # "vault-00042" -> "Vault 00042"
        label=key.replace("-", " ").capitalize(),
        source=SYNTHETIC_SOURCE,
        catalog=catalog,
        visible=visible,
    )


def _relationship(
    source: str, target: str, relationship_type: str, weight: Decimal | None = None
) -> RadarRelationship:
    return RadarRelationship(
        source_entity_id=source,
        target_entity_id=target,
        relationship_type=relationship_type,
        weight=weight,
    )


def _similarity(candidate: str, catalog_vault: str, now: datetime) -> SimilarityRecord:
    return SimilarityRecord(
        catalog_entity_id=catalog_vault,
        candidate_entity_id=candidate,
        score=Decimal("0.5"),
        components={"shared_markets": Decimal("0.5")},
        stored=True,
        normally_visible=False,
        computed_at=now,
    )


def _warning(entity_id: str, now: datetime) -> HealthState:
    return HealthState(
        entity_id=entity_id,
        status=HealthStatus.WARNING,
        score=Decimal("0.9"),
        reasons=("synthetic warning",),
        missing_fields=(),
        evaluated_at=now,
        rule_version="synthetic-v1",
    )


def _incident(
    incident_id: str, source: str, severity: IncidentSeverity, observed_at: datetime
) -> Incident:
    return Incident(
        incident_id=incident_id,
        source_entity_id=source,
        severity=severity,
        status=IncidentStatus.OPEN,
        trigger_metrics={"deviation": Decimal("0.08")},
        rule_version="synthetic-v1",
        first_observed_at=observed_at,
        last_observed_at=observed_at,
        reasons=("synthetic incident",),
    )


def _impact_targets(index: int, source: str, reach: int, vault_ids: list[str]) -> list[str]:
    start = index * 13 % len(vault_ids)
    return [
        vault
        for vault in (vault_ids[(start + offset) % len(vault_ids)] for offset in range(reach))
        if vault != source
    ]
