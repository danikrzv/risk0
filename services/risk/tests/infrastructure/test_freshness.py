from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.infrastructure.freshness import DataQuality, Freshness


FETCHED_AT = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
TTL = timedelta(minutes=1)
MAXIMUM_AGE = timedelta(minutes=5)


def test_freshness_classifies_fresh_data_before_ttl():
    assert (
        Freshness.from_times(
            FETCHED_AT,
            FETCHED_AT + timedelta(seconds=59),
            TTL,
            MAXIMUM_AGE,
        )
        is Freshness.FRESH
    )


def test_freshness_classifies_data_at_ttl_as_stale_usable():
    assert (
        Freshness.from_times(FETCHED_AT, FETCHED_AT + TTL, TTL, MAXIMUM_AGE)
        is Freshness.STALE_USABLE
    )


def test_freshness_classifies_data_at_maximum_age_as_expired():
    assert (
        Freshness.from_times(
            FETCHED_AT,
            FETCHED_AT + MAXIMUM_AGE,
            TTL,
            MAXIMUM_AGE,
        )
        is Freshness.EXPIRED
    )


def test_freshness_normalizes_dst_fallback_before_measuring_age():
    zurich = ZoneInfo("Europe/Zurich")
    fetched_at = datetime(2026, 10, 25, 2, 45, tzinfo=zurich, fold=0)
    now = datetime(2026, 10, 25, 2, 15, tzinfo=zurich, fold=1)

    assert (
        Freshness.from_times(
            fetched_at,
            now,
            ttl=timedelta(minutes=20),
            maximum_age=timedelta(hours=1),
        )
        is Freshness.STALE_USABLE
    )


@pytest.mark.parametrize("naive_argument", ["fetched_at", "now"])
def test_freshness_rejects_naive_datetimes(naive_argument):
    fetched_at = FETCHED_AT
    now = FETCHED_AT + timedelta(seconds=30)
    if naive_argument == "fetched_at":
        fetched_at = fetched_at.replace(tzinfo=None)
    else:
        now = now.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-aware"):
        Freshness.from_times(fetched_at, now, TTL, MAXIMUM_AGE)


def test_data_quality_records_expired_critical_source_as_missing():
    quality = DataQuality.from_sources(
        sources=(
            ("market:weth-usdc", Freshness.FRESH),
            ("oracle:eth-usd", Freshness.EXPIRED),
        ),
        critical_sources=("oracle:eth-usd",),
        conflicts=(),
    )

    assert quality.freshness == (Freshness.FRESH, Freshness.EXPIRED)
    assert quality.critical_missing == ("oracle:eth-usd",)
