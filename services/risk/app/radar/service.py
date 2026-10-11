from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
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
from app.radar.analysis.incidents import is_data_gap_incident
from app.radar.contracts import (
    HealthState,
    HealthStatus,
    IncidentSeverity,
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


# Unchanged observations are persisted (snapshot and health) at most this often,
# so raw history is a change log that still has a sample in every hour.
STATE_HEARTBEAT = timedelta(hours=1)
# Per-cycle SSE budget; anything beyond collapses into one summary event.
MAX_INCIDENT_EVENTS = 20
MAX_NOTIFICATION_EVENTS = 20


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
        chain_ids: Collection[int] | None = None,
    ) -> None:
        """``chain_ids`` limits monitoring and incidents to those chains (None: all)."""
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
        self._chain_ids = None if chain_ids is None else tuple(sorted(set(chain_ids)))
        self._batch: DiscoveryBatch | None = None
        self._health_history: dict[str, HealthHistory] = {}
        # Latest observation evaluated per (entity, source): a re-read of the same
        # observation must not advance the health hysteresis counters again.
        self._observed_at: dict[tuple[str, str], datetime] = {}
        # Entities whose evaluation (with its derivation) was persisted since this
        # process started. Rows written before derivations existed have none, and
        # change detection ignores derivations, so each entity is written once
        # after start; the change-only rule applies from then on.
        self._derivation_recorded: set[str] = set()
        # Propagation inputs are incidents (changed only by health updates) and
        # topology/similarities (changed only by discovery). Exposures created
        # through the API are propagated at creation. Until either input changes,
        # re-propagating every fast cycle would only rewrite identical impacts.
        self._propagation_stale = True

    def run_discovery(self, now: datetime) -> SchedulerRun:
        run = self._discover(now)
        # Hourly retention rides on the slow loop, whatever discovery's outcome.
        try:
            self._states.aggregate_completed_hours(now)
            self._states.prune(now)
        except Exception as error:
            return run.model_copy(
                update={
                    "status": "failed" if run.status == "failed" else "partial",
                    "error_count": run.error_count + 1,
                    "message": f"{run.message} Retention failed: {type(error).__name__}",
                }
            )
        return run

    def _discover(self, now: datetime) -> SchedulerRun:
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
                chain_ids = {entity.chain_id for entity in self._batch.entities} | set(
                    self._batch.complete_chain_ids
                )
                if self._chain_ids is not None:
                    chain_ids &= set(self._chain_ids)
                snapshots.extend(
                    self._normalize_chain_snapshots(
                        self._chain_collector.collect_all(tuple(sorted(chain_ids)), now)
                    )
                )
            except Exception:
                errors += 1

        monitored = self._monitored_entities()
        observations = tuple(
            snapshot
            for snapshot in sorted(snapshots, key=lambda item: item.entity_id)
            if snapshot.entity_id in monitored and self._is_new_observation(snapshot)
        )
        stored = self._states.append_snapshots(observations, heartbeat=STATE_HEARTBEAT)
        health_states, health_changes, health_errors = self._evaluate_health(
            observations, monitored
        )
        errors += health_errors
        events = _CycleEvents()
        incidents_changed, incident_errors = self._reconcile_incidents(
            health_states, now, monitored, events
        )
        errors += incident_errors
        status = "partial" if errors else "completed"
        # One graph update per cycle, and only when something visible changed.
        if stored or health_changes or incidents_changed:
            events.graph = {"processed_count": len(observations), "status": status}
        self._flush(events)
        return self._record_run(
            "fast",
            now,
            status,
            len(observations),
            errors,
            f"Processed {len(observations)} new observations; stored "
            f"{len(stored)} changed snapshots and {health_changes} health changes.",
        )

    def _monitored_entities(self) -> dict[str, RadarEntity]:
        """Live topology entities on the monitored chains."""
        if self._chain_ids is None:
            entities = self._topology.entities()
        else:
            entities = tuple(
                entity
                for chain_id in self._chain_ids
                for entity in self._topology.entities(chain_id=chain_id)
            )
        return {entity.entity_id: entity for entity in entities}

    def _is_new_observation(self, snapshot: StateSnapshot) -> bool:
        key = (snapshot.entity_id, snapshot.source_identity)
        previous = self._observed_at.get(key)
        if previous is not None and snapshot.observed_at <= previous:
            return False
        self._observed_at[key] = snapshot.observed_at
        return True

    def _evaluate_health(
        self, snapshots: Sequence[StateSnapshot], entities: Mapping[str, RadarEntity]
    ) -> tuple[tuple[HealthState, ...], int, int]:
        """Evaluate every new observation; persist only changed (or heartbeat) health."""
        if not snapshots:
            return (), 0, 0
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
        persisted = self._states.latest_health_all()
        latest = dict(persisted)
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
            latest[state.entity_id] = state
            evaluated.append(state)
            self._health_history[state.entity_id] = _next_history(history, state)
        to_record = [
            state
            for state in evaluated
            if _health_changed(persisted.get(state.entity_id), state)
            or (state.derivation is not None and state.entity_id not in self._derivation_recorded)
        ]
        changes = self._states.record_health(to_record)
        self._derivation_recorded.update(
            state.entity_id for state in to_record if state.derivation is not None
        )
        return tuple(evaluated), changes, errors

    def _reconcile_incidents(
        self,
        health_states: tuple[HealthState, ...],
        now: datetime,
        monitored: Mapping[str, RadarEntity],
        events: "_CycleEvents",
    ) -> tuple[bool, int]:
        """Apply incident changes; returns whether any changed materially, and errors."""
        active = self._incidents.active()
        changed = False
        if health_states or active:
            known_data_gap_ids = {
                incident.incident_id
                for incident in active
                if is_data_gap_incident(incident)
                and (
                    origin := self._states.health_at(
                        incident.source_entity_id, incident.first_observed_at
                    )
                )
                is not None
                and origin.status is HealthStatus.UNKNOWN
            }
            changes = self._incident_detector.reconcile(
                active,
                health_states,
                now,
                # Incidents whose source left the topology or the monitored
                # chains are resolved, not orphaned.
                monitored_entity_ids=set(monitored),
                known_data_gap_incident_ids=known_data_gap_ids,
            )
            prior = {incident.incident_id: incident for incident in active}
            for incident in (*changes.opened, *changes.updated):
                result = self._incidents.upsert_active(incident)
                stored = result.incident
                previous = prior.get(stored.incident_id)
                summary = {
                    "source_entity_id": stored.source_entity_id,
                    "severity": stored.severity.value,
                }
                if result.created:
                    changed = True
                    events.incident("incident.opened", stored.incident_id, summary)
                    notification = Notification(
                        notification_id=f"notification:{stored.incident_id}",
                        incident_id=stored.incident_id,
                        severity=stored.severity,
                        title=f"{stored.severity.value}: radar incident",
                        message=f"{stored.source_entity_id}: {'; '.join(stored.reasons)}",
                        created_at=now,
                    )
                    if self._incidents.add_notification(notification):
                        events.notification(
                            notification.notification_id,
                            {
                                "incident_id": notification.incident_id,
                                "severity": notification.severity.value,
                            },
                        )
                elif previous is None or (stored.severity, stored.reasons) != (
                    previous.severity,
                    previous.reasons,
                ):
                    changed = True
                    events.incident("incident.updated", stored.incident_id, summary)
                # Otherwise the same cause was observed again: only its
                # last_observed_at moved, so no event and no re-propagation.
            if changes.resolved:
                changed = True
                self._incidents.resolve_many(
                    (incident.incident_id for incident in changes.resolved), now
                )
                # Alerts raised from data gaps never reported a real deterioration.
                self._incidents.mark_incidents_read(
                    (
                        incident.incident_id
                        for incident in changes.resolved
                        if is_data_gap_incident(incident)
                    ),
                    now,
                )
                for incident in changes.resolved:
                    events.incident(
                        "incident.resolved",
                        incident.incident_id,
                        {"source_entity_id": incident.source_entity_id},
                    )

        if not changed and not self._propagation_stale:
            return changed, 0
        # Cleared before reading topology so a concurrent discovery that lands
        # mid-propagation marks the next cycle stale again.
        self._propagation_stale = False
        entities = tuple(self._monitored_entities().values())
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
        return changed, errors

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

    def _flush(self, events: "_CycleEvents") -> None:
        """Publish one cycle's events: bounded incident events, then the graph update."""
        self._publish_capped(
            sorted(events.incidents, key=_incident_event_priority),
            MAX_INCIDENT_EVENTS,
            "incident.updated",
            "incidents",
        )
        self._publish_capped(
            events.notifications,
            MAX_NOTIFICATION_EVENTS,
            "notification.created",
            "notifications",
        )
        if events.graph is not None:
            self._publish("graph.updated", "graph", events.graph)

    def _publish_capped(
        self,
        items: Sequence[tuple[str, str, Mapping[str, object]]],
        limit: int,
        summary_type: str,
        summary_id: str,
    ) -> None:
        if len(items) <= limit:
            for event_type, resource_id, summary in items:
                self._publish(event_type, resource_id, summary)
            return
        # Over budget: the first events individually, the rest as one summary
        # that clients handle like any other change of that kind (a refetch).
        for event_type, resource_id, summary in items[: limit - 1]:
            self._publish(event_type, resource_id, summary)
        rest = items[limit - 1 :]
        counts: dict[str, int] = {}
        for event_type, _, _ in rest:
            kind = event_type.rsplit(".", 1)[-1]
            counts[kind] = counts.get(kind, 0) + 1
        self._publish(
            summary_type, summary_id, {"coalesced": True, "count": len(rest), **counts}
        )

    def _publish(
        self, event_type: str, resource_id: str, summary: Mapping[str, object]
    ) -> None:
        if self._events is not None:
            self._events.publish(event_type, resource_id, summary)


class _CycleEvents:
    """Events gathered during one cycle and published together at its end."""

    def __init__(self) -> None:
        self.incidents: list[tuple[str, str, Mapping[str, object]]] = []
        self.notifications: list[tuple[str, str, Mapping[str, object]]] = []
        self.graph: Mapping[str, object] | None = None

    def incident(
        self, event_type: str, incident_id: str, summary: Mapping[str, object]
    ) -> None:
        self.incidents.append((event_type, incident_id, summary))

    def notification(self, notification_id: str, summary: Mapping[str, object]) -> None:
        self.notifications.append(("notification.created", notification_id, summary))


_INCIDENT_EVENT_ORDER = {"incident.opened": 0, "incident.updated": 1, "incident.resolved": 2}


def _incident_event_priority(
    item: tuple[str, str, Mapping[str, object]],
) -> tuple[int, int]:
    """New incidents first, then escalations, then resolutions; critical first."""
    event_type, _, summary = item
    critical = summary.get("severity") == IncidentSeverity.CRITICAL.value
    return _INCIDENT_EVENT_ORDER[event_type], 0 if critical else 1


def _health_changed(previous: HealthState | None, state: HealthState) -> bool:
    """Whether an evaluation is worth persisting next to the latest persisted one."""
    if previous is None:
        return True
    if state.evaluated_at <= previous.evaluated_at:
        return False
    if state.evaluated_at - previous.evaluated_at >= STATE_HEARTBEAT:
        return True
    return (
        state.status,
        state.score,
        state.reasons,
        tuple(sorted(state.missing_fields)),
        state.rule_version,
    ) != (
        previous.status,
        previous.score,
        previous.reasons,
        tuple(sorted(previous.missing_fields)),
        previous.rule_version,
    )


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
