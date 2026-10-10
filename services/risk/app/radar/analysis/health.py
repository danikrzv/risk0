from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

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
    def __init__(self, rules: RadarRuleSet | None = None) -> None:
        self.rules = rules or RadarRuleSet()

    def evaluate(
        self,
        entity: RadarEntity,
        snapshot: StateSnapshot,
        dependencies: Iterable[HealthState],
        history: HealthHistory,
    ) -> HealthState:
        if snapshot.entity_id != entity.entity_id or snapshot.state_type is not entity.entity_type:
            raise ValueError("snapshot does not match entity")
        raw = self._raw(entity, snapshot, tuple(dependencies))
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
    ) -> _RawHealth:
        if snapshot.freshness is FreshnessStatus.EXPIRED:
            return _RawHealth(HealthStatus.UNKNOWN, ("snapshot is expired",))
        if snapshot.missing_fields:
            return _RawHealth(
                HealthStatus.UNKNOWN,
                ("required source fields are missing",),
                tuple(sorted(snapshot.missing_fields)),
            )
        dependency_statuses = {item.status for item in dependencies}
        if HealthStatus.CRITICAL in dependency_statuses:
            return _RawHealth(HealthStatus.CRITICAL, ("critical dependency",))
        if HealthStatus.UNKNOWN in dependency_statuses:
            return _RawHealth(HealthStatus.UNKNOWN, ("unknown dependency",))

        if entity.entity_type is RadarEntityType.MARKET:
            raw = self._market(snapshot)
        elif entity.entity_type is RadarEntityType.VAULT:
            raw = self._vault(snapshot)
        else:
            raw = self._generic(snapshot)

        if raw.status is HealthStatus.HEALTHY and HealthStatus.WARNING in dependency_statuses:
            return _RawHealth(HealthStatus.WARNING, ("warning dependency",))
        return raw

    def _market(self, snapshot: StateSnapshot) -> _RawHealth:
        missing = self._missing(snapshot, MARKET_REQUIRED)
        if missing:
            return _RawHealth(HealthStatus.UNKNOWN, ("market metrics are incomplete",), missing)
        metrics = {name: self._decimal(snapshot, name) for name in MARKET_REQUIRED}
        if metrics["bad_debt"] > 0:
            return _RawHealth(HealthStatus.CRITICAL, ("bad debt detected",))
        if metrics["utilization"] >= self.rules.market_critical_utilization:
            return _RawHealth(HealthStatus.CRITICAL, ("utilization exceeds critical threshold",))
        if metrics["near_liquidation_debt_ratio"] >= self.rules.near_liquidation_critical_ratio:
            return _RawHealth(HealthStatus.CRITICAL, ("near-liquidation debt exceeds critical threshold",))
        reasons: list[str] = []
        if metrics["utilization"] >= self.rules.market_warning_utilization:
            reasons.append("utilization exceeds warning threshold")
        if metrics["lender_hhi"] >= self.rules.concentration_warning_hhi:
            reasons.append("lender concentration exceeds warning threshold")
        if metrics["borrower_hhi"] >= self.rules.concentration_warning_hhi:
            reasons.append("borrower concentration exceeds warning threshold")
        if metrics["near_liquidation_debt_ratio"] >= self.rules.near_liquidation_warning_ratio:
            reasons.append("near-liquidation debt exceeds warning threshold")
        if metrics["available_liquidity"] <= 0:
            reasons.append("available liquidity is exhausted")
        return _RawHealth(HealthStatus.WARNING if reasons else HealthStatus.HEALTHY, tuple(reasons) or ("market metrics normal",))

    def _vault(self, snapshot: StateSnapshot) -> _RawHealth:
        missing = self._missing(snapshot, VAULT_REQUIRED)
        if missing:
            return _RawHealth(HealthStatus.UNKNOWN, ("vault metrics are incomplete",), missing)
        unmodelled = self._decimal(snapshot, "unmodelled_allocation_ratio")
        if unmodelled > self.rules.material_unmodelled_allocation:
            return _RawHealth(HealthStatus.UNKNOWN, ("material unmodelled allocation",))
        if self._decimal(snapshot, "withdrawal_liquidity") <= 0:
            return _RawHealth(HealthStatus.WARNING, ("withdrawal liquidity is exhausted",))
        if self._decimal(snapshot, "allocation_hhi") >= self.rules.concentration_warning_hhi:
            return _RawHealth(HealthStatus.WARNING, ("allocation concentration exceeds warning threshold",))
        return _RawHealth(HealthStatus.HEALTHY, ("vault metrics normal",))

    def _generic(self, snapshot: StateSnapshot) -> _RawHealth:
        explicit = snapshot.metrics.get("status")
        if explicit in {item.value for item in HealthStatus}:
            return _RawHealth(HealthStatus(str(explicit)), ("source supplied explicit status",))
        return _RawHealth(HealthStatus.HEALTHY, ("required measurements are fresh",))

    def _apply_hysteresis(
        self, raw: _RawHealth, history: HealthHistory
    ) -> tuple[HealthStatus, tuple[str, ...]]:
        if raw.status in (HealthStatus.CRITICAL, HealthStatus.UNKNOWN):
            return raw.status, raw.reasons
        if raw.status is HealthStatus.WARNING:
            abnormal = history.consecutive_abnormal + 1
            if abnormal < self.rules.warning_cycles:
                return HealthStatus.HEALTHY, (*raw.reasons, f"warning pending ({abnormal}/{self.rules.warning_cycles})")
            return HealthStatus.WARNING, raw.reasons
        if history.current is not None and history.current.status is not HealthStatus.HEALTHY:
            normal = history.consecutive_normal + 1
            if normal < self.rules.recovery_cycles:
                return history.current.status, (*raw.reasons, f"recovery pending ({normal}/{self.rules.recovery_cycles})")
        return HealthStatus.HEALTHY, raw.reasons

    @staticmethod
    def _missing(snapshot: StateSnapshot, required: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(name for name in required if snapshot.metrics.get(name) is None)

    @staticmethod
    def _decimal(snapshot: StateSnapshot, name: str) -> Decimal:
        value = snapshot.metrics.get(name)
        try:
            if isinstance(value, bool) or value is None:
                raise InvalidOperation
            return Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError(f"metric {name} must be a decimal string") from error
