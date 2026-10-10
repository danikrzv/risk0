from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from app.radar.contracts import HealthStatus
from app.radar.domain.observations import OracleFeedObservation
from app.radar.domain.rules import RadarRuleSet


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    status: HealthStatus
    reference_price: Decimal | None
    deviations: Mapping[str, Decimal]
    feed_statuses: Mapping[str, HealthStatus]
    included: tuple[str, ...]
    excluded: tuple[str, ...]
    derived_feeds: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deviations", MappingProxyType(dict(self.deviations)))
        object.__setattr__(self, "feed_statuses", MappingProxyType(dict(self.feed_statuses)))


MIN_INDEPENDENT_PEERS = 2


class OracleConsensus:
    def __init__(self, rules: RadarRuleSet | None = None) -> None:
        self.rules = rules or RadarRuleSet()

    def evaluate(
        self, group: Sequence[OracleFeedObservation], now: datetime
    ) -> ConsensusResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("consensus timestamp must be timezone-aware")
        pairs = {feed.represented_pair for feed in group}
        if len(pairs) > 1:
            raise ValueError("oracle consensus group must represent one pair")
        fresh = tuple(feed for feed in group if self._is_fresh(feed, now))
        excluded = tuple(sorted(feed.entity_id for feed in group if feed not in fresh))
        # A hard failure is CRITICAL on its own, whatever its age or peer count.
        failed = tuple(feed for feed in group if feed.hard_failure)
        usable = tuple(feed for feed in fresh if not feed.hard_failure)
        derived = tuple(sorted(feed.entity_id for feed in fresh if feed.derived))
        hard_statuses = {feed.entity_id: HealthStatus.CRITICAL for feed in failed}
        hard_reasons = (
            ("oracle reported a non-positive price",) if failed else ()
        )
        families = {feed.source_family for feed in usable}
        statuses: dict[str, HealthStatus] = dict(hard_statuses)
        deviations: dict[str, Decimal] = {}
        reference: Decimal | None = None
        if usable:
            reference = self._median(tuple(feed.price for feed in usable))
        for feed in usable:
            # A feed is never its own peer: it needs enough fresh feeds from
            # other, independent source families to be judged at all.
            peers = families - {feed.source_family}
            if len(peers) < MIN_INDEPENDENT_PEERS:
                statuses[feed.entity_id] = HealthStatus.UNKNOWN
                continue
            assert reference is not None
            deviation = abs(feed.price - reference) / reference
            deviations[feed.entity_id] = deviation
            statuses[feed.entity_id] = self._status(deviation)
        for feed in group:
            statuses.setdefault(feed.entity_id, HealthStatus.UNKNOWN)
        if not deviations:
            return ConsensusResult(
                status=HealthStatus.CRITICAL if failed else HealthStatus.UNKNOWN,
                reference_price=None,
                deviations={},
                feed_statuses=statuses,
                included=(),
                excluded=excluded,
                derived_feeds=derived,
                reasons=(*hard_reasons, "two independent fresh oracle peers are required"),
            )
        return ConsensusResult(
            status=self._worst(
                tuple(statuses[feed.entity_id] for feed in (*usable, *failed))
            ),
            reference_price=reference,
            deviations=deviations,
            feed_statuses=statuses,
            included=tuple(sorted(deviations)),
            excluded=excluded,
            derived_feeds=derived,
            reasons=(*hard_reasons, "fresh independent feeds compared against median consensus"),
        )

    @staticmethod
    def _is_fresh(feed: OracleFeedObservation, now: datetime) -> bool:
        return now >= feed.observed_at and now - feed.observed_at <= timedelta(
            seconds=feed.heartbeat_seconds + 1800
        )

    @staticmethod
    def _median(values: tuple[Decimal, ...]) -> Decimal:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / Decimal(2)

    def _status(self, deviation: Decimal) -> HealthStatus:
        if deviation >= self.rules.oracle_critical_deviation:
            return HealthStatus.CRITICAL
        if deviation >= self.rules.oracle_warning_deviation:
            return HealthStatus.WARNING
        return HealthStatus.HEALTHY

    @staticmethod
    def _worst(statuses: tuple[HealthStatus, ...]) -> HealthStatus:
        order = {
            HealthStatus.HEALTHY: 0,
            HealthStatus.WARNING: 1,
            HealthStatus.UNKNOWN: 2,
            HealthStatus.CRITICAL: 3,
        }
        return max(statuses, key=order.__getitem__)


__all__ = ["ConsensusResult", "OracleConsensus", "OracleFeedObservation"]
