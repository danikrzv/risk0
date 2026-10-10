from collections.abc import Callable, Sequence
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
from app.infrastructure.freshness import Freshness


EventPredicate = Callable[[Sequence[Observation]], bool]
MISSING_CONFIDENCE_THRESHOLD = Decimal("0.05")


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


class HistoricalProbabilityService:
    def __init__(
        self,
        client: ChainlinkHistoryClient,
        cache: NormalizedHistoryCache,
    ) -> None:
        self._client = client
        self._cache = cache
        self._estimator = RollingWindowEstimator()

    def estimate(
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
        normalized = normalize_hourly(
            observations,
            maximum_gap=timedelta(hours=6),
            start=normalized_start,
            end=normalized_end,
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
        window_count = 0
        event_dates: list[datetime] = []
        candidate_count = expected_count - horizon_observations + 1
        for start_index in range(candidate_count):
            window_start = series.start + start_index * HOUR
            window = tuple(
                by_timestamp.get(window_start + offset * HOUR)
                for offset in range(horizon_observations)
            )
            if any(item is None for item in window):
                continue
            complete_window = tuple(item for item in window if item is not None)
            window_count += 1
            if event(complete_window):
                event_dates.append(window_start)

        if window_count == 0:
            return _unavailable(
                series,
                observation_count,
                expected_count,
                missing_ratio,
                threshold,
                "insufficient complete history for one 30-day window",
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


def _materially_partial(series: NormalizedSeries) -> bool:
    expected = int((series.end - series.start) / HOUR) + 1
    observed = sum(not item.is_forward_filled for item in series.observations)
    return Decimal(expected - observed) / Decimal(expected) > MISSING_CONFIDENCE_THRESHOLD
