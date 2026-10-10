from datetime import UTC, datetime
from decimal import Decimal

from app.contracts.models import FreshnessStatus
from app.radar.analysis.health import HealthEvaluator, HealthHistory
from app.radar.contracts import HealthState, HealthStatus, RadarEntity, StateSnapshot


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def entity(entity_type: str = "market") -> RadarEntity:
    return RadarEntity(
        entity_id=f"{entity_type}:1:test",
        entity_type=entity_type,
        chain_id=1,
        address_or_key="test",
        label="Test entity",
        source="fixture",
        metadata={},
    )


def snapshot(entity_type: str = "market", **metrics: str | bool | None) -> StateSnapshot:
    return StateSnapshot(
        snapshot_id=f"snapshot:{entity_type}:1:test",
        entity_id=f"{entity_type}:1:test",
        state_type=entity_type,
        observed_at=NOW,
        retrieved_at=NOW,
        source="fixture",
        source_identity=f"{entity_type}:1:test",
        freshness=FreshnessStatus.FRESH,
        metrics=metrics,
    )


def previous(status: HealthStatus) -> HealthState:
    return HealthState(
        entity_id="market:1:test",
        status=status,
        score=Decimal("0.5"),
        reasons=("previous",),
        missing_fields=(),
        evaluated_at=NOW,
        rule_version="radar-health-v1",
    )


def healthy_market() -> StateSnapshot:
    return snapshot(
        utilization="0.70",
        available_liquidity="1000000",
        lender_hhi="0.10",
        borrower_hhi="0.12",
        near_liquidation_debt_ratio="0.02",
        bad_debt="0",
    )


def test_warning_requires_two_consecutive_abnormal_cycles():
    evaluator = HealthEvaluator()
    stressed = healthy_market().model_copy(update={"metrics": {**healthy_market().metrics, "utilization": "0.93"}})

    first = evaluator.evaluate(entity(), stressed, (), HealthHistory())
    second = evaluator.evaluate(
        entity(),
        stressed,
        (),
        HealthHistory(current=first, consecutive_abnormal=1),
    )

    assert first.status is HealthStatus.HEALTHY
    assert "warning pending (1/2)" in first.reasons
    assert second.status is HealthStatus.WARNING


def test_critical_is_immediate_and_recovery_requires_three_normal_cycles():
    evaluator = HealthEvaluator()
    critical_snapshot = healthy_market().model_copy(
        update={"metrics": {**healthy_market().metrics, "bad_debt": "1"}}
    )
    critical = evaluator.evaluate(entity(), critical_snapshot, (), HealthHistory())
    assert critical.status is HealthStatus.CRITICAL

    first = evaluator.evaluate(
        entity(), healthy_market(), (), HealthHistory(current=critical, consecutive_normal=0)
    )
    second = evaluator.evaluate(
        entity(), healthy_market(), (), HealthHistory(current=first, consecutive_normal=1)
    )
    third = evaluator.evaluate(
        entity(), healthy_market(), (), HealthHistory(current=second, consecutive_normal=2)
    )

    assert first.status is HealthStatus.CRITICAL
    assert second.status is HealthStatus.CRITICAL
    assert third.status is HealthStatus.HEALTHY


def test_missing_market_borrower_or_lender_metrics_is_unknown():
    evaluator = HealthEvaluator()
    incomplete = snapshot(
        utilization="0.70",
        available_liquidity="1000000",
        lender_hhi="0.10",
        bad_debt="0",
    )

    result = evaluator.evaluate(entity(), incomplete, (), HealthHistory())

    assert result.status is HealthStatus.UNKNOWN
    assert "borrower_hhi" in result.missing_fields
    assert "near_liquidation_debt_ratio" in result.missing_fields


def test_material_unknown_vault_allocation_prevents_healthy_status():
    evaluator = HealthEvaluator()
    vault = entity("vault")
    vault_snapshot = snapshot(
        "vault",
        withdrawal_liquidity="500000",
        allocation_hhi="0.25",
        unmodelled_allocation_ratio="0.08",
    )

    result = evaluator.evaluate(vault, vault_snapshot, (), HealthHistory())

    assert result.status is HealthStatus.UNKNOWN
    assert "material unmodelled allocation" in result.reasons


def test_expired_snapshot_is_unknown_even_when_metrics_look_healthy():
    evaluator = HealthEvaluator()
    expired = healthy_market().model_copy(update={"freshness": FreshnessStatus.EXPIRED})

    result = evaluator.evaluate(entity(), expired, (), HealthHistory())

    assert result.status is HealthStatus.UNKNOWN
    assert "snapshot is expired" in result.reasons


def stressed_market() -> StateSnapshot:
    return healthy_market().model_copy(
        update={"metrics": {**healthy_market().metrics, "utilization": "0.93"}}
    )


def dependency(entity_id: str, status: HealthStatus) -> HealthState:
    return previous(status).model_copy(update={"entity_id": entity_id})


def test_raw_warning_after_critical_or_unknown_is_not_reported_healthy():
    evaluator = HealthEvaluator()
    for prior in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
        result = evaluator.evaluate(
            entity(), stressed_market(), (), HealthHistory(current=previous(prior))
        )
        assert result.status is HealthStatus.WARNING


def test_generic_entity_without_measurements_is_unknown():
    evaluator = HealthEvaluator()
    for metrics in ({}, {"oracle_price": None}):
        result = evaluator.evaluate(
            entity("oracle"), snapshot("oracle", **metrics), (), HealthHistory()
        )
        assert result.status is HealthStatus.UNKNOWN


def test_generic_entity_with_fresh_measurements_is_healthy():
    result = HealthEvaluator().evaluate(
        entity("oracle"), snapshot("oracle", oracle_price="1.00"), (), HealthHistory()
    )

    assert result.status is HealthStatus.HEALTHY


def test_non_positive_oracle_price_is_critical():
    result = HealthEvaluator().evaluate(
        entity("oracle"), snapshot("oracle", oracle_price="0"), (), HealthHistory()
    )

    assert result.status is HealthStatus.CRITICAL


def test_critical_wins_over_unknown():
    evaluator = HealthEvaluator()
    incomplete_with_bad_debt = snapshot(utilization="0.70", bad_debt="5")
    result = evaluator.evaluate(entity(), incomplete_with_bad_debt, (), HealthHistory())
    assert result.status is HealthStatus.CRITICAL

    expired = healthy_market().model_copy(update={"freshness": FreshnessStatus.EXPIRED})
    deps = (
        dependency("oracle:1:a", HealthStatus.UNKNOWN),
        dependency("oracle:1:b", HealthStatus.CRITICAL),
    )
    result = evaluator.evaluate(entity(), expired, deps, HealthHistory())
    assert result.status is HealthStatus.CRITICAL


def test_stale_usable_data_cannot_be_healthy():
    stale = healthy_market().model_copy(update={"freshness": FreshnessStatus.STALE_USABLE})

    result = HealthEvaluator().evaluate(entity(), stale, (), HealthHistory())

    assert result.status is HealthStatus.UNKNOWN


def test_non_finite_metric_is_treated_as_missing():
    nan_market = healthy_market().model_copy(
        update={"metrics": {**healthy_market().metrics, "utilization": "NaN"}}
    )

    result = HealthEvaluator().evaluate(entity(), nan_market, (), HealthHistory())

    assert result.status is HealthStatus.UNKNOWN
    assert "utilization" in result.missing_fields


def healthy_vault() -> StateSnapshot:
    return snapshot(
        "vault",
        withdrawal_liquidity="500000",
        allocation_hhi="0.10",
        unmodelled_allocation_ratio="0",
    )


def test_vault_health_is_allocation_weighted_when_allocations_are_known():
    evaluator = HealthEvaluator()
    vault = entity("vault")
    minor_critical = (
        dependency("market:1:big", HealthStatus.HEALTHY),
        dependency("market:1:small", HealthStatus.CRITICAL),
    )
    allocations = {"market:1:big": Decimal("0.98"), "market:1:small": Decimal("0.02")}

    weighted = evaluator.evaluate(
        vault,
        healthy_vault(),
        minor_critical,
        HealthHistory(consecutive_abnormal=1),
        allocations=allocations,
    )
    material = evaluator.evaluate(
        vault,
        healthy_vault(),
        minor_critical,
        HealthHistory(),
        allocations={"market:1:big": Decimal("0.60"), "market:1:small": Decimal("0.40")},
    )
    worst_of = evaluator.evaluate(vault, healthy_vault(), minor_critical, HealthHistory())

    assert weighted.status is HealthStatus.WARNING
    assert material.status is HealthStatus.CRITICAL
    assert worst_of.status is HealthStatus.CRITICAL


def test_vault_with_material_unknown_market_allocation_is_unknown():
    result = HealthEvaluator().evaluate(
        entity("vault"),
        healthy_vault(),
        (dependency("market:1:dark", HealthStatus.UNKNOWN),),
        HealthHistory(),
        allocations={"market:1:dark": Decimal("0.30")},
    )

    assert result.status is HealthStatus.UNKNOWN
