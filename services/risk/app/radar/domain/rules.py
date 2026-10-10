from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RadarRuleSet:
    version: str = "radar-health-v1"
    warning_cycles: int = 2
    recovery_cycles: int = 3
    oracle_warning_deviation: Decimal = Decimal("0.02")
    oracle_critical_deviation: Decimal = Decimal("0.05")
    market_warning_utilization: Decimal = Decimal("0.90")
    market_critical_utilization: Decimal = Decimal("0.98")
    concentration_warning_hhi: Decimal = Decimal("0.25")
    near_liquidation_warning_ratio: Decimal = Decimal("0.10")
    near_liquidation_critical_ratio: Decimal = Decimal("0.30")
    material_unmodelled_allocation: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.warning_cycles < 1 or self.recovery_cycles < 1:
            raise ValueError("hysteresis cycles must be positive")
