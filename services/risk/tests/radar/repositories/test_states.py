from datetime import timedelta
from decimal import Decimal

import pytest

from app.radar.contracts import HealthStatus, RadarEntityType
from tests.radar.repositories.builders import (
    CATALOG_VAULT,
    NOW,
    entity,
    health,
    incident,
    snapshot,
)


@pytest.fixture
def states(database):
    from app.radar.repositories import StateRepository, TopologyRepository

    TopologyRepository(database).upsert_entities((entity(),), seen_at=NOW)
    return StateRepository(database)


def test_snapshot_round_trip_preserves_metric_types_and_missing_fields(states):
    original = snapshot(
        metrics={
            "utilization": Decimal("0.912345678901234567890"),
            "status_label": "0.5",
            "listed": True,
            "liquidity_usd": None,
        },
        missing_fields=("liquidity_usd",),
        freshness="stale_usable",
    )

    assert states.append_snapshot(original) is True

    assert states.latest_snapshot(CATALOG_VAULT) == original
    restored = states.latest_snapshot(CATALOG_VAULT).metrics
    assert isinstance(restored["utilization"], Decimal)
    assert isinstance(restored["status_label"], str)
    assert restored["liquidity_usd"] is None


def test_duplicate_snapshots_are_ignored(states):
    assert states.append_snapshot(snapshot()) is True
    assert states.append_snapshot(snapshot()) is False
    assert (
        states.append_snapshot(
            snapshot(snapshot_id="another-id-for-the-same-observation")
        )
        is False
    )

    assert len(states.history(CATALOG_VAULT).snapshots) == 1


def test_delayed_snapshot_is_rejected_and_does_not_regress_latest_state(states):
    current = snapshot(observed_at=NOW, metrics={"utilization": Decimal("0.95")})
    delayed = snapshot(
        observed_at=NOW - timedelta(minutes=5), metrics={"utilization": Decimal("0.10")}
    )

    assert states.append_snapshot(current) is True
    assert states.append_snapshot(delayed) is False

    assert states.latest_snapshot(CATALOG_VAULT) == current


def test_snapshots_from_independent_sources_are_tracked_separately(states):
    morpho = snapshot(observed_at=NOW, source_identity="morpho:1")
    rpc = snapshot(observed_at=NOW - timedelta(minutes=1), source_identity="rpc:1")

    assert states.append_snapshot(morpho) is True
    assert states.append_snapshot(rpc) is True

    assert states.latest_snapshot(CATALOG_VAULT) == morpho
    assert states.latest_snapshot(CATALOG_VAULT, source_identity="rpc:1") == rpc


def test_unchanged_snapshots_are_stored_only_at_the_heartbeat(states):
    heartbeat = timedelta(hours=1)
    metrics = {"utilization": Decimal("0.5"), "state_age_seconds": Decimal("10")}
    # Only the clock-derived age moves between these cycles: not a state change.
    cycles = [
        snapshot(
            observed_at=NOW + timedelta(minutes=5 * index),
            metrics={**metrics, "state_age_seconds": Decimal(10 + 300 * index)},
        )
        for index in range(12)
    ]
    changed = {"utilization": Decimal("0.6")}

    def at(minutes: int, values: dict) -> tuple:
        observed = snapshot(observed_at=NOW + timedelta(minutes=minutes), metrics=values)
        return states.append_snapshots([observed], heartbeat=heartbeat)

    stored = states.append_snapshots(cycles, heartbeat=heartbeat)
    assert [item.observed_at for item in stored] == [NOW]
    assert at(60, metrics)  # heartbeat
    assert at(61, changed)  # state change
    assert at(66, changed) == ()
    assert at(121, changed)  # heartbeat
    assert len(states.history(CATALOG_VAULT).snapshots) == 4


def test_snapshot_freshness_or_missing_fields_change_is_a_state_change(states):
    heartbeat = timedelta(hours=1)
    states.append_snapshots([snapshot(observed_at=NOW)], heartbeat=heartbeat)
    expired = snapshot(observed_at=NOW + timedelta(minutes=5), freshness="expired")
    missing = snapshot(
        observed_at=NOW + timedelta(minutes=10),
        freshness="expired",
        missing_fields=("utilization",),
    )

    assert states.append_snapshots([expired, missing], heartbeat=heartbeat) == (expired, missing)


def test_health_is_recorded_in_one_batch(states):
    first = health(evaluated_at=NOW)
    second = health(evaluated_at=NOW + timedelta(minutes=5), status=HealthStatus.WARNING)

    assert states.record_health([first, second, first]) == 2
    assert states.record_health([]) == 0

    assert states.latest_health(CATALOG_VAULT) == second


def test_completed_hours_are_aggregated_once_and_the_current_hour_is_left_open(states):
    for minutes in (0, 30, 65, 130):
        states.append_snapshot(snapshot(observed_at=NOW + timedelta(minutes=minutes)))

    # Two completed hours (12:00, 13:00); 14:00 is still running.
    assert states.aggregate_completed_hours(NOW + timedelta(minutes=135)) == 2
    hours = states.hourly(CATALOG_VAULT, start=NOW, end=NOW + timedelta(hours=3))
    assert [(item.hour_start, item.sample_count) for item in hours] == [
        (NOW, 2),
        (NOW + timedelta(hours=1), 1),
    ]

    # The next run rebuilds only the last aggregated hour onwards.
    assert states.aggregate_completed_hours(NOW + timedelta(minutes=185)) == 2
    assert len(states.hourly(CATALOG_VAULT, start=NOW, end=NOW + timedelta(hours=3))) == 3


def test_completed_hours_without_snapshots_build_nothing(states):
    assert states.aggregate_completed_hours(NOW) == 0


def test_snapshots_require_a_known_entity(states):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        states.append_snapshot(snapshot("vault:1:0xunknown"))


def test_latest_health_never_regresses_to_a_delayed_evaluation(states):
    critical = health(evaluated_at=NOW, status=HealthStatus.CRITICAL)
    delayed_healthy = health(evaluated_at=NOW - timedelta(minutes=5))

    states.set_health(critical)
    states.set_health(delayed_healthy)
    states.set_health(critical)

    assert states.latest_health(CATALOG_VAULT) == critical
    assert states.latest_health_all() == {CATALOG_VAULT: critical}
    assert len(states.history(CATALOG_VAULT).health_states) == 2


def test_duplicate_timestamp_does_not_diverge_latest_from_history(states):
    original = health(evaluated_at=NOW, status=HealthStatus.WARNING)
    conflicting_duplicate = health(evaluated_at=NOW, status=HealthStatus.CRITICAL)

    states.record_health((original, conflicting_duplicate))

    assert states.latest_health(CATALOG_VAULT) == original
    assert states.history(CATALOG_VAULT).health_states == (original,)


def test_latest_health_reads_survive_history_pruning(states, database):
    latest = health(evaluated_at=NOW, status=HealthStatus.CRITICAL)
    states.set_health(latest)

    with database.transaction() as connection:
        connection.execute("DELETE FROM health_states")

    assert states.latest_health(CATALOG_VAULT) == latest
    assert states.latest_health_all() == {CATALOG_VAULT: latest}
    assert states.latest_health_for((CATALOG_VAULT,)) == {CATALOG_VAULT: latest}


def test_health_at_returns_the_exact_historical_evaluation(states):
    first = health(evaluated_at=NOW, status=HealthStatus.WARNING)
    latest = health(evaluated_at=NOW + timedelta(minutes=5), status=HealthStatus.CRITICAL)
    states.record_health((first, latest))

    assert states.health_at(CATALOG_VAULT, NOW) == first
    assert states.health_at(CATALOG_VAULT, NOW + timedelta(minutes=1)) is None


def test_unknown_health_is_stored_explicitly(states):
    unknown = health(status=HealthStatus.UNKNOWN)

    states.set_health(unknown)

    stored = states.latest_health(CATALOG_VAULT)
    assert stored.status is HealthStatus.UNKNOWN
    assert stored.score is None
    assert stored.missing_fields == ("utilization",)


def test_history_is_paginated_newest_first(states):
    for minutes in range(5):
        states.append_snapshot(snapshot(observed_at=NOW + timedelta(minutes=5 * minutes)))
        states.set_health(health(evaluated_at=NOW + timedelta(minutes=5 * minutes)))

    first = states.history(CATALOG_VAULT, limit=2)
    second = states.history(CATALOG_VAULT, limit=2, cursor=first.next_cursor)
    last = states.history(CATALOG_VAULT, limit=2, cursor=second.next_cursor)

    assert [s.observed_at for s in first.snapshots] == [
        NOW + timedelta(minutes=20),
        NOW + timedelta(minutes=15),
    ]
    assert [h.evaluated_at for h in first.health_states] == [
        NOW + timedelta(minutes=20),
        NOW + timedelta(minutes=15),
    ]
    assert [s.observed_at for s in second.snapshots] == [
        NOW + timedelta(minutes=10),
        NOW + timedelta(minutes=5),
    ]
    assert [s.observed_at for s in last.snapshots] == [NOW]
    assert [h.evaluated_at for h in last.health_states] == [NOW]
    assert last.next_cursor is None


def test_history_rejects_an_invalid_cursor(states):
    with pytest.raises(ValueError):
        states.history(CATALOG_VAULT, cursor="not-a-cursor")


def test_hourly_aggregate_summarizes_numeric_metrics_without_inventing_values(states):
    hour = NOW
    for minutes, utilization, liquidity in (
        (0, "0.50", None),
        (5, "0.70", "100"),
        (10, "0.60", None),
    ):
        states.append_snapshot(
            snapshot(
                observed_at=hour + timedelta(minutes=minutes),
                metrics={
                    "utilization": Decimal(utilization),
                    "liquidity_usd": Decimal(liquidity) if liquidity else None,
                    "bad_debt_usd": None,
                    "label": f"cycle-{minutes}",
                },
                freshness="fresh" if minutes else "stale_usable",
            )
        )

    assert states.aggregate_hour(hour + timedelta(minutes=17)) == 1
    assert states.aggregate_hour(hour) == 1

    (aggregate,) = states.hourly(CATALOG_VAULT, start=hour, end=hour + timedelta(hours=1))
    assert aggregate.hour_start == hour
    assert aggregate.state_type is RadarEntityType.VAULT
    assert aggregate.sample_count == 3
    assert aggregate.worst_freshness == "stale_usable"
    assert aggregate.metrics["utilization"] == {
        "min": Decimal("0.50"),
        "max": Decimal("0.70"),
        "mean": Decimal("0.60"),
        "last": Decimal("0.60"),
        "count": Decimal(3),
    }
    assert aggregate.metrics["liquidity_usd"]["count"] == Decimal(1)
    assert aggregate.metrics["liquidity_usd"]["last"] is None
    assert aggregate.metrics["bad_debt_usd"] == {
        "min": None,
        "max": None,
        "mean": None,
        "last": None,
        "count": Decimal(0),
    }
    assert aggregate.metrics["label"] == {"last": "cycle-10"}


def test_prune_applies_30_day_raw_and_one_year_hourly_retention(database, states):
    from app.radar.repositories import IncidentRepository

    old = NOW - timedelta(days=31)
    assert states.aggregate_hour(NOW - timedelta(days=400)) == 0
    states.append_snapshot(snapshot(observed_at=NOW - timedelta(days=400)))
    assert states.aggregate_hour(NOW - timedelta(days=400)) == 1
    states.append_snapshot(snapshot(observed_at=old))
    states.append_snapshot(snapshot(observed_at=NOW - timedelta(days=29)))
    states.set_health(health(evaluated_at=old))
    IncidentRepository(database).upsert_active(
        incident(source_entity_id=CATALOG_VAULT, first_observed_at=old)
    )

    result = states.prune(NOW)

    remaining = states.history(CATALOG_VAULT, limit=100).snapshots
    assert [s.observed_at for s in remaining] == [NOW - timedelta(days=29)]
    assert result.removed_snapshots == 2
    assert result.removed_hourly_aggregates == 1
    # Hours are summarized before raw snapshots age out.
    assert states.hourly(CATALOG_VAULT, start=old - timedelta(hours=1), end=old + timedelta(hours=1))
    # The latest health of every entity survives retention.
    assert states.latest_health(CATALOG_VAULT) == health(evaluated_at=old)
    assert IncidentRepository(database).active(source_entity_id=CATALOG_VAULT)


def test_prune_drops_superseded_health_history_older_than_30_days(states):
    old = NOW - timedelta(days=40)
    states.set_health(health(evaluated_at=old))
    states.set_health(health(evaluated_at=NOW - timedelta(days=1)))

    result = states.prune(NOW)

    assert result.removed_health_states == 1
    assert [h.evaluated_at for h in states.history(CATALOG_VAULT).health_states] == [
        NOW - timedelta(days=1)
    ]


def test_latest_health_for_returns_each_entitys_newest_evaluation(states):
    states.set_health(health(CATALOG_VAULT, NOW - timedelta(hours=1), HealthStatus.HEALTHY))
    states.set_health(health(CATALOG_VAULT, NOW, HealthStatus.WARNING))

    latest = states.latest_health_for((CATALOG_VAULT, "vault:1:0xunknown"))

    assert list(latest) == [CATALOG_VAULT]
    assert latest[CATALOG_VAULT].status is HealthStatus.WARNING
    assert states.latest_health_for(()) == {}


def evaluated_health(evaluated_at=NOW, allocation_hhi: str = "0.30"):
    """A real evaluation of the catalog vault entity, carrying a derivation."""
    from app.contracts.models import FreshnessStatus
    from app.radar.analysis import HealthEvaluator, HealthHistory
    from app.radar.contracts import StateSnapshot

    vault = entity()
    observed = StateSnapshot(
        snapshot_id=f"derivation:{evaluated_at.isoformat()}",
        entity_id=vault.entity_id,
        state_type=vault.entity_type,
        observed_at=evaluated_at,
        retrieved_at=evaluated_at,
        source="fixture",
        source_identity="fixture",
        freshness=FreshnessStatus.FRESH,
        metrics={
            "withdrawal_liquidity": Decimal("100"),
            "allocation_hhi": Decimal(allocation_hhi),
            "unmodelled_allocation_ratio": Decimal("0"),
        },
    )
    return HealthEvaluator().evaluate(
        vault, observed, (), HealthHistory(consecutive_abnormal=1)
    )


def test_health_derivation_round_trips_through_history_and_latest(states):
    state = evaluated_health()
    assert state.derivation is not None

    assert states.set_health(state) is True

    assert states.latest_health(CATALOG_VAULT) == state
    assert states.latest_health(CATALOG_VAULT).derivation == state.derivation
    assert states.health_at(CATALOG_VAULT, NOW) == state


def test_bulk_health_reads_leave_the_derivation_out(states):
    state = evaluated_health()
    states.set_health(state)
    summary = state.model_copy(update={"derivation": None})

    assert states.latest_health_all() == {CATALOG_VAULT: summary}
    assert states.latest_health_for((CATALOG_VAULT,)) == {CATALOG_VAULT: summary}
    assert states.history(CATALOG_VAULT).health_states == (summary,)


def test_latest_derivation_follows_the_latest_recorded_state(states):
    first = evaluated_health(NOW, "0.30")
    later = evaluated_health(NOW + timedelta(minutes=5), "0.40")
    legacy = health(evaluated_at=NOW + timedelta(minutes=10))

    states.record_health((first, later))
    assert states.latest_health(CATALOG_VAULT).derivation == later.derivation

    # A state recorded without a derivation clears it rather than keeping a stale one.
    states.set_health(legacy)
    assert states.latest_health(CATALOG_VAULT) == legacy
    assert states.latest_health(CATALOG_VAULT).derivation is None


def test_unreadable_stored_derivation_is_returned_as_null(states, database):
    state = evaluated_health()
    states.set_health(state)
    with database.transaction() as connection:
        connection.execute("UPDATE latest_health_states SET derivation = '{\"checks\": 1}'")

    assert states.latest_health(CATALOG_VAULT) == state.model_copy(update={"derivation": None})
