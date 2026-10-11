"""Chainlink update cadence: heartbeat inference and staleness windows from exact round times.

Heartbeat inference (`infer_heartbeat`)
    A Chainlink feed publishes a new round when the answer moves past its
    deviation threshold or when the heartbeat elapses, whichever comes first, so
    the longest regular gap between updates is the heartbeat. The inferred
    heartbeat is the 99th percentile (nearest rank) of the gaps between
    consecutive updates, which ignores the rare outage that a maximum would
    adopt, snapped to the smallest standard Chainlink heartbeat that is at least
    the percentile less a 5 % tolerance for block-inclusion delay. At least
    `MINIMUM_GAPS_FOR_INFERENCE` gaps are required; otherwise the heartbeat is
    unknown. A documented table of well-known feeds overrides the inference (see
    `app.analysis.service.CHAINLINK_HEARTBEATS`).

Staleness (`staleness_series`, `StalenessEvent`)
    Risk0's oracle-failure trigger fires when a feed is older than its heartbeat
    plus 30 minutes (spec § 13 interpretation). From the exact update
    timestamps, every gap between consecutive updates longer than heartbeat +
    1800 s is a stale interval starting `heartbeat + 1800 s` after the earlier
    update and ending at the later one (the last update's interval stays open
    until `as_of`). Each UTC hour of the period is flagged 1 when it overlaps a
    stale interval and 0 otherwise, so the hourly grid of the rolling-window
    estimator loses no staleness: a 30-day window contains the event exactly
    when it overlaps a stale interval. Hours before the first known update are
    missing, never assumed healthy.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.history.normalization import (
    HOUR,
    DataGap,
    NormalizedSeries,
    Observation,
    normalize_period,
)
from app.infrastructure.freshness import Freshness


# Spec § 13: an oracle stale for 30 minutes beyond its heartbeat is a failure.
STALENESS_ALLOWANCE_SECONDS = 1800
HEARTBEAT_PERCENTILE = 99
HEARTBEAT_SNAP_TOLERANCE = Decimal("0.05")
MINIMUM_GAPS_FOR_INFERENCE = 20
# Heartbeats Chainlink configures on Ethereum mainnet feeds (data.chain.link).
STANDARD_HEARTBEATS_SECONDS = (60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800)


def infer_heartbeat(update_times: Sequence[datetime]) -> int | None:
    """The feed's heartbeat in seconds from its update times, or None with too few gaps."""
    times = sorted({_seconds(item) for item in update_times})
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    if len(gaps) < MINIMUM_GAPS_FOR_INFERENCE:
        return None
    ordered = sorted(gaps)
    rank = -(-HEARTBEAT_PERCENTILE * len(ordered) // 100)  # nearest rank, 1-based
    percentile = ordered[max(1, rank) - 1]
    floor = Decimal(percentile) / (Decimal(1) + HEARTBEAT_SNAP_TOLERANCE)
    for candidate in STANDARD_HEARTBEATS_SECONDS:
        if candidate >= floor:
            return candidate
    return percentile


class StalenessEvent:
    """The window overlaps a period where the feed was older than heartbeat + 30 minutes."""

    def __init__(self, heartbeat_seconds: int) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.description = (
            f"gap between consecutive updates longer than the {heartbeat_seconds} s heartbeat "
            f"plus {STALENESS_ALLOWANCE_SECONDS} s"
        )

    def __call__(self, window: Sequence[Observation]) -> bool:
        return any(item.value > 0 for item in window)

    def flags(self, observations: Sequence[Observation]) -> list[bool]:
        """Per-hour event flags; a window contains the event when any flag in it is set."""
        return [item.value > 0 for item in observations]


def staleness_series(
    updates: Sequence[Observation],
    heartbeat_seconds: int,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime,
    source: str,
    retrieved_at: datetime,
) -> NormalizedSeries | None:
    """Hourly 0/1 staleness flags over [start, end]; None when no update precedes the period's end."""
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat must be positive")
    period_start, period_end = normalize_period(start, end)
    times = sorted({_seconds(item.timestamp) for item in updates})
    horizon = _seconds(as_of)
    threshold = heartbeat_seconds + STALENESS_ALLOWANCE_SECONDS
    stale: list[tuple[int, int]] = []
    for earlier, later in zip(times, [*times[1:], horizon], strict=False):
        if later - earlier > threshold:
            stale.append((earlier + threshold, later))
    first_hour = _seconds(period_start)
    last_hour = _seconds(period_end)
    hour = int(HOUR.total_seconds())
    if not times or times[0] > last_hour + hour - 1:
        return None
    # The first hour fully covered by a known update: the hour containing the first update
    # is still partly unknown, so coverage starts at the next hour unless the update opens it.
    covered_from = times[0] if times[0] % hour == 0 else times[0] - times[0] % hour + hour
    covered_from = max(covered_from, first_hour)
    observations: list[Observation] = []
    index = 0
    for hour_start in range(covered_from, last_hour + 1, hour):
        hour_end = hour_start + hour
        while index < len(stale) and stale[index][1] <= hour_start:
            index += 1
        flagged = index < len(stale) and stale[index][0] < hour_end
        observations.append(
            Observation(
                timestamp=datetime.fromtimestamp(hour_start, tz=UTC),
                value=Decimal(1) if flagged else Decimal(0),
                block_number=0,
                log_index=0,
                source=source,
            )
        )
    if not observations:
        return None
    gaps: tuple[DataGap, ...] = ()
    if covered_from > first_hour:
        gap_end = datetime.fromtimestamp(covered_from, tz=UTC) - HOUR
        gaps = (
            DataGap(
                start=period_start,
                end=gap_end,
                missing_observations=int((gap_end - period_start) / HOUR) + 1,
            ),
        )
    return NormalizedSeries(
        observations=tuple(observations),
        gaps=gaps,
        start=period_start,
        end=period_end,
        source=source,
        retrieved_at=retrieved_at,
        freshness=Freshness.FRESH,
    )


def _seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp())

