from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.radar.analysis.oracle_consensus import OracleConsensus, OracleFeedObservation
from app.radar.contracts import HealthStatus


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def feed(
    entity_id: str,
    price: str,
    source_family: str,
    *,
    age_minutes: int = 0,
    derived: bool = False,
) -> OracleFeedObservation:
    return OracleFeedObservation(
        entity_id=entity_id,
        represented_pair="USDC/USD",
        price=Decimal(price),
        observed_at=NOW - timedelta(minutes=age_minutes),
        heartbeat_seconds=3600,
        source_family=source_family,
        derived=derived,
    )


def test_consensus_uses_median_and_reports_feed_deviations():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "1.01", "pyth"),
            feed("oracle:1:c", "1.50", "redstone"),
        ),
        NOW,
    )

    assert result.reference_price == Decimal("1.01")
    assert result.deviations["oracle:1:a"] == Decimal("0.009900990099009900990099009901")
    assert result.feed_statuses["oracle:1:c"] is HealthStatus.CRITICAL


def test_stale_feeds_are_excluded_from_consensus():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "1.01", "pyth"),
            feed("oracle:1:c", "1.02", "redstone"),
            feed("oracle:1:stale", "0.20", "uniswap", age_minutes=100),
        ),
        NOW,
    )

    assert result.reference_price == Decimal("1.01")
    assert result.excluded == ("oracle:1:stale",)


def test_fewer_than_two_independent_fresh_sources_is_unknown():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "1.01", "chainlink"),
        ),
        NOW,
    )

    assert result.status is HealthStatus.UNKNOWN
    assert result.reference_price is None
    assert "two independent fresh oracle peers are required" in result.reasons


def test_derived_references_are_labelled_but_can_join_consensus():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "1.00", "pyth"),
            feed("oracle:1:derived", "1.00", "dex-twap", derived=True),
        ),
        NOW,
    )

    assert result.status is HealthStatus.HEALTHY
    assert result.derived_feeds == ("oracle:1:derived",)


def test_a_feed_is_not_its_own_peer_so_two_sources_cannot_form_consensus():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "0.92", "pyth"),
        ),
        NOW,
    )

    assert result.status is HealthStatus.UNKNOWN
    assert result.reference_price is None
    assert result.deviations == {}
    assert set(result.feed_statuses.values()) == {HealthStatus.UNKNOWN}


def test_non_positive_price_is_a_hard_failure_even_without_peers():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:zero", "0", "pyth"),
        ),
        NOW,
    )

    assert result.status is HealthStatus.CRITICAL
    assert result.feed_statuses["oracle:1:zero"] is HealthStatus.CRITICAL
    assert result.feed_statuses["oracle:1:a"] is HealthStatus.UNKNOWN
    assert "oracle reported a non-positive price" in result.reasons


def test_hard_failure_is_excluded_from_the_median_and_critical_wins():
    result = OracleConsensus().evaluate(
        (
            feed("oracle:1:a", "1.00", "chainlink"),
            feed("oracle:1:b", "1.00", "pyth"),
            feed("oracle:1:c", "1.00", "redstone"),
            feed("oracle:1:neg", "-1", "uniswap"),
        ),
        NOW,
    )

    assert result.reference_price == Decimal("1.00")
    assert result.status is HealthStatus.CRITICAL
    assert "oracle:1:neg" not in result.included


def test_non_finite_price_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        feed("oracle:1:nan", "NaN", "chainlink")
