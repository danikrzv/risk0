from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from app.contracts.models import FreshnessStatus
from app.radar.contracts import HealthState, HealthStatus, RadarEntity, RadarEntityType, StateSnapshot
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


class HealthEvaluator:
    """Pure health rules with hysteresis.

    Precedence is CRITICAL > UNKNOWN > WARNING > HEALTHY: a critical signal is
    never masked by missing data, and missing data is never reported as healthy.
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
        raw = self._raw(entity, snapshot, tuple(dependencies), allocations)
        status, reasons = self._apply_hysteresis(raw, history)
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
        )

    def _raw(
        self,
        entity: RadarEntity,
        snapshot: StateSnapshot,
        dependencies: tuple[HealthState, ...],
        allocations: Mapping[str, Decimal] | None,
    ) -> _RawHealth:
        dependency = self._dependencies(entity, dependencies, allocations)
        if dependency.status is HealthStatus.CRITICAL:
            return dependency
        if snapshot.freshness is FreshnessStatus.EXPIRED:
            return _RawHealth(HealthStatus.UNKNOWN, ("snapshot is expired",))

        if entity.entity_type is RadarEntityType.MARKET:
            raw = self._market(snapshot)
        elif entity.entity_type is RadarEntityType.VAULT:
            raw = self._vault(snapshot)
        else:
            raw = self._generic(entity, snapshot)

        if raw.status in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
            return raw
        if dependency.status is HealthStatus.UNKNOWN:
            return dependency
        if raw.status is HealthStatus.HEALTHY and dependency.status is HealthStatus.WARNING:
            return dependency
        if raw.status is HealthStatus.HEALTHY and snapshot.freshness is not FreshnessStatus.FRESH:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("snapshot is stale; healthy requires fresh measurements",),
            )
        return raw

    def _dependencies(
        self,
        entity: RadarEntity,
        dependencies: tuple[HealthState, ...],
        allocations: Mapping[str, Decimal] | None,
    ) -> _RawHealth:
        weighted = (
            dict(allocations)
            if allocations is not None and entity.entity_type is RadarEntityType.VAULT
            else {}
        )
        material = self.rules.material_allocation
        totals = {status: Decimal(0) for status in HealthStatus}
        statuses: set[HealthStatus] = set()
        for item in dependencies:
            weight = weighted.get(item.entity_id)
            if weight is None:
                statuses.add(item.status)
            else:
                totals[item.status] += weight
        if HealthStatus.CRITICAL in statuses:
            return _RawHealth(HealthStatus.CRITICAL, ("critical dependency",))
        if totals[HealthStatus.CRITICAL] >= material:
            return _RawHealth(HealthStatus.CRITICAL, ("critical market holds a material allocation",))
        if HealthStatus.UNKNOWN in statuses:
            return _RawHealth(HealthStatus.UNKNOWN, ("unknown dependency",))
        if totals[HealthStatus.UNKNOWN] > material:
            return _RawHealth(HealthStatus.UNKNOWN, ("unknown market holds a material allocation",))
        if HealthStatus.WARNING in statuses:
            return _RawHealth(HealthStatus.WARNING, ("warning dependency",))
        if totals[HealthStatus.CRITICAL] > 0:
            return _RawHealth(HealthStatus.WARNING, ("critical market holds a minor allocation",))
        if totals[HealthStatus.WARNING] >= material:
            return _RawHealth(HealthStatus.WARNING, ("warning market holds a material allocation",))
        return _RawHealth(HealthStatus.HEALTHY, ("dependencies normal",))

    def _market(self, snapshot: StateSnapshot) -> _RawHealth:
        metrics = {name: self._decimal(snapshot, name) for name in MARKET_REQUIRED}
        # Critical evidence wins even when other measurements are missing.
        if self._at_least(metrics["bad_debt"], Decimal(0), strict=True):
            return _RawHealth(HealthStatus.CRITICAL, ("bad debt detected",))
        if self._at_least(metrics["utilization"], self.rules.market_critical_utilization):
            return _RawHealth(HealthStatus.CRITICAL, ("utilization exceeds critical threshold",))
        if self._at_least(
            metrics["near_liquidation_debt_ratio"], self.rules.near_liquidation_critical_ratio
        ):
            return _RawHealth(HealthStatus.CRITICAL, ("near-liquidation debt exceeds critical threshold",))
        missing = self._missing(snapshot, metrics)
        if missing:
            return _RawHealth(HealthStatus.UNKNOWN, ("market metrics are incomplete",), missing)
        values = {name: value for name, value in metrics.items() if value is not None}
        reasons: list[str] = []
        if values["utilization"] >= self.rules.market_warning_utilization:
            reasons.append("utilization exceeds warning threshold")
        if values["lender_hhi"] >= self.rules.concentration_warning_hhi:
            reasons.append("lender concentration exceeds warning threshold")
        if values["borrower_hhi"] >= self.rules.concentration_warning_hhi:
            reasons.append("borrower concentration exceeds warning threshold")
        if values["near_liquidation_debt_ratio"] >= self.rules.near_liquidation_warning_ratio:
            reasons.append("near-liquidation debt exceeds warning threshold")
        if values["available_liquidity"] <= 0:
            reasons.append("available liquidity is exhausted")
        return _RawHealth(HealthStatus.WARNING if reasons else HealthStatus.HEALTHY, tuple(reasons) or ("market metrics normal",))

    def _vault(self, snapshot: StateSnapshot) -> _RawHealth:
        metrics = {name: self._decimal(snapshot, name) for name in VAULT_REQUIRED}
        missing = self._missing(snapshot, metrics)
        if missing:
            return _RawHealth(HealthStatus.UNKNOWN, ("vault metrics are incomplete",), missing)
        values = {name: value for name, value in metrics.items() if value is not None}
        if values["unmodelled_allocation_ratio"] > self.rules.material_unmodelled_allocation:
            return _RawHealth(HealthStatus.UNKNOWN, ("material unmodelled allocation",))
        if values["withdrawal_liquidity"] <= 0:
            return _RawHealth(HealthStatus.WARNING, ("withdrawal liquidity is exhausted",))
        if values["allocation_hhi"] >= self.rules.concentration_warning_hhi:
            return _RawHealth(HealthStatus.WARNING, ("allocation concentration exceeds warning threshold",))
        return _RawHealth(HealthStatus.HEALTHY, ("vault metrics normal",))

    def _generic(self, entity: RadarEntity, snapshot: StateSnapshot) -> _RawHealth:
        if entity.entity_type is RadarEntityType.ORACLE:
            price = self._decimal(snapshot, "oracle_price")
            if price is not None and price <= 0:
                return _RawHealth(HealthStatus.CRITICAL, ("oracle reported a non-positive price",))
        if snapshot.missing_fields:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("required source fields are missing",),
                tuple(sorted(snapshot.missing_fields)),
            )
        explicit = snapshot.metrics.get("status")
        if explicit in {item.value for item in HealthStatus}:
            return _RawHealth(HealthStatus(str(explicit)), ("source supplied explicit status",))
        if all(value is None for value in snapshot.metrics.values()):
            return _RawHealth(HealthStatus.UNKNOWN, ("no measurements available",))
        return _RawHealth(HealthStatus.HEALTHY, ("required measurements are fresh",))

    def _apply_hysteresis(
        self, raw: _RawHealth, history: HealthHistory
    ) -> tuple[HealthStatus, tuple[str, ...]]:
        if raw.status in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
            return raw.status, raw.reasons
        previous = history.current.status if history.current is not None else None
        if raw.status is HealthStatus.WARNING:
            # Coming from an abnormal state, the warning condition has already
            # persisted; only a healthy entity gets the confirmation cycle.
            if previous in (None, HealthStatus.HEALTHY):
                abnormal = history.consecutive_abnormal + 1
                if abnormal < self.rules.warning_cycles:
                    return HealthStatus.HEALTHY, (*raw.reasons, f"warning pending ({abnormal}/{self.rules.warning_cycles})")
            return HealthStatus.WARNING, raw.reasons
        if previous is not None and previous is not HealthStatus.HEALTHY:
            normal = history.consecutive_normal + 1
            if normal < self.rules.recovery_cycles:
                return previous, (*raw.reasons, f"recovery pending ({normal}/{self.rules.recovery_cycles})")
        return HealthStatus.HEALTHY, raw.reasons

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
