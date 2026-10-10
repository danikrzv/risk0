import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.contracts.models import ConfidenceLevel, ProbabilityEstimate, ProbabilityMethod
from app.adapters.ethereum import EthereumProviderError, UnsupportedFeedError
from app.history.estimator import HistoricalProbabilityService, RollingWindowEstimator
from app.history.normalization import (
    DataGap,
    NormalizedHistoryCache,
    NormalizedSeries,
    Observation,
    normalize_hourly,
    normalize_period,
)
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


FIXTURES = Path(__file__).parents[1] / "fixtures" / "rpc"
START = datetime(2026, 1, 1, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 4, 1, tzinfo=UTC)


def test_normalization_forward_fills_short_holes_and_preserves_long_gaps():
    fixture = json.loads((FIXTURES / "uniswap-observations.json").read_text())
    observations = tuple(
        Observation(
            timestamp=datetime.fromisoformat(item["timestamp"]),
            value=Decimal(item["value"]),
            block_number=item["block_number"],
            log_index=item["log_index"],
        )
        for item in fixture["observations"]
    )

    series = normalize_hourly(observations, maximum_gap=timedelta(hours=6))

    assert [item.timestamp for item in series.observations] == [
        START,
        START + timedelta(hours=1),
        START + timedelta(hours=2),
        START + timedelta(hours=10),
    ]
    assert series.observations[1].value == Decimal("100")
    assert series.observations[1].is_forward_filled is True
    assert series.gaps == (
        DataGap(
            start=START + timedelta(hours=3),
            end=START + timedelta(hours=9),
            missing_observations=7,
        ),
    )


def test_six_hour_separation_is_fillable_but_seven_hours_is_a_gap():
    at_zero = observation(0, "100")
    at_six = observation(6, "106")
    at_seven = observation(7, "107")

    six_hours = normalize_hourly((at_zero, at_six), timedelta(hours=6))
    seven_hours = normalize_hourly((at_zero, at_seven), timedelta(hours=6))

    assert len(six_hours.observations) == 7
    assert six_hours.gaps == ()
    assert len(seven_hours.observations) == 2
    assert seven_hours.gaps == (
        DataGap(START + timedelta(hours=1), START + timedelta(hours=6), 6),
    )


def test_request_bounds_normalize_to_utc_hours_with_edge_gaps():
    plus_two = timezone(timedelta(hours=2))
    requested_start = datetime(2026, 1, 1, 2, 30, tzinfo=plus_two)
    requested_end = datetime(2026, 1, 1, 12, 45, tzinfo=plus_two)
    normalized_start, normalized_end = normalize_period(requested_start, requested_end)

    series = normalize_hourly(
        (observation(3, "103"), observation(8, "108")),
        timedelta(hours=6),
        start=requested_start,
        end=requested_end,
    )

    assert normalized_start == START + timedelta(hours=1)
    assert normalized_end == START + timedelta(hours=10)
    assert series.start == normalized_start
    assert series.end == normalized_end
    assert series.gaps == (
        DataGap(START + timedelta(hours=1), START + timedelta(hours=2), 2),
        DataGap(START + timedelta(hours=9), START + timedelta(hours=10), 2),
    )

def test_normalized_history_cache_refreshes_daily_and_retains_365_days(tmp_path):
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    series = fixed_series()
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)

    fresh = cache.get(
        "ETH/USD", series.start, series.end, now=RETRIEVED_AT + timedelta(hours=23)
    )
    stale = cache.get(
        "ETH/USD", series.start, series.end, now=RETRIEVED_AT + timedelta(days=2)
    )
    expired = cache.get(
        "ETH/USD", series.start, series.end, now=RETRIEVED_AT + timedelta(days=365)
    )

    assert fresh is not None and fresh.freshness is Freshness.FRESH
    assert stale is not None and stale.freshness is Freshness.STALE_USABLE
    assert expired is None
    assert stale.series.observations[1080].value == Decimal("0.89")


def test_cache_identity_rejects_different_normalized_periods(tmp_path):
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    series = fixed_series()
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)

    same_period_with_offset = cache.get(
        "ETH/USD",
        series.start.astimezone(timezone(timedelta(hours=2))),
        series.end.astimezone(timezone(timedelta(hours=2))),
        now=RETRIEVED_AT,
    )
    different_period = cache.get(
        "ETH/USD",
        series.start + timedelta(hours=1),
        series.end,
        now=RETRIEVED_AT,
    )

    assert same_period_with_offset is not None
    assert different_period is None


def test_normalized_history_cache_preserves_observation_provenance(tmp_path):
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    series = fixed_series()
    sourced = replace(
        series,
        observations=tuple(
            replace(item, source=series.source) for item in series.observations
        ),
    )
    cache.set("ETH/USD", sourced, fetched_at=RETRIEVED_AT)

    cached = cache.get(
        "ETH/USD", sourced.start, sourced.end, now=RETRIEVED_AT
    )

    assert cached is not None
    assert {item.source for item in cached.series.observations} == {series.source}


def test_fixed_90_day_estimator_returns_exact_depeg_window_counts_and_dates():
    estimate = RollingWindowEstimator().estimate_with_evidence(
        fixed_series(),
        event=lambda window: any(item.value <= Decimal("0.90") for item in window),
    )

    assert estimate.probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert estimate.observation_count == 2_160
    assert estimate.expected_observation_count == 2_160
    assert estimate.window_count == 1_441
    assert estimate.event_window_count == 1_080
    assert estimate.probability.central == Decimal(1080) / Decimal(1441)
    assert estimate.probability.confidence is ConfidenceLevel.HIGH
    assert len(estimate.sample_dates) == 1_080
    assert estimate.sample_dates[0] == START + timedelta(hours=361)
    assert estimate.sample_dates[-1] == START + timedelta(hours=1440)
    assert estimate.gaps == ()


def test_fixed_90_day_estimator_counts_collateral_shock_windows_exactly():
    estimate = RollingWindowEstimator().estimate_with_evidence(
        fixed_series(),
        event=lambda window: any(item.value <= Decimal("0.85") for item in window),
    )

    assert estimate.window_count == 1_441
    assert estimate.event_window_count == 360
    assert estimate.probability.central == Decimal(360) / Decimal(1441)
    assert estimate.sample_dates[0] == START + timedelta(hours=1081)
    assert estimate.sample_dates[-1] == START + timedelta(hours=1440)


def test_more_than_five_percent_missing_degrades_confidence_and_excludes_gapped_windows():
    complete = fixed_series()
    observations = complete.observations[:900] + complete.observations[1009:]
    gap = DataGap(
        start=START + timedelta(hours=900),
        end=START + timedelta(hours=1008),
        missing_observations=109,
    )
    incomplete = NormalizedSeries(
        observations=observations,
        gaps=(gap,),
        start=complete.start,
        end=complete.end,
        source=complete.source,
        retrieved_at=complete.retrieved_at,
        freshness=complete.freshness,
    )

    estimate = RollingWindowEstimator().estimate_with_evidence(
        incomplete,
        event=lambda window: any(item.value <= Decimal("0.90") for item in window),
    )

    assert estimate.observation_count == 2_051
    assert estimate.expected_observation_count == 2_160
    assert estimate.missing_observation_ratio == Decimal(109) / Decimal(2160)
    assert estimate.window_count == 613
    assert estimate.event_window_count == 432
    assert estimate.probability.central == Decimal(432) / Decimal(613)
    assert estimate.probability.confidence is ConfidenceLevel.LOW
    assert estimate.gaps == (gap,)


def test_forward_filled_values_count_as_missing_for_confidence():
    complete = fixed_series()
    observations = tuple(
        replace(item, is_forward_filled=index < 109)
        for index, item in enumerate(complete.observations)
    )
    forward_filled = replace(complete, observations=observations)

    estimate = RollingWindowEstimator().estimate_with_evidence(
        forward_filled,
        event=lambda window: False,
    )

    assert estimate.observation_count == 2_051
    assert estimate.expected_observation_count == 2_160
    assert estimate.missing_observation_ratio == Decimal(109) / Decimal(2160)
    assert estimate.window_count == 1_441
    assert estimate.probability.confidence is ConfidenceLevel.LOW


def test_insufficient_history_returns_explicit_unavailable_evidence():
    short = fixed_series(hours=719)

    estimate = RollingWindowEstimator().estimate_with_evidence(
        short, event=lambda window: False
    )

    assert estimate.window_count == 0
    assert estimate.event_window_count == 0
    assert estimate.probability.method is ProbabilityMethod.UNAVAILABLE
    assert estimate.probability.confidence is ConfidenceLevel.UNAVAILABLE
    assert estimate.probability.central is None
    assert "insufficient history" in estimate.probability.explanation


def test_unsupported_feed_returns_explicit_unavailable_evidence(tmp_path):
    class UnsupportedClient:
        def get_chainlink_observations(self, feed, start, end):
            raise UnsupportedFeedError(f"unsupported Chainlink feed: {feed}")

    service = HistoricalProbabilityService(
        UnsupportedClient(),
        NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite")),
    )

    estimate = service.estimate(
        "NOT/A/FEED",
        START,
        START + timedelta(days=90) - timedelta(hours=1),
        event=lambda window: False,
        now=RETRIEVED_AT,
    )

    assert estimate.probability.method is ProbabilityMethod.UNAVAILABLE
    assert estimate.probability.confidence is ConfidenceLevel.UNAVAILABLE
    assert estimate.probability.central is None
    assert "unsupported Chainlink feed" in estimate.probability.explanation


def test_estimate_returns_shared_probability_contract():
    probability = RollingWindowEstimator().estimate(
        fixed_series(),
        event=lambda window: any(item.value <= Decimal("0.85") for item in window),
    )

    assert isinstance(probability, ProbabilityEstimate)
    assert probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert probability.central == Decimal(360) / Decimal(1441)


def test_fresh_period_cache_hit_skips_provider(tmp_path):
    class FailingClient:
        def get_chainlink_observations(self, feed, start, end):
            raise AssertionError("fresh cache must avoid provider")

    series = fixed_series()
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)
    service = HistoricalProbabilityService(FailingClient(), cache)

    estimate = service.estimate(
        "ETH/USD",
        series.start,
        series.end,
        event=lambda window: False,
        now=RETRIEVED_AT + timedelta(hours=1),
    )

    assert estimate.expected_observation_count == 2_160
    assert estimate.probability.freshness == "fresh"


def test_empty_refresh_uses_usable_stale_period_history(tmp_path):
    class EmptyClient:
        def get_chainlink_observations(self, feed, start, end):
            return ()

    series = fixed_series()
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)
    service = HistoricalProbabilityService(EmptyClient(), cache)

    estimate = service.estimate(
        "ETH/USD",
        series.start,
        series.end,
        event=lambda window: False,
        now=RETRIEVED_AT + timedelta(days=2),
    )

    assert estimate.expected_observation_count == 2_160
    assert estimate.probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert estimate.probability.freshness == "stale_usable"


def test_materially_partial_refresh_uses_usable_stale_period_history(tmp_path):
    class PartialClient:
        def get_chainlink_observations(self, feed, start, end):
            return fixed_series(hours=100).observations

    series = fixed_series()
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)
    service = HistoricalProbabilityService(PartialClient(), cache)

    estimate = service.estimate(
        "ETH/USD",
        series.start,
        series.end,
        event=lambda window: False,
        now=RETRIEVED_AT + timedelta(days=2),
    )

    assert estimate.expected_observation_count == 2_160
    assert estimate.probability.freshness == "stale_usable"


def test_out_of_period_refresh_uses_usable_stale_period_history(tmp_path):
    class OutOfPeriodClient:
        def get_chainlink_observations(self, feed, start, end):
            return (observation(-1, "1"),)

    series = fixed_series()
    cache = NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    cache.set("ETH/USD", series, fetched_at=RETRIEVED_AT)
    service = HistoricalProbabilityService(OutOfPeriodClient(), cache)

    estimate = service.estimate(
        "ETH/USD",
        series.start,
        series.end,
        event=lambda window: False,
        now=RETRIEVED_AT + timedelta(days=2),
    )

    assert estimate.expected_observation_count == 2_160
    assert estimate.probability.freshness == "stale_usable"


def test_phase_discovery_failure_returns_sanitized_unavailable_gap(tmp_path):
    class PhaseFailureClient:
        def get_chainlink_observations(self, feed, start, end):
            raise EthereumProviderError("https://rpc.invalid/private-token")

    service = HistoricalProbabilityService(
        PhaseFailureClient(),
        NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite")),
    )
    end = START + timedelta(days=90) - timedelta(hours=1)

    estimate = service.estimate(
        "ETH/USD", START, end, event=lambda window: False, now=RETRIEVED_AT
    )

    assert estimate.probability.method is ProbabilityMethod.UNAVAILABLE
    assert estimate.gaps == (DataGap(START, end, 2_160),)
    assert "private-token" not in estimate.probability.explanation


def fixed_series(hours: int = 2_160) -> NormalizedSeries:
    observations = []
    for index in range(hours):
        value = Decimal("1")
        if index == 1_080:
            value = Decimal("0.89")
        elif index == 1_800:
            value = Decimal("0.80")
        observations.append(
            Observation(
                timestamp=START + timedelta(hours=index),
                value=value,
                block_number=index,
                log_index=0,
            )
        )
    return NormalizedSeries(
        observations=tuple(observations),
        gaps=(),
        start=START,
        end=START + timedelta(hours=hours - 1),
        source="fixture:90-day-hourly",
        retrieved_at=RETRIEVED_AT,
        freshness=Freshness.FRESH,
    )


def observation(hour: int, value: str) -> Observation:
    return Observation(
        timestamp=START + timedelta(hours=hour),
        value=Decimal(value),
        block_number=hour,
        log_index=0,
    )
