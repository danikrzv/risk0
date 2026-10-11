from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.ethereum import EthereumProviderError, UnsupportedFeedError
from app.contracts.models import ConfidenceLevel, ProbabilityMethod
from app.history.estimator import HistoricalProbabilityService, RollingWindowEstimator
from app.history.normalization import (
    NormalizedHistoryCache,
    NormalizedSeries,
    Observation,
    normalize_hourly,
)
from app.history.staleness import (
    MINIMUM_GAPS_FOR_INFERENCE,
    StalenessEvent,
    infer_heartbeat,
    staleness_series,
)
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


START = datetime(2026, 1, 1, tzinfo=UTC)
FEED = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419"


def update(seconds: int, value: str = "1", round_id: int | None = None) -> Observation:
    return Observation(
        timestamp=START + timedelta(seconds=seconds),
        value=Decimal(value),
        block_number=0,
        log_index=0,
        round_id=round_id if round_id is not None else seconds,
    )


def regular(interval: int, until: int, *, skip: tuple[tuple[int, int], ...] = ()) -> list[Observation]:
    """Updates every `interval` seconds from 0 to `until`, minus [a, b) holes in `skip`."""
    times = [t for t in range(0, until + 1, interval) if not any(a <= t < b for a, b in skip)]
    return [update(t) for t in times]


# Heartbeat inference


def test_hourly_heartbeat_with_deviation_updates_and_block_delay_is_3600():
    times = []
    t = 0
    for index in range(400):
        t += 3612 if index % 3 == 0 else 900  # heartbeat updates land a block late
        times.append(START + timedelta(seconds=t))

    assert infer_heartbeat(times) == 3600


def test_daily_heartbeat_is_86400_and_one_outage_is_ignored_by_the_percentile():
    times = [START + timedelta(days=day, seconds=day % 7) for day in range(200)]
    times.append(times[-1] + timedelta(days=5))  # a single 5-day outage

    assert infer_heartbeat(times) == 86400


def test_too_few_gaps_leave_the_heartbeat_unknown():
    times = [START + timedelta(hours=hour) for hour in range(MINIMUM_GAPS_FOR_INFERENCE)]

    assert infer_heartbeat(times) is None
    assert infer_heartbeat(times + [START + timedelta(hours=100)]) is not None


# Staleness series


def test_hours_overlapping_a_gap_beyond_heartbeat_plus_thirty_minutes_are_flagged():
    # Hourly feed; one gap from 10:00 to 15:00 (5 h). Stale from 11:30 to 15:00.
    updates = regular(3600, 24 * 3600, skip=((11 * 3600, 15 * 3600),))

    series = staleness_series(
        updates, 3600, START, START + timedelta(hours=23), as_of=START + timedelta(hours=24),
        source="chainlink:test", retrieved_at=START,
    )

    assert series is not None
    flagged = [item.timestamp.hour for item in series.observations if item.value == 1]
    assert flagged == [11, 12, 13, 14]
    assert len(series.observations) == 24
    assert series.gaps == ()


@pytest.mark.parametrize(("gap", "stale"), [(3600 + 1800, False), (3600 + 1801, True)])
def test_a_gap_must_exceed_heartbeat_plus_thirty_minutes(gap, stale):
    updates = [update(0), update(gap), update(gap + 3600)]

    series = staleness_series(
        updates, 3600, START, START + timedelta(hours=3), as_of=START + timedelta(seconds=gap + 3600),
        source="s", retrieved_at=START,
    )

    assert series is not None
    assert any(item.value == 1 for item in series.observations) is stale


def test_a_feed_silent_until_as_of_is_stale_at_the_end():
    series = staleness_series(
        [update(0)], 3600, START, START + timedelta(hours=5), as_of=START + timedelta(hours=6),
        source="s", retrieved_at=START,
    )

    assert series is not None
    assert [item.value for item in series.observations] == [Decimal(v) for v in (0, 1, 1, 1, 1, 1)]


def test_hours_before_the_first_update_are_missing_not_healthy():
    series = staleness_series(
        [update(5 * 3600 + 60), update(6 * 3600)], 3600, START, START + timedelta(hours=6),
        as_of=START + timedelta(hours=6), source="s", retrieved_at=START,
    )

    assert series is not None
    assert series.observations[0].timestamp == START + timedelta(hours=6)
    assert series.gaps[0].start == START and series.gaps[0].missing_observations == 6


def test_flags_fast_path_matches_calling_the_event_per_window():
    hours = 24 * 75
    values = [Decimal(1) if hour in (100, 900, 901, 1700) else Decimal(0) for hour in range(hours)]
    series = NormalizedSeries(
        observations=tuple(
            Observation(START + timedelta(hours=hour), value, 0, 0) for hour, value in enumerate(values)
        ),
        gaps=(),
        start=START,
        end=START + timedelta(hours=hours - 1),
        source="s",
        retrieved_at=START,
        freshness=Freshness.FRESH,
    )
    event = StalenessEvent(3600)

    fast = RollingWindowEstimator().estimate_with_evidence(series, event)
    slow = RollingWindowEstimator().estimate_with_evidence(series, lambda window: event(window))

    assert fast.window_count == slow.window_count
    assert fast.event_window_count == slow.event_window_count > 0
    assert fast.sample_dates == slow.sample_dates


# Service


class FakeUpdates:
    def __init__(self, updates=(), error=None):
        self.updates = tuple(updates)
        self.error = error
        self.calls = []

    def get_chainlink_observations(self, feed, start, end):
        raise AssertionError("staleness must use exact feed updates")

    def get_feed_updates(self, address, start, end):
        self.calls.append(address)
        if self.error is not None:
            raise self.error
        return self.updates


def service(client, tmp_path) -> HistoricalProbabilityService:
    return HistoricalProbabilityService(
        client, NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite"))
    )


def test_staleness_probability_counts_windows_with_a_gap_beyond_the_threshold(tmp_path):
    days = 90
    hole = (40 * 86400, 40 * 86400 + 2 * 3600)  # one 2-hour (+1 h) silence on day 40
    client = FakeUpdates(regular(1800, days * 86400, skip=(hole,)))
    end = START + timedelta(days=days)

    evidence = service(client, tmp_path).estimate_staleness(
        FEED, 3600, START, end, now=end, label="ETH / USD"
    )

    probability = evidence.probability
    assert probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert probability.confidence is ConfidenceLevel.HIGH
    assert evidence.window_count == (days * 24 + 1) - 30 * 24 + 1
    # Windows overlapping the stale hours (day 40, 01:00-02:00 after the threshold).
    assert 0 < evidence.event_window_count <= 30 * 24 + 2
    assert probability.central == Decimal(evidence.event_window_count) / Decimal(evidence.window_count)
    assert probability.low <= probability.central <= probability.high
    assert "3600 s heartbeat + 1800 s" in probability.explanation
    assert "ETH / USD" in probability.explanation
    assert client.calls == [FEED]


def test_a_feed_that_never_goes_stale_has_zero_central_and_a_wilson_upper_bound(tmp_path):
    client = FakeUpdates(regular(3600, 60 * 86400))
    end = START + timedelta(days=60)

    probability = service(client, tmp_path).estimate_staleness(FEED, 3600, START, end, now=end).probability

    assert probability.central == 0
    assert probability.low == 0 and probability.high > 0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (EthereumProviderError("https://rpc.example.invalid/secret"), "provider history is unavailable"),
        (UnsupportedFeedError("x"), "not a Chainlink aggregator"),
    ],
)
def test_staleness_history_failures_are_explicitly_unavailable(tmp_path, error, reason):
    end = START + timedelta(days=60)

    probability = service(FakeUpdates(error=error), tmp_path).estimate_staleness(
        FEED, 3600, START, end, now=end
    ).probability

    assert probability.method is ProbabilityMethod.UNAVAILABLE
    assert probability.central is None
    assert reason in probability.explanation
    assert "secret" not in probability.explanation


def test_heartbeat_forward_fill_completes_a_daily_feed_series(tmp_path):
    class Daily:
        def get_chainlink_observations(self, feed, start, end):
            return tuple(update(day * 86400 + 60, "1.0", round_id=day) for day in range(70))

    end = START + timedelta(days=69)
    default = service(Daily(), tmp_path).estimate("USDC/USD", START, end, lambda w: False, now=end)
    daily = HistoricalProbabilityService(
        Daily(),
        NormalizedHistoryCache(SQLiteCache(tmp_path / "other.sqlite")),
        heartbeats={"USDC/USD": 86400},
        feed_notes={"USDC/USD": "A feed note."},
    ).estimate("USDC/USD", START, end, lambda w: False, now=end)

    assert default.probability.central is None  # 6-hour fill leaves a gap every day
    assert daily.probability.central == 0
    assert daily.missing_observation_ratio < Decimal("0.05")
    assert daily.probability.explanation.endswith("A feed note.")


def test_round_ids_keep_same_second_answers_distinct_in_normalization():
    first = update(0, "1", round_id=1)
    second = update(0, "2", round_id=2)

    series = normalize_hourly([first, second], maximum_gap=timedelta(hours=1))

    assert series.observations[0].value == Decimal("2")  # the later round wins the hour
