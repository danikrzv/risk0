"""The health derivation: what each evaluation records about how it decided."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.contracts.models import FreshnessStatus
from app.radar.analysis.health import HealthEvaluator, HealthHistory
from app.radar.contracts import HealthCheck, HealthState, HealthStatus
from tests.radar.analysis.test_health import (
    dependency,
    entity,
    healthy_market,
    healthy_vault,
    previous,
    snapshot,
    stressed_market,
)


SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "packages/contracts/schemas/radar.schema.json"
)


def with_metrics(**metrics: str) -> object:
    base = healthy_market()
    return base.model_copy(update={"metrics": {**base.metrics, **metrics}})


def checks(state: HealthState) -> list[HealthCheck]:
    assert state.derivation is not None
    return list(state.derivation.checks)


def decisive(state: HealthState) -> list[HealthCheck]:
    return [check for check in checks(state) if check.decisive]


def by_rule(state: HealthState, rule: str) -> HealthCheck:
    matches = [check for check in checks(state) if check.rule == rule]
    assert len(matches) == 1, [check.rule for check in checks(state)]
    return matches[0]


def test_market_critical_names_the_breached_rule_and_stops_the_cascade():
    state = HealthEvaluator().evaluate(
        entity(), with_metrics(utilization="0.99"), (), HealthHistory()
    )
    derivation = state.derivation

    assert state.status is HealthStatus.CRITICAL
    assert [(check.stage.value, check.metric) for check in derivation.checks] == [
        ("freshness", "freshness"),
        ("metrics", "bad_debt"),
        ("metrics", "utilization"),
        ("hysteresis", None),
    ]
    [rule] = decisive(state)
    assert rule.metric == "utilization"
    assert (rule.observed, rule.operator, rule.threshold) == (
        Decimal("0.99"),
        ">=",
        Decimal("0.98"),
    )
    assert rule.outcome.value == "breach"
    assert rule.leads_to is HealthStatus.CRITICAL
    assert derivation.raw_status is HealthStatus.CRITICAL
    assert derivation.hysteresis.rule == "immediate"
    assert derivation.hysteresis.held is False
    assert derivation.final_status is HealthStatus.CRITICAL
    assert derivation.score == Decimal("0")
    assert derivation.score_mapping.WARNING == Decimal("0.5")
    assert derivation.rule_version == "radar-health-v1"
    assert derivation.dependencies is None


def test_confirmed_market_warning_records_thresholds_on_both_sides():
    state = HealthEvaluator().evaluate(
        entity(), stressed_market(), (), HealthHistory(consecutive_abnormal=1)
    )

    assert state.status is HealthStatus.WARNING
    assert state.score == Decimal("0.5")
    critical = by_rule(state, "Utilization at or above the critical threshold")
    assert (critical.observed, critical.threshold, critical.outcome.value) == (
        Decimal("0.93"),
        Decimal("0.98"),
        "within",
    )
    [rule] = [check for check in decisive(state) if check.stage.value == "metrics"]
    assert (rule.metric, rule.observed, rule.operator, rule.threshold, rule.leads_to) == (
        "utilization",
        Decimal("0.93"),
        ">=",
        Decimal("0.90"),
        HealthStatus.WARNING,
    )
    # Every warning rule ran; only the breached one is decisive.
    warning_metrics = [
        check.metric
        for check in checks(state)
        if check.stage.value == "metrics" and check.leads_to is HealthStatus.WARNING
    ]
    assert warning_metrics == [
        "utilization",
        "lender_hhi",
        "borrower_hhi",
        "near_liquidation_debt_ratio",
        "available_liquidity",
    ]
    hysteresis = state.derivation.hysteresis
    assert (hysteresis.rule, hysteresis.counter, hysteresis.cycles_required) == (
        "warning_confirmation",
        2,
        2,
    )
    assert hysteresis.held is False
    assert hysteresis.final_status is HealthStatus.WARNING


def test_pending_warning_is_held_healthy_by_hysteresis():
    state = HealthEvaluator().evaluate(entity(), stressed_market(), (), HealthHistory())
    derivation = state.derivation

    assert state.status is HealthStatus.HEALTHY
    assert derivation.raw_status is HealthStatus.WARNING
    assert derivation.raw_reasons == ("utilization exceeds warning threshold",)
    assert derivation.hysteresis.held is True
    assert derivation.hysteresis.previous_status is None
    assert (derivation.hysteresis.counter, derivation.hysteresis.cycles_required) == (1, 2)
    assert derivation.final_status is HealthStatus.HEALTHY
    assert derivation.score == Decimal("1")
    hysteresis_check = checks(state)[-1]
    assert hysteresis_check.stage.value == "hysteresis"
    assert hysteresis_check.decisive is True
    assert hysteresis_check.leads_to is HealthStatus.HEALTHY
    assert (hysteresis_check.observed, hysteresis_check.threshold) == (Decimal(1), Decimal(2))


def test_healthy_market_has_no_decisive_rule_and_requires_freshness():
    state = HealthEvaluator().evaluate(entity(), healthy_market(), (), HealthHistory())

    assert state.status is HealthStatus.HEALTHY
    assert decisive(state) == []
    assert all(check.outcome.value in {"within", "info"} for check in checks(state))
    fresh = by_rule(state, "HEALTHY requires a fresh snapshot")
    assert (fresh.note, fresh.outcome.value) == ("fresh", "within")
    assert state.derivation.hysteresis.rule == "none"


def test_missing_metrics_are_unknown_and_list_the_missing_fields():
    incomplete = snapshot(
        utilization="0.70",
        available_liquidity="1000000",
        lender_hhi="0.10",
        bad_debt="0",
    )

    state = HealthEvaluator().evaluate(entity(), incomplete, (), HealthHistory())

    assert state.status is HealthStatus.UNKNOWN
    assert state.score is None
    [rule] = decisive(state)
    assert rule.rule == "Required market metrics are missing"
    assert rule.outcome.value == "missing"
    assert rule.observed == Decimal(2)
    assert rule.note == "borrower_hhi, near_liquidation_debt_ratio"
    assert rule.leads_to is HealthStatus.UNKNOWN
    near = by_rule(state, "Near-liquidation debt at or above the critical threshold")
    assert (near.observed, near.outcome.value) == (None, "missing")
    # The warning rules never ran.
    assert not any(check.leads_to is HealthStatus.WARNING for check in checks(state))
    assert state.derivation.score_mapping.UNKNOWN is None


def test_vault_weighted_dependencies_record_shares_totals_and_materiality():
    minor_critical = (
        dependency("market:1:big", HealthStatus.HEALTHY),
        dependency("market:1:small", HealthStatus.CRITICAL),
    )

    state = HealthEvaluator().evaluate(
        entity("vault"),
        healthy_vault(),
        minor_critical,
        HealthHistory(consecutive_abnormal=1),
        allocations={"market:1:big": Decimal("0.98"), "market:1:small": Decimal("0.02")},
    )
    aggregation = state.derivation.dependencies

    assert state.status is HealthStatus.WARNING
    assert [(item.entity_id, item.status, item.counted, item.weight) for item in aggregation.dependencies] == [
        ("market:1:big", HealthStatus.HEALTHY, "weighted", Decimal("0.98")),
        ("market:1:small", HealthStatus.CRITICAL, "weighted", Decimal("0.02")),
    ]
    assert aggregation.weighted_totals.CRITICAL == Decimal("0.02")
    assert aggregation.weighted_totals.HEALTHY == Decimal("0.98")
    assert aggregation.material_allocation == Decimal("0.05")
    assert aggregation.status is HealthStatus.WARNING
    assert aggregation.reason == "critical market holds a minor allocation"
    material = by_rule(state, "CRITICAL markets hold a material allocation")
    assert (material.observed, material.operator, material.threshold, material.outcome.value) == (
        Decimal("0.02"),
        ">=",
        Decimal("0.05"),
        "within",
    )
    [rule] = [check for check in decisive(state) if check.stage.value == "dependencies"]
    assert rule.rule == "CRITICAL markets hold a minor allocation"
    assert (rule.observed, rule.operator, rule.threshold) == (Decimal("0.02"), ">", Decimal(0))
    # Rules over dependencies counted in full cannot fire, so none is listed.
    assert not any(
        check.metric and check.metric.endswith("_dependencies") for check in checks(state)
    )


def test_material_critical_market_makes_the_vault_critical_before_its_metrics():
    state = HealthEvaluator().evaluate(
        entity("vault"),
        healthy_vault(),
        (dependency("market:1:big", HealthStatus.CRITICAL),),
        HealthHistory(),
        allocations={"market:1:big": Decimal("0.40")},
    )

    assert state.status is HealthStatus.CRITICAL
    [rule] = decisive(state)
    assert rule.stage.value == "dependencies"
    assert (rule.observed, rule.threshold) == (Decimal("0.40"), Decimal("0.05"))
    assert {check.stage.value for check in checks(state)} == {"dependencies", "hysteresis"}


def test_unweighted_dependencies_count_in_full():
    state = HealthEvaluator().evaluate(
        entity(),
        healthy_market(),
        (
            dependency("oracle:1:a", HealthStatus.HEALTHY),
            dependency("oracle:1:b", HealthStatus.WARNING),
        ),
        HealthHistory(consecutive_abnormal=1),
    )
    aggregation = state.derivation.dependencies

    assert state.status is HealthStatus.WARNING
    assert {item.counted for item in aggregation.dependencies} == {"full"}
    assert all(item.weight is None for item in aggregation.dependencies)
    [rule] = decisive(state)
    assert (rule.metric, rule.observed, rule.leads_to) == (
        "warning_dependencies",
        Decimal(1),
        HealthStatus.WARNING,
    )
    assert aggregation.reason == "warning dependency"


def test_generic_oracle_records_its_price_rule():
    healthy = HealthEvaluator().evaluate(
        entity("oracle"), snapshot("oracle", oracle_price="1.00"), (), HealthHistory()
    )
    critical = HealthEvaluator().evaluate(
        entity("oracle"), snapshot("oracle", oracle_price="0"), (), HealthHistory()
    )
    explicit = HealthEvaluator().evaluate(
        entity("asset"), snapshot("asset", status="WARNING"), (), HealthHistory()
    )
    empty = HealthEvaluator().evaluate(
        entity("chain"), snapshot("chain"), (), HealthHistory()
    )

    price = by_rule(healthy, "Oracle price is zero or negative")
    assert (price.observed, price.operator, price.threshold, price.outcome.value) == (
        Decimal("1.00"),
        "<=",
        Decimal(0),
        "within",
    )
    assert healthy.derivation.raw_reasons == ("required measurements are fresh",)
    [rule] = decisive(critical)
    assert (rule.metric, rule.leads_to) == ("oracle_price", HealthStatus.CRITICAL)
    [rule] = [check for check in decisive(explicit) if check.stage.value == "metrics"]
    assert (rule.metric, rule.note, rule.outcome.value) == ("status", "WARNING", "info")
    [rule] = decisive(empty)
    assert (rule.metric, rule.observed, rule.outcome.value) == ("measurements", Decimal(0), "missing")


def test_recovery_is_held_then_confirmed():
    evaluator = HealthEvaluator()

    held = evaluator.evaluate(
        entity(), healthy_market(), (), HealthHistory(current=previous(HealthStatus.CRITICAL))
    )
    confirmed = evaluator.evaluate(
        entity(),
        healthy_market(),
        (),
        HealthHistory(current=previous(HealthStatus.CRITICAL), consecutive_normal=2),
    )

    assert held.status is HealthStatus.CRITICAL
    assert held.derivation.raw_status is HealthStatus.HEALTHY
    hysteresis = held.derivation.hysteresis
    assert (hysteresis.rule, hysteresis.previous_status, hysteresis.counter, hysteresis.cycles_required) == (
        "recovery_confirmation",
        HealthStatus.CRITICAL,
        1,
        3,
    )
    assert hysteresis.held is True
    assert [check.stage.value for check in decisive(held)] == ["hysteresis"]
    assert decisive(held)[0].leads_to is HealthStatus.CRITICAL
    assert confirmed.status is HealthStatus.HEALTHY
    assert confirmed.derivation.hysteresis.held is False
    assert confirmed.derivation.hysteresis.counter == 3
    assert decisive(confirmed) == []


def test_warning_after_an_abnormal_state_applies_without_confirmation():
    state = HealthEvaluator().evaluate(
        entity(), stressed_market(), (), HealthHistory(current=previous(HealthStatus.CRITICAL))
    )

    assert state.status is HealthStatus.WARNING
    assert state.derivation.hysteresis.rule == "already_abnormal"
    assert state.derivation.hysteresis.held is False


@pytest.mark.parametrize(
    ("freshness", "rule"),
    [
        (FreshnessStatus.EXPIRED, "An expired snapshot cannot be evaluated"),
        (FreshnessStatus.STALE_USABLE, "HEALTHY requires a fresh snapshot"),
    ],
)
def test_freshness_rules_are_decisive_when_they_apply(freshness, rule):
    stale = healthy_market().model_copy(update={"freshness": freshness})

    state = HealthEvaluator().evaluate(entity(), stale, (), HealthHistory())

    assert state.status is HealthStatus.UNKNOWN
    [check] = decisive(state)
    assert (check.rule, check.note, check.outcome.value) == (rule, freshness.value, "breach")
    assert state.derivation.freshness is freshness


@pytest.mark.parametrize(
    "metrics",
    [
        {},
        {"utilization": "0.99"},
        {"bad_debt": "3"},
        {"near_liquidation_debt_ratio": "0.12", "lender_hhi": "0.30"},
        {"available_liquidity": "0"},
        {"borrower_hhi": None},
    ],
)
@pytest.mark.parametrize("prior", [None, *HealthStatus])
@pytest.mark.parametrize("abnormal", [0, 1])
def test_derivation_agrees_with_the_state_it_explains(metrics, prior, abnormal):
    state = HealthEvaluator().evaluate(
        entity(),
        with_metrics(**metrics),
        (),
        HealthHistory(
            current=None if prior is None else previous(prior),
            consecutive_abnormal=abnormal,
        ),
    )
    derivation = state.derivation

    assert derivation.final_status is state.status
    assert derivation.score == state.score
    assert derivation.rule_version == state.rule_version
    assert derivation.hysteresis.final_status is state.status
    assert derivation.hysteresis.held is (derivation.raw_status is not state.status)
    if derivation.raw_status is not HealthStatus.HEALTHY:
        assert any(
            check.decisive and check.leads_to is derivation.raw_status
            for check in derivation.checks
            if check.stage.value != "hysteresis"
        )


def test_derivation_validates_against_the_json_schema():
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(
        {"$ref": "#/$defs/HealthState", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )
    states = [
        HealthEvaluator().evaluate(entity(), stressed_market(), (), HealthHistory()),
        HealthEvaluator().evaluate(
            entity("vault"),
            healthy_vault(),
            (dependency("market:1:a", HealthStatus.WARNING),),
            HealthHistory(),
            allocations={"market:1:a": Decimal("0.5")},
        ),
        HealthEvaluator().evaluate(entity("oracle"), snapshot("oracle"), (), HealthHistory()),
    ]

    for state in states:
        payload = state.model_dump(mode="json")
        validator.validate(payload)
        assert HealthState.model_validate(payload) == state
    legacy = states[0].model_copy(update={"derivation": None}).model_dump(mode="json")
    validator.validate(legacy)
    del legacy["derivation"]
    validator.validate(legacy)


def test_an_invalid_derivation_never_fails_the_evaluation(monkeypatch):
    import app.radar.analysis.health as health

    def broken(**_):
        raise ValueError("broken")

    monkeypatch.setattr(health, "HealthCheck", broken)

    state = HealthEvaluator().evaluate(entity(), stressed_market(), (), HealthHistory())

    assert state.status is HealthStatus.HEALTHY
    assert state.derivation is None
