from datetime import UTC, datetime, timedelta

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


def test_data_quality_preserves_expired_critical_data_as_missing():
    quality = DataQuality(
        freshness=(Freshness.EXPIRED,),
        conflicts=(),
        critical_missing=("oracle:eth-usd",),
    )

    assert quality.freshness == (Freshness.EXPIRED,)
    assert quality.critical_missing == ("oracle:eth-usd",)
