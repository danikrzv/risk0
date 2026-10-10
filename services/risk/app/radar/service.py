from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.radar.analysis import (
    ExposureImpactEstimator,
    HealthEvaluator,
    HealthHistory,
    IncidentDetector,
    PropagationEngine,
    VaultSimilarity,
)
from app.radar.contracts import (
    HealthState,
    HealthStatus,
    Notification,
    RadarEntity,
    RadarEntityType,
    RadarRelationship,
    RadarRelationshipType,
    SchedulerRun,
    StateSnapshot,
)
from app.radar.discovery.morpho import DiscoveryBatch
from app.radar.domain.observations import VaultProfile
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    StateRepository,
    TopologyRepository,
)


class Discovery(Protocol):
    def discover(self, now: datetime) -> DiscoveryBatch: ...


class MorphoCollector(Protocol):
    def collect(self, batch: DiscoveryBatch, now: datetime) -> tuple[StateSnapshot, ...]: ...


class ChainCollector(Protocol):
    def collect_all(
        self, chain_ids: Iterable[int], now: datetime
    ) -> tuple[StateSnapshot, ...]: ...


class EventPublisher(Protocol):
    def publish(
        self, event_type: str, resource_id: str, summary: Mapping[str, object]
    ) -> object: ...


class RadarService:
    """Coordinates deterministic slow discovery and fast monitoring cycles."""

    def __init__(
        self,
        *,
        discovery: Discovery | None,
        morpho_collector: MorphoCollector | None,
        chain_collector: ChainCollector | None,
        topology: TopologyRepository,
        states: StateRepository,
        incidents: IncidentRepository,
        exposures: ExposureRepository,
        health: HealthEvaluator | None = None,
        incident_detector: IncidentDetector | None = None,
        propagation: PropagationEngine | None = None,
        exposure_impact: ExposureImpactEstimator | None = None,
        similarity: VaultSimilarity | None = None,
        events: EventPublisher | None = None,
    ) -> None:
        self._discovery = discovery
        self._morpho_collector = morpho_collector
        self._chain_collector = chain_collector
        self._topology = topology
        self._states = states
        self._incidents = incidents
        self._exposures = exposures
        self._health = health or HealthEvaluator()
        self._incident_detector = incident_detector or IncidentDetector()
        self._propagation = propagation or PropagationEngine()
        self._exposure_impact = exposure_impact or ExposureImpactEstimator()
        self._similarity = similarity or VaultSimilarity()
        self._events = events
        self._batch: DiscoveryBatch | None = None
        self._health_history: dict[str, HealthHistory] = {}
        # Propagation inputs are incidents (changed only by health updates) and
        # topology/similarities (changed only by discovery). Exposures created
        # through the API are propagated at creation. Until either input changes,
        # re-propagating every fast cycle would only rewrite identical impacts.
        self._propagation_stale = True

    def run_discovery(self, now: datetime) -> SchedulerRun:
        if self._discovery is None:
            return self._record_run(
                "slow", now, "failed", 0, 1, "Radar discovery is not configured."
            )
        try:
            batch = self._discovery.discover(now)
            entity_count = self._topology.upsert_entities(
                batch.entities, seen_at=batch.discovered_at
            )
            relationship_count = self._topology.upsert_relationships(
                batch.relationships, seen_at=batch.discovered_at
            )
            self._topology.tombstone_missing(
                seen_entity_ids=(entity.entity_id for entity in batch.entities),
                chain_ids=batch.complete_chain_ids,
                at=batch.discovered_at,
            )
            self._refresh_similarities(
                batch.entities, batch.relationships, batch.discovered_at
            )
            self._batch = batch
            self._propagation_stale = True
        except Exception as error:
            return self._record_run(
                "slow",
                now,
                "failed",
                0,
                1,
                f"Discovery failed: {type(error).__name__}",
            )
        errors = len(batch.failures)
        status = "partial" if errors else "completed"
        self._publish(
            "graph.updated",
            "graph",
            {
                "entity_count": entity_count,
                "relationship_count": relationship_count,
                "status": status,
            },
        )
        return self._record_run(
            "slow",
            now,
            status,
            entity_count + relationship_count,
            errors,
            f"Stored {entity_count} entities and {relationship_count} relationships.",
        )

    def run_monitoring(self, now: datetime) -> SchedulerRun:
        if self._batch is None:
            return self._record_run(
                "fast", now, "partial", 0, 1, "Discovery must run before monitoring."
            )

        snapshots: list[StateSnapshot] = []
        errors = 0
        if self._morpho_collector is None:
            errors += 1
        else:
            try:
                snapshots.extend(self._morpho_collector.collect(self._batch, now))
            except Exception:
                errors += 1
        if self._chain_collector is None:
            errors += 1
        else:
            try:
                chain_ids = tuple(
                    sorted(
                        {entity.chain_id for entity in self._batch.entities}
                        | set(self._batch.complete_chain_ids)
                    )
                )
                snapshots.extend(
                    self._normalize_chain_snapshots(
                        self._chain_collector.collect_all(chain_ids, now)
                    )
                )
            except Exception:
                errors += 1

        new_snapshots = tuple(
            snapshot
            for snapshot in sorted(snapshots, key=lambda item: item.entity_id)
            if self._states.append_snapshot(snapshot)
        )
        health_states, health_errors = self._evaluate_health(new_snapshots)
        errors += health_errors
        errors += self._reconcile_incidents(health_states, now)
        status = "partial" if errors else "completed"
        if new_snapshots:
            self._publish(
                "graph.updated",
                "graph",
                {"processed_count": len(new_snapshots), "status": status},
            )
        return self._record_run(
            "fast",
            now,
            status,
            len(new_snapshots),
            errors,
            f"Processed {len(new_snapshots)} new state snapshots.",
        )

    def _evaluate_health(
        self, snapshots: Sequence[StateSnapshot]
    ) -> tuple[tuple[HealthState, ...], int]:
        entities = {entity.entity_id: entity for entity in self._topology.entities()}
        relationships = self._topology.relationships()
        dependency_ids: dict[str, list[str]] = {}
        allocations: dict[str, dict[str, Decimal]] = {}
        for relationship in relationships:
            dependency_ids.setdefault(relationship.source_entity_id, []).append(
                relationship.target_entity_id
            )
            if (
                relationship.relationship_type is RadarRelationshipType.ALLOCATED_TO
                and relationship.weight is not None
            ):
                allocations.setdefault(relationship.source_entity_id, {})[
                    relationship.target_entity_id
                ] = relationship.weight
        latest = self._states.latest_health_all()
        evaluated: list[HealthState] = []
        errors = 0
        for snapshot in sorted(snapshots, key=_snapshot_health_order):
            entity = entities.get(snapshot.entity_id)
            if entity is None:
                continue
            dependencies = tuple(
                latest[entity_id]
                for entity_id in dependency_ids.get(entity.entity_id, ())
                if entity_id in latest
            )
            history = self._health_history.get(
                entity.entity_id, HealthHistory(current=latest.get(entity.entity_id))
            )
            try:
                if entity.entity_type is RadarEntityType.VAULT and entity.entity_id in allocations:
                    # Vault health is weighted by allocation share when weights are known.
                    state = self._health.evaluate(
                        entity,
                        snapshot,
                        dependencies,
                        history,
                        allocations=allocations[entity.entity_id],
                    )
                else:
                    state = self._health.evaluate(entity, snapshot, dependencies, history)
            except Exception:
                # Isolate a bad entity: the rest of the cycle still evaluates.
                errors += 1
                continue
            self._states.set_health(state)
            latest[state.entity_id] = state
            evaluated.append(state)
            self._health_history[state.entity_id] = _next_history(history, state)
        return tuple(evaluated), errors

    def _reconcile_incidents(
        self, health_states: tuple[HealthState, ...], now: datetime
    ) -> int:
        if not health_states and not self._propagation_stale:
            return 0
        # Cleared before reading topology so a concurrent discovery that lands
        # mid-propagation marks the next cycle stale again.
        self._propagation_stale = False
        if health_states:
            changes = self._incident_detector.reconcile(
                self._incidents.active(),
                health_states,
                now,
                # Incidents whose source left the topology are resolved, not orphaned.
                monitored_entity_ids={
                    entity.entity_id for entity in self._topology.entities()
                },
            )
            for incident in (*changes.opened, *changes.updated):
                result = self._incidents.upsert_active(incident)
                self._publish(
                    "incident.opened" if result.created else "incident.updated",
                    result.incident.incident_id,
                    {
                        "source_entity_id": result.incident.source_entity_id,
                        "severity": result.incident.severity.value,
                    },
                )
                if result.created:
                    notification = Notification(
                        notification_id=f"notification:{result.incident.incident_id}",
                        incident_id=result.incident.incident_id,
                        severity=result.incident.severity,
                        title=f"{result.incident.severity.value}: radar incident",
                        message=(
                            f"{result.incident.source_entity_id}: "
                            f"{'; '.join(result.incident.reasons)}"
                        ),
                        created_at=now,
                    )
                    if self._incidents.add_notification(notification):
                        self._publish(
                            "notification.created",
                            notification.notification_id,
                            {
                                "incident_id": notification.incident_id,
                                "severity": notification.severity.value,
                            },
                        )
            for incident in changes.resolved:
                self._incidents.resolve(incident.incident_id, now)
                self._publish(
                    "incident.resolved",
                    incident.incident_id,
                    {"source_entity_id": incident.source_entity_id},
                )

        entities = self._topology.entities()
        relationships = self._topology.relationships()
        similarities = self._topology.similarities()
        errors = 0
        propagated_by_incident = {}
        for incident in self._incidents.active():
            try:
                impacts = self._propagation.propagate(
                    incident, entities, relationships, similarities
                )
            except Exception:
                errors += 1
                continue
            self._incidents.replace_impacts(incident.incident_id, impacts)
            propagated_by_incident[incident.incident_id] = (incident, impacts)
        for exposure in self._exposures.list_exposures():
            impacts = []
            for incident, propagated in propagated_by_incident.values():
                try:
                    result = self._exposure_impact.estimate(
                        exposure, incident, propagated, relationships
                    )
                except Exception:
                    errors += 1
                    continue
                impacts.extend(result.impacts)
            self._exposures.replace_impacts(exposure.exposure_id, impacts)
        if errors:
            self._propagation_stale = True
        return errors

    def _refresh_similarities(
        self,
        entities: Sequence[RadarEntity],
        relationships: Sequence[RadarRelationship],
        computed_at: datetime,
    ) -> None:
        profiles = _vault_profiles(entities, relationships)
        for catalog in sorted(
            (entity for entity in entities if entity.catalog and entity.entity_id in profiles),
            key=lambda entity: entity.entity_id,
        ):
            scores = tuple(
                self._similarity.score(
                    profiles[catalog.entity_id], profile
                ).model_copy(update={"computed_at": computed_at})
                for entity_id, profile in sorted(profiles.items())
                if entity_id != catalog.entity_id
            )
            selected = self._similarity.select_neighbors(catalog.entity_id, scores)
            self._topology.replace_similarities(catalog.entity_id, selected.stored)

    def _normalize_chain_snapshots(
        self, snapshots: Sequence[StateSnapshot]
    ) -> tuple[StateSnapshot, ...]:
        assert self._batch is not None
        entity_ids = {entity.chain_id: entity.entity_id for entity in self._batch.chains}
        normalized = []
        for snapshot in snapshots:
            try:
                chain_id = int(snapshot.source_identity.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                normalized.append(snapshot)
                continue
            entity_id = entity_ids.get(chain_id)
            if entity_id is None or entity_id == snapshot.entity_id:
                normalized.append(snapshot)
                continue
            normalized.append(
                snapshot.model_copy(
                    update={
                        "entity_id": entity_id,
                        "snapshot_id": snapshot.snapshot_id.replace(
                            snapshot.entity_id, entity_id, 1
                        ),
                    }
                )
            )
        return tuple(normalized)

    def _record_run(
        self,
        loop: str,
        now: datetime,
        status: str,
        processed_count: int,
        error_count: int,
        message: str,
    ) -> SchedulerRun:
        run = SchedulerRun(
            run_id=f"radar:{loop}:{now.isoformat()}",
            loop=loop,
            status=status,
            started_at=now,
            finished_at=now,
            processed_count=processed_count,
            error_count=error_count,
            message=message,
        )
        return run

    def _publish(
        self, event_type: str, resource_id: str, summary: Mapping[str, object]
    ) -> None:
        if self._events is not None:
            self._events.publish(event_type, resource_id, summary)


def _snapshot_health_order(snapshot: StateSnapshot) -> tuple[int, str]:
    order = {
        RadarEntityType.CHAIN: 0,
        RadarEntityType.ASSET: 1,
        RadarEntityType.ORACLE: 2,
        RadarEntityType.IRM: 3,
        RadarEntityType.PROTOCOL: 4,
        RadarEntityType.MARKET: 5,
        RadarEntityType.VAULT: 6,
        RadarEntityType.POSITION: 7,
    }
    return order[snapshot.state_type], snapshot.entity_id


def _next_history(previous: HealthHistory, state: HealthState) -> HealthHistory:
    pending_recovery = any("recovery pending" in reason for reason in state.reasons)
    if pending_recovery:
        return HealthHistory(
            current=state,
            consecutive_normal=previous.consecutive_normal + 1,
        )
    pending_warning = any("warning pending" in reason for reason in state.reasons)
    if state.status is HealthStatus.WARNING or pending_warning:
        return HealthHistory(
            current=state,
            consecutive_abnormal=previous.consecutive_abnormal + 1,
        )
    if state.status is HealthStatus.HEALTHY:
        recovering = previous.current is not None and previous.current.status is not HealthStatus.HEALTHY
        return HealthHistory(
            current=state,
            consecutive_normal=previous.consecutive_normal + 1 if recovering else 0,
        )
    return HealthHistory(current=state)


def _vault_profiles(
    entities: Sequence[RadarEntity], relationships: Sequence[RadarRelationship]
) -> dict[str, VaultProfile]:
    by_id = {entity.entity_id: entity for entity in entities}
    outgoing: dict[str, list[RadarRelationship]] = {}
    for relationship in relationships:
        outgoing.setdefault(relationship.source_entity_id, []).append(relationship)
    profiles: dict[str, VaultProfile] = {}
    for vault in entities:
        if vault.entity_type is not RadarEntityType.VAULT:
            continue
        allocations: dict[str, Decimal] = {}
        loan_assets: set[str] = set()
        collateral_assets: set[str] = set()
        oracle_groups: set[str] = set()
        irms: set[str] = set()
        lltvs: list[Decimal] = []
        for relationship in outgoing.get(vault.entity_id, ()):
            if relationship.relationship_type is RadarRelationshipType.LENDS:
                loan_assets.add(relationship.target_entity_id)
            if relationship.relationship_type is not RadarRelationshipType.ALLOCATED_TO:
                continue
            market = by_id.get(relationship.target_entity_id)
            if market is None or market.entity_type is not RadarEntityType.MARKET:
                continue
            allocations[market.entity_id] = relationship.weight or Decimal(0)
            _add_metadata_id(loan_assets, market, "loan_asset", RadarEntityType.ASSET)
            _add_metadata_id(
                collateral_assets, market, "collateral_asset", RadarEntityType.ASSET
            )
            _add_metadata_id(oracle_groups, market, "oracle", RadarEntityType.ORACLE)
            _add_metadata_id(irms, market, "irm", RadarEntityType.IRM)
            lltv = _decimal_metadata(market.metadata.get("lltv"))
            if lltv is not None:
                lltvs.append(lltv)
        curator = vault.metadata.get("curator")
        profiles[vault.entity_id] = VaultProfile(
            entity_id=vault.entity_id,
            market_allocations=allocations,
            loan_assets=frozenset(loan_assets),
            collateral_assets=frozenset(collateral_assets),
            oracle_groups=frozenset(oracle_groups),
            irms=frozenset(irms),
            lltvs=tuple(lltvs),
            chain_id=vault.chain_id,
            curator=str(curator) if curator else None,
        )
    return profiles


def _add_metadata_id(
    target: set[str],
    entity: RadarEntity,
    field: str,
    entity_type: RadarEntityType,
) -> None:
    value = entity.metadata.get(field)
    if value:
        target.add(f"{entity_type.value}:{entity.chain_id}:{str(value).lower()}")


def _decimal_metadata(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None
