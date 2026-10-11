from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.adapters.ethereum import EthereumProviderError, UnsupportedFeedError
from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
)
from app.history.normalization import (
    DataGap,
    HOUR,
    NormalizedHistoryCache,
    NormalizedSeries,
    Observation,
    normalize_hourly,
    normalize_period,
)
from app.history.staleness import (
    STALENESS_ALLOWANCE_SECONDS,
    StalenessEvent,
    staleness_series,
)
from app.infrastructure.freshness import Freshness


EventPredicate = Callable[[Sequence[Observation]], bool]
# The history is readable but too short (e.g. a feed launched under 30 days ago).
INSUFFICIENT_HISTORY_REASON = "insufficient complete history for one 30-day window"
MISSING_CONFIDENCE_THRESHOLD = Decimal("0.05")
# Hourly normalization forward-fills a Chainlink answer across a gap up to this
# long by default. A feed's answer stands until its next round, and the feed
# publishes a round whenever the answer moves past its deviation threshold, so
# for a feed with a known heartbeat the limit is raised to heartbeat + 30
# minutes: a 24-hour-heartbeat feed (USDC/USD) is then a complete hourly series
# instead of one with a gap after every quiet day.
DEFAULT_FORWARD_FILL = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class EmpiricalProbabilityEvidence:
    probability: ProbabilityEstimate
    observation_count: int
    expected_observation_count: int
    window_count: int
    event_window_count: int
    sample_dates: tuple[datetime, ...]
    gaps: tuple[DataGap, ...]
    missing_observation_ratio: Decimal
    observation_period: tuple[datetime, datetime]
    sampling_interval: timedelta
    threshold_definition: str


class ChainlinkHistoryClient(Protocol):
    def get_chainlink_observations(
        self,
        feed: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Observation, ...]: ...


class FeedUpdatesClient(Protocol):
    def get_feed_updates(
        self,
        address: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Observation, ...]: ...


class HistoricalProbabilityService:
    """Empirical 30-day probabilities from Chainlink history.

    `heartbeats` (feed id -> seconds) raises a feed's forward-fill limit to its
    heartbeat + 30 minutes; `feed_notes` (feed id -> text) is appended to the
    explanation of every estimate from that feed, for caveats such as a proxy
    feed standing in for an asset.
    """

    def __init__(
        self,
        client: ChainlinkHistoryClient,
        cache: NormalizedHistoryCache,
        *,
        heartbeats: Mapping[str, int] | None = None,
        feed_notes: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._estimator = RollingWindowEstimator()
        self._heartbeats = dict(heartbeats or {})
        self._notes = dict(feed_notes or {})

    def estimate(
        self,
        feed: str,
        start: datetime,
        end: datetime,
        event: EventPredicate,
        *,
        now: datetime,
    ) -> EmpiricalProbabilityEvidence:
        evidence = self._estimate(feed, start, end, event, now=now)
        note = self._notes.get(feed)
        if not note:
            return evidence
        probability = evidence.probability.model_copy(
            update={"explanation": f"{evidence.probability.explanation} {note}"}
        )
        return replace(evidence, probability=probability)

    def estimate_staleness(
        self,
        feed_address: str,
        heartbeat_seconds: int,
        start: datetime,
        end: datetime,
        *,
        now: datetime,
        label: str | None = None,
        require_phase: bool = True,
    ) -> EmpiricalProbabilityEvidence:
        """Share of 30-day windows in which the feed was stale beyond heartbeat + 30 minutes.

        Uses exact update timestamps from round reads (`get_feed_updates`), never
        the hourly price series, so a gap only minutes past the threshold is still
        seen; see `app.history.staleness`. `require_phase=False` reads a
        third-party AggregatorV3 feed whose round ids carry no Chainlink phase.
        """
        name = label or feed_address.lower()
        event = StalenessEvent(heartbeat_seconds)
        normalized_start, normalized_end = normalize_period(start, end)

        def unavailable(reason: str) -> EmpiricalProbabilityEvidence:
            return _unavailable_period(
                name, normalized_start, normalized_end, now, event, reason
            )

        reader = getattr(self._client, "get_feed_updates", None)
        if not callable(reader):
            return unavailable("Feed update history is not available from this client")
        try:
            if require_phase:
                updates = reader(feed_address, normalized_start, normalized_end)
            else:
                updates = reader(
                    feed_address, normalized_start, normalized_end, require_phase=False
                )
        except UnsupportedFeedError:
            kind = "a Chainlink aggregator" if require_phase else "a feed"
            return unavailable(f"{name} is not {kind} with readable rounds")
        except EthereumProviderError:
            return unavailable("Ethereum provider history is unavailable")
        series = staleness_series(
            updates,
            heartbeat_seconds,
            normalized_start,
            normalized_end,
            as_of=min(now, normalized_end + HOUR),
            source=f"chainlink:{name}",
            retrieved_at=now,
        )
        if series is None:
            return unavailable("Ethereum provider returned no feed updates")
        evidence = self._estimator.estimate_with_evidence(series, event)
        probability = evidence.probability
        if probability.central is not None:
            days = (normalized_end - normalized_start).days
            probability = probability.model_copy(
                update={
                    "explanation": (
                        f"{evidence.event_window_count} of {evidence.window_count} overlapping "
                        f"30-day windows (hourly starts over {days} days) contained a gap "
                        f"between consecutive {name} updates longer than its "
                        f"{heartbeat_seconds} s heartbeat + {STALENESS_ALLOWANCE_SECONDS} s, "
                        f"from exact round timestamps ({len(updates)} rounds read with "
                        "getRoundData)."
                    )
                }
            )
        return replace(evidence, probability=probability)

    def _estimate(
        self,
        feed: str,
        start: datetime,
        end: datetime,
        event: EventPredicate,
        *,
        now: datetime,
    ) -> EmpiricalProbabilityEvidence:
        normalized_start, normalized_end = normalize_period(start, end)
        cached = self._cache.get(feed, normalized_start, normalized_end, now)
        if cached is not None and cached.freshness is Freshness.FRESH:
            return self._estimator.estimate_with_evidence(cached.series, event)

        try:
            observations = self._client.get_chainlink_observations(
                feed,
                normalized_start,
                normalized_end,
            )
        except UnsupportedFeedError:
            return _unavailable_period(
                feed,
                normalized_start,
                normalized_end,
                now,
                event,
                f"unsupported Chainlink feed: {feed}",
            )
        except EthereumProviderError:
            if cached is not None:
                stale_series = replace(cached.series, freshness=cached.freshness)
                return self._estimator.estimate_with_evidence(stale_series, event)
            return _unavailable_period(
                feed,
                normalized_start,
                normalized_end,
                now,
                event,
                "Ethereum provider history is unavailable",
            )

        observations = tuple(
            observation
            for observation in observations
            if normalized_start
            <= observation.timestamp.astimezone(UTC)
            <= normalized_end
        )
        if not observations:
            if cached is not None:
                stale_series = replace(cached.series, freshness=cached.freshness)
                return self._estimator.estimate_with_evidence(stale_series, event)
            return _unavailable_period(
                feed,
                normalized_start,
                normalized_end,
                now,
                event,
                "Ethereum provider returned no history",
            )
        heartbeat = self._heartbeats.get(feed)
        maximum_gap = (
            max(DEFAULT_FORWARD_FILL, timedelta(seconds=heartbeat + STALENESS_ALLOWANCE_SECONDS))
            if heartbeat
            else DEFAULT_FORWARD_FILL
        )
        normalized = normalize_hourly(
            observations,
            maximum_gap=maximum_gap,
            start=normalized_start,
            end=normalized_end,
        )
        if heartbeat:
            # A Chainlink answer is the feed's value until its next round, which
            # comes at the latest one heartbeat later: an hour carried forward
            # within heartbeat + 30 minutes is an observation, not an imputation.
            normalized = replace(
                normalized,
                observations=tuple(
                    replace(item, is_forward_filled=False) for item in normalized.observations
                ),
            )
        series = replace(
            normalized,
            retrieved_at=now,
        )
        if cached is not None and _materially_partial(series):
            stale_series = replace(cached.series, freshness=cached.freshness)
            return self._estimator.estimate_with_evidence(stale_series, event)
        self._cache.set(feed, series, fetched_at=now)
        return self._estimator.estimate_with_evidence(series, event)


class RollingWindowEstimator:
    def estimate(
        self,
        series: NormalizedSeries,
        event: EventPredicate,
        horizon_days: int = 30,
    ) -> ProbabilityEstimate:
        return self.estimate_with_evidence(series, event, horizon_days).probability

    def estimate_with_evidence(
        self,
        series: NormalizedSeries,
        event: EventPredicate,
        horizon_days: int = 30,
    ) -> EmpiricalProbabilityEvidence:
        if horizon_days != 30:
            raise ValueError("empirical estimates require the fixed 30-day horizon")

        expected_count = int((series.end - series.start) / HOUR) + 1
        observation_count = sum(
            not observation.is_forward_filled
            for observation in series.observations
        )
        missing_ratio = Decimal(expected_count - observation_count) / Decimal(
            expected_count
        )
        threshold = getattr(event, "description", getattr(event, "__name__", "event"))
        horizon_observations = horizon_days * 24
        if expected_count < horizon_observations:
            return _unavailable(
                series,
                observation_count,
                expected_count,
                missing_ratio,
                threshold,
                "insufficient history for one 30-day window",
            )

        by_timestamp = {item.timestamp: item for item in series.observations}
        hourly = [
            by_timestamp.get(series.start + index * HOUR) for index in range(expected_count)
        ]
        missing_before = _prefix_counts(item is None for item in hourly)
        # Predicates with per-hour `flags` (a window has the event when any hour is
        # flagged) are counted with prefix sums instead of one call per window.
        flags = getattr(event, "flags", None)
        flagged_before = _prefix_counts(_hour_flags(flags, hourly)) if callable(flags) else None
        window_count = 0
        event_dates: list[datetime] = []
        candidate_count = expected_count - horizon_observations + 1
        for start_index in range(candidate_count):
            stop = start_index + horizon_observations
            if missing_before[stop] - missing_before[start_index]:
                continue
            window_count += 1
            if flagged_before is not None:
                happened = flagged_before[stop] - flagged_before[start_index] > 0
            else:
                happened = event(tuple(hourly[start_index:stop]))
            if happened:
                event_dates.append(series.start + start_index * HOUR)

        if window_count == 0:
            return _unavailable(
                series,
                observation_count,
                expected_count,
                missing_ratio,
                threshold,
                INSUFFICIENT_HISTORY_REASON,
            )

        event_count = len(event_dates)
        central = Decimal(event_count) / Decimal(window_count)
        low, high = _wilson_interval(event_count, window_count)
        confidence = (
            ConfidenceLevel.LOW
            if missing_ratio > MISSING_CONFIDENCE_THRESHOLD
            else ConfidenceLevel.HIGH
        )
        probability = ProbabilityEstimate(
            method=ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
            low=low,
            central=central,
            high=high,
            confidence=confidence,
            horizon_days=30,
            source=series.source,
            retrieved_at=series.retrieved_at,
            freshness=FreshnessStatus(series.freshness.value),
            explanation=(
                f"{event_count} of {window_count} complete overlapping 30-day "
                "hourly windows contained the event."
            ),
        )
        return EmpiricalProbabilityEvidence(
            probability=probability,
            observation_count=observation_count,
            expected_observation_count=expected_count,
            window_count=window_count,
            event_window_count=event_count,
            sample_dates=tuple(event_dates),
            gaps=series.gaps,
            missing_observation_ratio=missing_ratio,
            observation_period=(series.start, series.end),
            sampling_interval=HOUR,
            threshold_definition=str(threshold),
        )


def _unavailable(
    series: NormalizedSeries,
    observation_count: int,
    expected_count: int,
    missing_ratio: Decimal,
    threshold: object,
    reason: str,
) -> EmpiricalProbabilityEvidence:
    probability = ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE,
        low=None,
        central=None,
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE,
        horizon_days=30,
        source=series.source,
        retrieved_at=series.retrieved_at,
        freshness=FreshnessStatus(series.freshness.value),
        explanation=reason,
    )
    return EmpiricalProbabilityEvidence(
        probability=probability,
        observation_count=observation_count,
        expected_observation_count=expected_count,
        window_count=0,
        event_window_count=0,
        sample_dates=(),
        gaps=series.gaps,
        missing_observation_ratio=missing_ratio,
        observation_period=(series.start, series.end),
        sampling_interval=HOUR,
        threshold_definition=str(threshold),
    )


def _unavailable_period(
    feed: str,
    start: datetime,
    end: datetime,
    retrieved_at: datetime,
    event: EventPredicate,
    reason: str,
) -> EmpiricalProbabilityEvidence:
    expected_count = max(0, int((end - start) / HOUR) + 1)
    probability = ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE,
        low=None,
        central=None,
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE,
        horizon_days=30,
        source=f"chainlink:{feed}",
        retrieved_at=retrieved_at,
        freshness=FreshnessStatus.EXPIRED,
        explanation=reason,
    )
    gaps = (
        DataGap(start=start, end=end, missing_observations=expected_count),
    ) if expected_count else ()
    return EmpiricalProbabilityEvidence(
        probability=probability,
        observation_count=0,
        expected_observation_count=expected_count,
        window_count=0,
        event_window_count=0,
        sample_dates=(),
        gaps=gaps,
        missing_observation_ratio=Decimal(1) if expected_count else Decimal(0),
        observation_period=(start, end),
        sampling_interval=HOUR,
        threshold_definition=str(
            getattr(event, "description", getattr(event, "__name__", "event"))
        ),
    )


def _wilson_interval(events: int, windows: int) -> tuple[Decimal, Decimal]:
    proportion = Decimal(events) / Decimal(windows)
    z_score = Decimal("1.96")
    z_squared = z_score * z_score
    sample_size = Decimal(windows)
    denominator = Decimal(1) + z_squared / sample_size
    center = (proportion + z_squared / (Decimal(2) * sample_size)) / denominator
    variance = (
        proportion * (Decimal(1) - proportion) / sample_size
        + z_squared / (Decimal(4) * sample_size * sample_size)
    )
    margin = z_score * variance.sqrt() / denominator
    return max(Decimal(0), center - margin), min(Decimal(1), center + margin)


def _prefix_counts(values) -> list[int]:
    counts = [0]
    for value in values:
        counts.append(counts[-1] + (1 if value else 0))
    return counts


def _hour_flags(flags, hourly: Sequence[Observation | None]) -> list[bool]:
    present = [item for item in hourly if item is not None]
    computed = iter(flags(present))
    return [False if item is None else bool(next(computed)) for item in hourly]


def _materially_partial(series: NormalizedSeries) -> bool:
    expected = int((series.end - series.start) / HOUR) + 1
    observed = sum(not item.is_forward_filled for item in series.observations)
    return Decimal(expected - observed) / Decimal(expected) > MISSING_CONFIDENCE_THRESHOLD
