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
