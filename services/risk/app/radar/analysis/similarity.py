from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.radar.contracts import (
    SIMILARITY_STORAGE_THRESHOLD,
    SIMILARITY_VISIBILITY_THRESHOLD,
    SIMILARITY_VISIBLE_LIMIT,
    SimilarityRecord,
)
from app.radar.domain.observations import VaultProfile


@dataclass(frozen=True, slots=True)
class NeighborSelection:
    stored: tuple[SimilarityRecord, ...]
    visible: tuple[SimilarityRecord, ...]


COMPONENT_QUANTUM = Decimal("0.0000000000000000000000000001")


class VaultSimilarity:
    def __init__(
        self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._clock = clock

    def score(self, left: VaultProfile, right: VaultProfile) -> SimilarityRecord:
        if left.entity_id == right.entity_id:
            raise ValueError("cannot score a vault against itself")
        components: dict[str, Decimal] = {}
        missing: list[str] = []

        components["shared_markets"] = Decimal("0.35") * self._weighted_jaccard(
            left.market_allocations, right.market_allocations
        )
        components["asset_overlap"] = Decimal("0.20") * self._jaccard(
            left.loan_assets | left.collateral_assets,
            right.loan_assets | right.collateral_assets,
        )
        components["oracle_overlap"] = self._set_component(
            "oracle_overlap", Decimal("0.15"), left.oracle_groups, right.oracle_groups, missing
        )
        components["irm_match"] = self._set_component(
            "irm_match", Decimal("0.10"), left.irms, right.irms, missing
        )
        components["lltv_proximity"] = self._lltv_component(left, right, missing)
        components["same_chain"] = Decimal("0.05") if left.chain_id == right.chain_id else Decimal(0)
        if left.curator is None or right.curator is None:
            missing.append("same_curator")
            components["same_curator"] = Decimal(0)
        else:
            components["same_curator"] = Decimal("0.05") if left.curator == right.curator else Decimal(0)

        normalized = {name: value.quantize(COMPONENT_QUANTUM) for name, value in components.items()}
        # Thresholds are strict, so the total is kept unrounded: rounding first
        # could push a 0.3349 score to 0.33 (or 0.6049 to 0.60) and flip policy.
        score = sum(normalized.values(), Decimal(0))
        return SimilarityRecord(
            catalog_entity_id=left.entity_id,
            candidate_entity_id=right.entity_id,
            score=score,
            components=normalized,
            missing_dimensions=tuple(missing),
            stored=score > SIMILARITY_STORAGE_THRESHOLD,
            normally_visible=score > SIMILARITY_VISIBILITY_THRESHOLD,
            computed_at=self._clock(),
        )

    def select_neighbors(
        self, catalog_id: str, scores: Iterable[SimilarityRecord]
    ) -> NeighborSelection:
        best: dict[str, SimilarityRecord] = {}
        for record in scores:
            if (
                record.catalog_entity_id != catalog_id
                or record.candidate_entity_id == catalog_id
                or record.score <= SIMILARITY_STORAGE_THRESHOLD
            ):
                continue
            kept = best.get(record.candidate_entity_id)
            if kept is None or (record.score, record.computed_at) > (kept.score, kept.computed_at):
                best[record.candidate_entity_id] = record
        candidates = tuple(best.values())
        ordered = tuple(sorted(candidates, key=lambda item: (-item.score, item.candidate_entity_id)))
        visible = tuple(
            item for item in ordered if item.score > SIMILARITY_VISIBILITY_THRESHOLD
        )[:SIMILARITY_VISIBLE_LIMIT]
        visible_ids = {item.candidate_entity_id for item in visible}
        stored = tuple(
            item.model_copy(
                update={
                    "stored": True,
                    "normally_visible": item.candidate_entity_id in visible_ids,
                }
            )
            for item in ordered
        )
        visible_by_id = {item.candidate_entity_id: item for item in stored}
        return NeighborSelection(
            stored=stored,
            visible=tuple(visible_by_id[item.candidate_entity_id] for item in visible),
        )

    @staticmethod
    def _weighted_jaccard(
        left: Mapping[str, Decimal], right: Mapping[str, Decimal]
    ) -> Decimal:
        left, right = _shares(left), _shares(right)
        keys = set(left) | set(right)
        if not keys:
            return Decimal(0)
        numerator = sum((min(left.get(key, Decimal(0)), right.get(key, Decimal(0))) for key in keys), Decimal(0))
        denominator = sum((max(left.get(key, Decimal(0)), right.get(key, Decimal(0))) for key in keys), Decimal(0))
        return Decimal(0) if denominator == 0 else numerator / denominator

    @staticmethod
    def _jaccard(left: Set[str], right: Set[str]) -> Decimal:
        union = left | right
        return Decimal(0) if not union else Decimal(len(left & right)) / Decimal(len(union))

    def _set_component(
        self,
        name: str,
        weight: Decimal,
        left: Set[str],
        right: Set[str],
        missing: list[str],
    ) -> Decimal:
        if not left or not right:
            missing.append(name)
            return Decimal(0)
        return weight * self._jaccard(left, right)

    @staticmethod
    def _lltv_component(
        left: VaultProfile, right: VaultProfile, missing: list[str]
    ) -> Decimal:
        if not left.lltvs or not right.lltvs:
            missing.append("lltv_proximity")
            return Decimal(0)
        left_average = sum(left.lltvs, Decimal(0)) / Decimal(len(left.lltvs))
        right_average = sum(right.lltvs, Decimal(0)) / Decimal(len(right.lltvs))
        return Decimal("0.10") * max(Decimal(0), Decimal(1) - abs(left_average - right_average))


def _shares(allocations: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Normalize allocations to shares of the modelled total so vaults with
    different idle/unmodelled remainders compare on the same scale."""
    total = sum(allocations.values(), Decimal(0))
    if total <= 0:
        return {}
    return {key: value / total for key, value in allocations.items() if value > 0}


__all__ = ["NeighborSelection", "VaultProfile", "VaultSimilarity"]
