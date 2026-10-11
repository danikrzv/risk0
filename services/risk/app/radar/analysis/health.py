import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Mapping

from app.contracts.models import FreshnessStatus
from app.radar.contracts import (
    HealthCheck,
    HealthCheckOutcome,
    HealthDependencyAggregation,
    HealthDependencyContribution,
    HealthDerivation,
    HealthDerivationStage,
    HealthHysteresis,
    HealthState,
    HealthStatus,
    HealthStatusTotals,
    RadarEntity,
    RadarEntityType,
    StateSnapshot,
)
from app.radar.domain.rules import RadarRuleSet


MARKET_REQUIRED = (
    "utilization",
    "available_liquidity",
    "lender_hhi",
    "borrower_hhi",
    "near_liquidation_debt_ratio",
    "bad_debt",
)
VAULT_REQUIRED = (
    "withdrawal_liquidity",
    "allocation_hhi",
    "unmodelled_allocation_ratio",
)


@dataclass(frozen=True, slots=True)
class HealthHistory:
    current: HealthState | None = None
    consecutive_abnormal: int = 0
    consecutive_normal: int = 0

    def __post_init__(self) -> None:
        if self.consecutive_abnormal < 0 or self.consecutive_normal < 0:
            raise ValueError("health counters must be non-negative")


@dataclass(frozen=True, slots=True)
class _RawHealth:
    status: HealthStatus
    reasons: tuple[str, ...]
    missing_fields: tuple[str, ...] = ()
    # Indices (into the trace) of the checks that produced this status.
    decided_by: tuple[int, ...] = ()


Operator = Literal[">", ">=", "<", "<="]
HysteresisRule = Literal[
    "immediate", "already_abnormal", "warning_confirmation", "recovery_confirmation", "none"
]
_LOGGER = logging.getLogger(__name__)
_STAGE = HealthDerivationStage
_OUTCOME = HealthCheckOutcome


@dataclass(slots=True)
class _Trace:
    """Checks recorded while the rules run. Purely observational: no rule reads it."""

    checks: list[dict] = field(default_factory=list)
    dependencies: HealthDependencyAggregation | None = None

    def check(
        self,
        stage: HealthDerivationStage,
        rule: str,
        outcome: HealthCheckOutcome,
        *,
        metric: str | None = None,
        observed: Decimal | None = None,
        operator: Operator | None = None,
        threshold: Decimal | None = None,
        leads_to: HealthStatus | None = None,
        note: str | None = None,
    ) -> int:
        self.checks.append(
            {
                "stage": stage,
                "rule": rule,
                "metric": metric,
                "observed": observed,
                "note": note,
                "operator": operator,
                "threshold": threshold,
                "outcome": outcome,
                "leads_to": leads_to,
                "decisive": False,
            }
        )
        return len(self.checks) - 1

    def threshold(
        self,
        stage: HealthDerivationStage,
        rule: str,
        metric: str,
        observed: Decimal | None,
        operator: Operator,
        threshold: Decimal,
        leads_to: HealthStatus,
    ) -> tuple[bool, int]:
        """Record ``observed operator threshold``; a missing value never breaches."""
        breached = observed is not None and _compare(observed, operator, threshold)
        outcome = (
            _OUTCOME.MISSING
            if observed is None
            else _OUTCOME.BREACH if breached else _OUTCOME.WITHIN
        )
        index = self.check(
            stage,
            rule,
            outcome,
            metric=metric,
            observed=observed,
            operator=operator,
            threshold=threshold,
            leads_to=leads_to,
        )
        return breached, index

    def mark(self, indices: Iterable[int]) -> None:
        for index in indices:
            self.checks[index]["decisive"] = True


def _compare(value: Decimal, operator: Operator, threshold: Decimal) -> bool:
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    raise ValueError(f"operator {operator} is not numeric")


class HealthEvaluator:
    """Pure health rules with hysteresis.

    Precedence is CRITICAL > UNKNOWN > WARNING > HEALTHY: a critical signal is
    never masked by missing data, and missing data is never reported as healthy.

    Every evaluation also records a `HealthDerivation`: the checks in the order
    they ran, the dependency aggregation, and the hysteresis step. Recording is
    observational only: no decision reads the trace.
    """

    def __init__(self, rules: RadarRuleSet | None = None) -> None:
        self.rules = rules or RadarRuleSet()

    def evaluate(
        self,
        entity: RadarEntity,
        snapshot: StateSnapshot,
        dependencies: Iterable[HealthState],
        history: HealthHistory,
        *,
        allocations: Mapping[str, Decimal] | None = None,
    ) -> HealthState:
        """Evaluate one entity.

        ``allocations`` (market entity ID -> share of vault assets) makes vault
        health an allocation-weighted aggregation of its market dependencies;
        without it every dependency counts fully (worst-of).
        """
        if snapshot.entity_id != entity.entity_id or snapshot.state_type is not entity.entity_type:
            raise ValueError("snapshot does not match entity")
        trace = _Trace()
        raw = self._raw(entity, snapshot, tuple(dependencies), allocations, trace)
        status, reasons, hysteresis = self._apply_hysteresis(raw, history, trace)
        score = {
            HealthStatus.HEALTHY: Decimal("1"),
            HealthStatus.WARNING: Decimal("0.5"),
            HealthStatus.CRITICAL: Decimal("0"),
            HealthStatus.UNKNOWN: None,
        }[status]
        return HealthState(
            entity_id=entity.entity_id,
            status=status,
            score=score,
            reasons=reasons,
            missing_fields=raw.missing_fields,
            evaluated_at=snapshot.retrieved_at,
            rule_version=self.rules.version,
            derivation=self._derivation(entity, snapshot, raw, hysteresis, status, score, trace),
        )

    def _derivation(
        self,
        entity: RadarEntity,
        snapshot: StateSnapshot,
        raw: _RawHealth,
        hysteresis: HealthHysteresis | None,
        status: HealthStatus,
        score: Decimal | None,
        trace: _Trace,
    ) -> HealthDerivation | None:
        if hysteresis is None:
            return None
        try:
            return HealthDerivation(
                entity_type=entity.entity_type,
                freshness=snapshot.freshness,
                checks=tuple(HealthCheck(**check) for check in trace.checks),
                dependencies=trace.dependencies,
                raw_status=raw.status,
                raw_reasons=raw.reasons,
                hysteresis=hysteresis,
                final_status=status,
                score=score,
                rule_version=self.rules.version,
            )
        except ValueError:
            # The explanation is additive: it never fails an evaluation.
            _LOGGER.warning("health derivation for %s is invalid", entity.entity_id, exc_info=True)
            return None

    def _raw(
        self,
        entity: RadarEntity,
        snapshot: StateSnapshot,
        dependencies: tuple[HealthState, ...],
        allocations: Mapping[str, Decimal] | None,
        trace: _Trace,
    ) -> _RawHealth:
        dependency = self._dependencies(entity, dependencies, allocations, trace)
        if dependency.status is HealthStatus.CRITICAL:
            trace.mark(dependency.decided_by)
            return dependency
        expired_check = trace.check(
            _STAGE.FRESHNESS,
            "An expired snapshot cannot be evaluated",
            _OUTCOME.BREACH if snapshot.freshness is FreshnessStatus.EXPIRED else _OUTCOME.WITHIN,
            metric="freshness",
            note=snapshot.freshness.value,
            leads_to=HealthStatus.UNKNOWN,
        )
        if snapshot.freshness is FreshnessStatus.EXPIRED:
            trace.mark((expired_check,))
            return _RawHealth(HealthStatus.UNKNOWN, ("snapshot is expired",))

        if entity.entity_type is RadarEntityType.MARKET:
            raw = self._market(snapshot, trace)
        elif entity.entity_type is RadarEntityType.VAULT:
            raw = self._vault(snapshot, trace)
        else:
            raw = self._generic(entity, snapshot, trace)

        if raw.status in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
            trace.mark(raw.decided_by)
            return raw
        if dependency.status is HealthStatus.UNKNOWN:
            trace.mark(dependency.decided_by)
            return dependency
        if raw.status is HealthStatus.HEALTHY and dependency.status is HealthStatus.WARNING:
            trace.mark(dependency.decided_by)
            return dependency
        if raw.status is HealthStatus.HEALTHY:
            fresh_check = trace.check(
                _STAGE.FRESHNESS,
                "HEALTHY requires a fresh snapshot",
                _OUTCOME.WITHIN if snapshot.freshness is FreshnessStatus.FRESH else _OUTCOME.BREACH,
                metric="freshness",
                note=snapshot.freshness.value,
                leads_to=HealthStatus.UNKNOWN,
            )
            if snapshot.freshness is not FreshnessStatus.FRESH:
                trace.mark((fresh_check,))
                return _RawHealth(
                    HealthStatus.UNKNOWN,
                    ("snapshot is stale; healthy requires fresh measurements",),
                )
        trace.mark(raw.decided_by)
        return raw

    def _dependencies(
        self,
        entity: RadarEntity,
        dependencies: tuple[HealthState, ...],
        allocations: Mapping[str, Decimal] | None,
        trace: _Trace,
    ) -> _RawHealth:
        weighted = (
            dict(allocations)
            if allocations is not None and entity.entity_type is RadarEntityType.VAULT
            else {}
        )
        material = self.rules.material_allocation
        totals = {status: Decimal(0) for status in HealthStatus}
        statuses: set[HealthStatus] = set()
        counts = {status: 0 for status in HealthStatus}
        contributions: list[HealthDependencyContribution] = []
        for item in dependencies:
            weight = weighted.get(item.entity_id)
            if weight is None:
                statuses.add(item.status)
                counts[item.status] += 1
                contributions.append(
                    HealthDependencyContribution(
                        entity_id=item.entity_id, status=item.status, counted="full"
                    )
                )
            else:
                totals[item.status] += weight
                contributions.append(
                    HealthDependencyContribution(
                        entity_id=item.entity_id,
                        status=item.status,
                        counted="weighted",
                        weight=weight,
                    )
                )
        has_full = any(item.counted == "full" for item in contributions)
        has_weighted = any(item.counted == "weighted" for item in contributions)
        stage = _STAGE.DEPENDENCIES

        def full(status: HealthStatus, rule: str) -> int | None:
            # A rule over dependencies counted in full cannot fire without any.
            if not has_full:
                return None
            return trace.threshold(
                stage,
                rule,
                f"{status.value.lower()}_dependencies",
                Decimal(counts[status]),
                ">",
                Decimal(0),
                status,
            )[1]

        def share(
            status: HealthStatus,
            rule: str,
            operator: Operator,
            threshold: Decimal,
            leads_to: HealthStatus,
            fires: bool,
        ) -> int | None:
            if not (has_weighted or fires):
                return None
            return trace.threshold(
                stage,
                rule,
                f"{status.value.lower()}_allocation",
                totals[status],
                operator,
                threshold,
                leads_to,
            )[1]

        def result(status: HealthStatus, reason: str, index: int | None) -> _RawHealth:
            if contributions:
                trace.dependencies = HealthDependencyAggregation(
                    dependencies=tuple(contributions),
                    weighted_totals=HealthStatusTotals(
                        **{item.value: totals[item] for item in HealthStatus}
                    ),
                    material_allocation=material,
                    status=status,
                    reason=reason,
                )
            return _RawHealth(status, (reason,), decided_by=() if index is None else (index,))

        index = full(HealthStatus.CRITICAL, "A dependency counted in full is CRITICAL")
        if HealthStatus.CRITICAL in statuses:
            return result(HealthStatus.CRITICAL, "critical dependency", index)
        fires = totals[HealthStatus.CRITICAL] >= material
        index = share(
            HealthStatus.CRITICAL,
            "CRITICAL markets hold a material allocation",
            ">=",
            material,
            HealthStatus.CRITICAL,
            fires,
        )
        if fires:
            return result(
                HealthStatus.CRITICAL, "critical market holds a material allocation", index
            )
        index = full(HealthStatus.UNKNOWN, "A dependency counted in full is UNKNOWN")
        if HealthStatus.UNKNOWN in statuses:
            return result(HealthStatus.UNKNOWN, "unknown dependency", index)
        fires = totals[HealthStatus.UNKNOWN] > material
        index = share(
            HealthStatus.UNKNOWN,
            "UNKNOWN markets hold more than a material allocation",
            ">",
            material,
            HealthStatus.UNKNOWN,
            fires,
        )
        if fires:
            return result(
                HealthStatus.UNKNOWN, "unknown market holds a material allocation", index
            )
        index = full(HealthStatus.WARNING, "A dependency counted in full is WARNING")
        if HealthStatus.WARNING in statuses:
            return result(HealthStatus.WARNING, "warning dependency", index)
        fires = totals[HealthStatus.CRITICAL] > 0
        index = share(
            HealthStatus.CRITICAL,
            "CRITICAL markets hold a minor allocation",
            ">",
            Decimal(0),
            HealthStatus.WARNING,
            fires,
        )
        if fires:
            return result(
                HealthStatus.WARNING, "critical market holds a minor allocation", index
            )
        fires = totals[HealthStatus.WARNING] >= material
        index = share(
            HealthStatus.WARNING,
            "WARNING markets hold a material allocation",
            ">=",
            material,
            HealthStatus.WARNING,
            fires,
        )
        if fires:
            return result(
                HealthStatus.WARNING, "warning market holds a material allocation", index
            )
        return result(HealthStatus.HEALTHY, "dependencies normal", None)

    def _market(self, snapshot: StateSnapshot, trace: _Trace) -> _RawHealth:
        metrics = {name: self._decimal(snapshot, name) for name in MARKET_REQUIRED}
        stage = _STAGE.METRICS
        # Critical evidence wins even when other measurements are missing.
        _, index = trace.threshold(
            stage,
            "Bad debt is recorded",
            "bad_debt",
            metrics["bad_debt"],
            ">",
            Decimal(0),
            HealthStatus.CRITICAL,
        )
        if self._at_least(metrics["bad_debt"], Decimal(0), strict=True):
            return _RawHealth(HealthStatus.CRITICAL, ("bad debt detected",), decided_by=(index,))
        _, index = trace.threshold(
            stage,
            "Utilization at or above the critical threshold",
            "utilization",
            metrics["utilization"],
            ">=",
            self.rules.market_critical_utilization,
            HealthStatus.CRITICAL,
        )
        if self._at_least(metrics["utilization"], self.rules.market_critical_utilization):
            return _RawHealth(
                HealthStatus.CRITICAL,
                ("utilization exceeds critical threshold",),
                decided_by=(index,),
            )
        _, index = trace.threshold(
            stage,
            "Near-liquidation debt at or above the critical threshold",
            "near_liquidation_debt_ratio",
            metrics["near_liquidation_debt_ratio"],
            ">=",
            self.rules.near_liquidation_critical_ratio,
            HealthStatus.CRITICAL,
        )
        if self._at_least(
            metrics["near_liquidation_debt_ratio"], self.rules.near_liquidation_critical_ratio
        ):
            return _RawHealth(
                HealthStatus.CRITICAL,
                ("near-liquidation debt exceeds critical threshold",),
                decided_by=(index,),
            )
        missing = self._missing(snapshot, metrics)
        index = self._completeness(trace, "market", missing)
        if missing:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("market metrics are incomplete",),
                missing,
                decided_by=(index,),
            )
        values = {name: value for name, value in metrics.items() if value is not None}
        warnings: tuple[tuple[str, Operator, Decimal, str, str], ...] = (
            (
                "utilization",
                ">=",
                self.rules.market_warning_utilization,
                "Utilization at or above the warning threshold",
                "utilization exceeds warning threshold",
            ),
            (
                "lender_hhi",
                ">=",
                self.rules.concentration_warning_hhi,
                "Lender concentration (HHI) at or above the warning threshold",
                "lender concentration exceeds warning threshold",
            ),
            (
                "borrower_hhi",
                ">=",
                self.rules.concentration_warning_hhi,
                "Borrower concentration (HHI) at or above the warning threshold",
                "borrower concentration exceeds warning threshold",
            ),
            (
                "near_liquidation_debt_ratio",
                ">=",
                self.rules.near_liquidation_warning_ratio,
                "Near-liquidation debt at or above the warning threshold",
                "near-liquidation debt exceeds warning threshold",
            ),
            (
                "available_liquidity",
                "<=",
                Decimal(0),
                "Available liquidity is exhausted",
                "available liquidity is exhausted",
            ),
        )
        reasons: list[str] = []
        decided: list[int] = []
        for name, operator, threshold, rule, reason in warnings:
            breached, index = trace.threshold(
                stage, rule, name, values[name], operator, threshold, HealthStatus.WARNING
            )
            if breached:
                reasons.append(reason)
                decided.append(index)
        return _RawHealth(
            HealthStatus.WARNING if reasons else HealthStatus.HEALTHY,
            tuple(reasons) or ("market metrics normal",),
            decided_by=tuple(decided),
        )

    def _vault(self, snapshot: StateSnapshot, trace: _Trace) -> _RawHealth:
        metrics = {name: self._decimal(snapshot, name) for name in VAULT_REQUIRED}
        stage = _STAGE.METRICS
        missing = self._missing(snapshot, metrics)
        index = self._completeness(trace, "vault", missing)
        if missing:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("vault metrics are incomplete",),
                missing,
                decided_by=(index,),
            )
        values = {name: value for name, value in metrics.items() if value is not None}
        _, index = trace.threshold(
            stage,
            "Unmodelled allocation above the material share",
            "unmodelled_allocation_ratio",
            values["unmodelled_allocation_ratio"],
            ">",
            self.rules.material_unmodelled_allocation,
            HealthStatus.UNKNOWN,
        )
        if values["unmodelled_allocation_ratio"] > self.rules.material_unmodelled_allocation:
            return _RawHealth(
                HealthStatus.UNKNOWN, ("material unmodelled allocation",), decided_by=(index,)
            )
        _, index = trace.threshold(
            stage,
            "Withdrawal liquidity is exhausted",
            "withdrawal_liquidity",
            values["withdrawal_liquidity"],
            "<=",
            Decimal(0),
            HealthStatus.WARNING,
        )
        if values["withdrawal_liquidity"] <= 0:
            return _RawHealth(
                HealthStatus.WARNING, ("withdrawal liquidity is exhausted",), decided_by=(index,)
            )
        _, index = trace.threshold(
            stage,
            "Allocation concentration (HHI) at or above the warning threshold",
            "allocation_hhi",
            values["allocation_hhi"],
            ">=",
            self.rules.concentration_warning_hhi,
            HealthStatus.WARNING,
        )
        if values["allocation_hhi"] >= self.rules.concentration_warning_hhi:
            return _RawHealth(
                HealthStatus.WARNING,
                ("allocation concentration exceeds warning threshold",),
                decided_by=(index,),
            )
        return _RawHealth(HealthStatus.HEALTHY, ("vault metrics normal",))

    def _generic(self, entity: RadarEntity, snapshot: StateSnapshot, trace: _Trace) -> _RawHealth:
        stage = _STAGE.METRICS
        if entity.entity_type is RadarEntityType.ORACLE:
            price = self._decimal(snapshot, "oracle_price")
            _, index = trace.threshold(
                stage,
                "Oracle price is zero or negative",
                "oracle_price",
                price,
                "<=",
                Decimal(0),
                HealthStatus.CRITICAL,
            )
            if price is not None and price <= 0:
                return _RawHealth(
                    HealthStatus.CRITICAL,
                    ("oracle reported a non-positive price",),
                    decided_by=(index,),
                )
        index = trace.check(
            stage,
            "The source reports missing fields",
            _OUTCOME.MISSING if snapshot.missing_fields else _OUTCOME.WITHIN,
            metric="missing_fields",
            observed=Decimal(len(snapshot.missing_fields)),
            operator=">",
            threshold=Decimal(0),
            leads_to=HealthStatus.UNKNOWN,
            note=", ".join(sorted(snapshot.missing_fields)) or None,
        )
        if snapshot.missing_fields:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("required source fields are missing",),
                tuple(sorted(snapshot.missing_fields)),
                decided_by=(index,),
            )
        explicit = snapshot.metrics.get("status")
        if explicit in {item.value for item in HealthStatus}:
            index = trace.check(
                stage,
                "The source supplies an explicit status",
                _OUTCOME.INFO,
                metric="status",
                note=str(explicit),
                leads_to=HealthStatus(str(explicit)),
            )
            return _RawHealth(
                HealthStatus(str(explicit)),
                ("source supplied explicit status",),
                decided_by=(index,),
            )
        present = sum(value is not None for value in snapshot.metrics.values())
        index = trace.check(
            stage,
            "No measurement is available",
            _OUTCOME.MISSING if present == 0 else _OUTCOME.WITHIN,
            metric="measurements",
            observed=Decimal(present),
            operator="<",
            threshold=Decimal(1),
            leads_to=HealthStatus.UNKNOWN,
        )
        if all(value is None for value in snapshot.metrics.values()):
            return _RawHealth(
                HealthStatus.UNKNOWN, ("no measurements available",), decided_by=(index,)
            )
        return _RawHealth(HealthStatus.HEALTHY, ("required measurements are fresh",))

    @staticmethod
    def _completeness(trace: _Trace, kind: str, missing: tuple[str, ...]) -> int:
        return trace.check(
            _STAGE.METRICS,
            f"Required {kind} metrics are missing",
            _OUTCOME.MISSING if missing else _OUTCOME.WITHIN,
            metric="missing_fields",
            observed=Decimal(len(missing)),
            operator=">",
            threshold=Decimal(0),
            leads_to=HealthStatus.UNKNOWN,
            note=", ".join(missing) or None,
        )

    def _apply_hysteresis(
        self, raw: _RawHealth, history: HealthHistory, trace: _Trace
    ) -> tuple[HealthStatus, tuple[str, ...], HealthHysteresis | None]:
        previous = history.current.status if history.current is not None else None
        stage = _STAGE.HYSTERESIS

        def record(
            final: HealthStatus,
            rule: HysteresisRule,
            *,
            counter: int | None = None,
            required: int | None = None,
        ) -> HealthHysteresis | None:
            try:
                return HealthHysteresis(
                    previous_status=previous,
                    raw_status=raw.status,
                    rule=rule,
                    consecutive_abnormal=history.consecutive_abnormal,
                    consecutive_normal=history.consecutive_normal,
                    counter=counter,
                    cycles_required=required,
                    held=final is not raw.status,
                    final_status=final,
                )
            except ValueError:
                return None

        if raw.status in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
            trace.check(
                stage,
                f"{raw.status.value} applies immediately, without confirmation cycles",
                _OUTCOME.INFO,
                leads_to=raw.status,
            )
            return raw.status, raw.reasons, record(raw.status, "immediate")
        if raw.status is HealthStatus.WARNING:
            # Coming from an abnormal state, the warning condition has already
            # persisted; only a healthy entity gets the confirmation cycle.
            if previous in (None, HealthStatus.HEALTHY):
                abnormal = history.consecutive_abnormal + 1
                cycles = self.rules.warning_cycles
                index = trace.check(
                    stage,
                    f"WARNING needs {cycles} consecutive abnormal evaluations",
                    _OUTCOME.INFO,
                    metric="consecutive_abnormal",
                    observed=Decimal(abnormal),
                    # The condition as it resolved: held below, confirmed at or above.
                    operator="<" if abnormal < cycles else ">=",
                    threshold=Decimal(cycles),
                    leads_to=(
                        HealthStatus.HEALTHY if abnormal < cycles else HealthStatus.WARNING
                    ),
                )
                if abnormal < self.rules.warning_cycles:
                    trace.mark((index,))
                    return (
                        HealthStatus.HEALTHY,
                        (*raw.reasons, f"warning pending ({abnormal}/{self.rules.warning_cycles})"),
                        record(
                            HealthStatus.HEALTHY,
                            "warning_confirmation",
                            counter=abnormal,
                            required=cycles,
                        ),
                    )
                return (
                    HealthStatus.WARNING,
                    raw.reasons,
                    record(
                        HealthStatus.WARNING,
                        "warning_confirmation",
                        counter=abnormal,
                        required=cycles,
                    ),
                )
            trace.check(
                stage,
                "Already abnormal: WARNING applies without confirmation cycles",
                _OUTCOME.INFO,
                note=previous.value if previous is not None else None,
                leads_to=HealthStatus.WARNING,
            )
            return HealthStatus.WARNING, raw.reasons, record(HealthStatus.WARNING, "already_abnormal")
        if previous is not None and previous is not HealthStatus.HEALTHY:
            normal = history.consecutive_normal + 1
            cycles = self.rules.recovery_cycles
            index = trace.check(
                stage,
                f"Recovery to HEALTHY needs {cycles} consecutive normal evaluations",
                _OUTCOME.INFO,
                metric="consecutive_normal",
                observed=Decimal(normal),
                operator="<" if normal < cycles else ">=",
                threshold=Decimal(cycles),
                leads_to=previous if normal < cycles else HealthStatus.HEALTHY,
                note=previous.value,
            )
            if normal < self.rules.recovery_cycles:
                trace.mark((index,))
                return (
                    previous,
                    (*raw.reasons, f"recovery pending ({normal}/{self.rules.recovery_cycles})"),
                    record(previous, "recovery_confirmation", counter=normal, required=cycles),
                )
            return (
                HealthStatus.HEALTHY,
                raw.reasons,
                record(
                    HealthStatus.HEALTHY, "recovery_confirmation", counter=normal, required=cycles
                ),
            )
        return HealthStatus.HEALTHY, raw.reasons, record(HealthStatus.HEALTHY, "none")

    @staticmethod
    def _missing(
        snapshot: StateSnapshot, metrics: Mapping[str, Decimal | None]
    ) -> tuple[str, ...]:
        names = set(snapshot.missing_fields) | {
            name for name, value in metrics.items() if value is None
        }
        return tuple(sorted(names))

    @staticmethod
    def _at_least(value: Decimal | None, threshold: Decimal, *, strict: bool = False) -> bool:
        if value is None:
            return False
        return value > threshold if strict else value >= threshold

    @staticmethod
    def _decimal(snapshot: StateSnapshot, name: str) -> Decimal | None:
        """Return a finite decimal metric, or None when absent or unusable."""
        value = snapshot.metrics.get(name)
        if isinstance(value, bool) or value is None:
            return None
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
