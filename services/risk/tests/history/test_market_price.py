from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.defillama import DefiLlamaUnavailableError
from app.adapters.defillama_coins import TokenPrice, TokenPriceHistory
from app.adapters.ethereum import EthereumProviderError
from app.analysis.service import DepegEvent
from app.catalog.assets import load_asset_registry
from app.contracts.models import ConfidenceLevel, ProbabilityMethod
from app.history.estimator import RollingWindowEstimator
from app.history.market_price import (
    MarketPriceDepegHistory,
    hourly_ratio_series,
    peg_ratios,
    registry_peg,
)
from app.history.normalization import NormalizedSeries, Observation
from app.infrastructure.freshness import Freshness
from tests.fixtures.defillama_coins.recorded import EURCV, EUROP, FakeCoinsApi


REGISTRY = load_asset_registry()
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
STEAK_EURCV = "0xbeef0C075Da5D01112AE5cF34d257074fB5DDB2f"
START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=40)
NOW = END + timedelta(minutes=5)


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


def answer(hours: float, value: str, index: int = 0) -> Observation:
    return Observation(timestamp=at(hours), value=Decimal(value), block_number=0, log_index=0, round_id=index + 1)


class FakePrices:
    def __init__(self, prices, *, period=timedelta(hours=2), confidence="0.99", error=None):
        self.prices = tuple(prices)
        self.period = period
        self.confidence = Decimal(confidence) if confidence is not None else None
        self.error = error
        self.calls: list[tuple[str, datetime, datetime]] = []

    def token_price_history(self, address, start, end):
        self.calls.append((address, start, end))
        if self.error is not None:
            raise self.error
        return TokenPriceHistory(
            address=address.lower(),
            symbol="SYN",
            confidence=self.confidence,
            period=self.period,
            start=start,
            end=end,
            prices=tuple(item for item in self.prices if start <= item.timestamp <= end),
            source="defillama-coins fake",
            fetched_at=NOW,
            freshness=Freshness.FRESH,
            chunk_count=1,
        )


class FakeChainlink:
    def __init__(self, answers=(), *, error=None):
        self.answers = tuple(answers)
        self.error = error
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_chainlink_observations(self, feed, start, end):
        self.calls.append((feed, start, end))
        if self.error is not None:
            raise self.error
        return tuple(item for item in self.answers if start <= item.timestamp <= end)


def flat_prices(value: str, *, step_hours: int = 2, days: int = 40, overrides=None):
    overrides = overrides or {}
    return [
        TokenPrice(at(hour), Decimal(overrides.get(hour, value)))
        for hour in range(0, days * 24 + 1, step_hours)
    ]


def daily_eur_usd(value: str, *, days: int = 41, overrides=None):
    overrides = overrides or {}
    return [answer(day * 24 - 24, overrides.get(day, value), day) for day in range(days + 1)]


# Peg from the asset registry


def test_registry_peg_follows_the_fiat_reserve_underlying():
    eur = registry_peg(REGISTRY, EURCV.lower())
    assert eur is not None and (eur.currency, eur.feed) == ("EUR", "EUR/USD")
    assert registry_peg(REGISTRY, EUROP).currency == "EUR"
    usd = registry_peg(REGISTRY, USDC)
    assert usd is not None and (usd.currency, usd.feed) == ("USD", None)
    # Not fiat-backed, backed by another token, or not registered: no peg.
    assert registry_peg(REGISTRY, WETH) is None
    assert registry_peg(REGISTRY, STEAK_EURCV) is None
    assert registry_peg(REGISTRY, "0x00000000000000000000000000000000000000a1") is None


# Ratio construction


def test_ratio_uses_the_latest_eur_usd_answer_at_or_before_each_price():
    reference = [answer(10, "1.25", 1), answer(0, "1.10", 0)]
    prices = [
        TokenPrice(at(-1), Decimal("1.10")),  # before any answer: dropped
        TokenPrice(at(1), Decimal("1.10")),  # answer at 0
        TokenPrice(at(9.99), Decimal("1.21")),  # still the answer at 0
        TokenPrice(at(10), Decimal("1.25")),  # the answer at exactly 10
        TokenPrice(at(34), Decimal("1.25")),  # 24 h after 10: within heartbeat + 30 min
        TokenPrice(at(35), Decimal("1.25")),  # 25 h after: stale reference, dropped
    ]

    ratios = peg_ratios(prices, reference, maximum_reference_age=timedelta(hours=24, minutes=30))

    assert [item.timestamp for item in ratios] == [at(1), at(9.99), at(10), at(34)]
    assert [item.value for item in ratios] == [
        Decimal(1),
        Decimal("1.21") / Decimal("1.10"),
        Decimal(1),
        Decimal(1),
    ]


def test_usd_peg_ratio_is_the_usd_price():
    prices = [TokenPrice(at(0), Decimal("0.97")), TokenPrice(at(2), Decimal("1.01"))]

    ratios = peg_ratios(prices, None)

    assert [item.value for item in ratios] == [Decimal("0.97"), Decimal("1.01")]


def test_hourly_series_counts_hours_within_one_sampling_interval_as_observed():
    ratios = peg_ratios(
        [
            TokenPrice(at(0), Decimal(1)),
            TokenPrice(at(2), Decimal(1)),  # regular 2-hour step
            TokenPrice(at(7), Decimal(1)),  # 5-hour gap: hours 3-4 observed, 5-6 imputed
            TokenPrice(at(20), Decimal(1)),  # 13-hour gap: a data gap
        ],
        None,
    )

    series = hourly_ratio_series(
        ratios,
        at(0),
        at(20),
        sampling_interval=timedelta(hours=2),
        source="test",
        retrieved_at=NOW,
        freshness=Freshness.FRESH,
    )

    filled = {item.timestamp for item in series.observations if item.is_forward_filled}
    assert filled == {at(5), at(6)}
    assert {item.timestamp for item in series.observations} >= {at(1), at(3), at(4)}
    assert [(gap.start, gap.end) for gap in series.gaps] == [(at(8), at(19))]


# Event semantics shared with the Chainlink path


def test_market_and_chainlink_paths_count_the_same_depeg_windows():
    # Ratio values per hour, as a Chainlink peg feed would report them: a dip to
    # exactly 0.90 (counts: at or below), one to 0.9001 (does not), and an
    # upward move to 1.12 (does not: the event is a fall from the peg).
    values = {hour: Decimal("1") for hour in range(40 * 24 + 1)}
    values[300] = Decimal("0.90")
    values[500] = Decimal("0.9001")
    values[700] = Decimal("1.12")
    chainlink_series = NormalizedSeries(
        observations=tuple(
            Observation(timestamp=at(hour), value=value, block_number=hour, log_index=0)
            for hour, value in values.items()
        ),
        gaps=(),
        start=START,
        end=END,
        source="chainlink:test",
        retrieved_at=NOW,
        freshness=Freshness.FRESH,
    )
    event = DepegEvent(Decimal("0.10"))
    chainlink = RollingWindowEstimator().estimate_with_evidence(chainlink_series, event)

    # The same ratios from market data: EURCV in USD over a constant EUR/USD of 1.2.
    prices = [TokenPrice(at(hour), value * Decimal("1.2")) for hour, value in values.items()]
    market = MarketPriceDepegHistory(
        FakePrices(prices, period=timedelta(hours=1)),
        FakeChainlink(daily_eur_usd("1.2")),
        REGISTRY,
    ).estimate(EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW)

    assert chainlink.event_window_count > 0
    assert market.window_count == chainlink.window_count
    assert market.event_window_count == chainlink.event_window_count
    assert market.sample_dates == chainlink.sample_dates
    assert market.probability.central == chainlink.probability.central
    assert market.probability.low == chainlink.probability.low
    assert market.probability.high == chainlink.probability.high


def test_a_euro_move_is_not_a_depeg_but_eurcv_leaving_the_euro_is():
    eur_usd = daily_eur_usd("1.20", overrides={day: "1.05" for day in range(10, 13)})
    # EURCV follows the euro down by 12.5%: on peg throughout.
    following = flat_prices("1.20", overrides={hour: "1.05" for hour in range(10 * 24 - 24, 13 * 24 - 24)})
    # EURCV falls to 1.07 USD while the euro stays at 1.20: 0.89 of its peg.
    depegged = flat_prices("1.20", overrides={hour: "1.07" for hour in range(600, 606, 2)})
    estimate = MarketPriceDepegHistory(FakePrices(following), FakeChainlink(eur_usd), REGISTRY).estimate

    on_peg = estimate(EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW)
    off_peg = MarketPriceDepegHistory(
        FakePrices(depegged), FakeChainlink(daily_eur_usd("1.20")), REGISTRY
    ).estimate(EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW)

    assert on_peg.probability.central == 0
    assert off_peg.probability.central > 0
    assert off_peg.event_window_count > 0


# Estimate labelling


def test_eur_estimate_is_labelled_and_capped_at_medium():
    chainlink = FakeChainlink(daily_eur_usd("1.17"))
    prices = FakePrices(flat_prices("1.17"))

    evidence = MarketPriceDepegHistory(prices, chainlink, REGISTRY).estimate(
        EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW
    )

    probability = evidence.probability
    assert probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert probability.central == 0
    assert probability.confidence is ConfidenceLevel.MEDIUM
    assert probability.source == f"defillama-coins:ethereum:{EURCV.lower()}+chainlink:EUR/USD"
    text = probability.explanation
    for expected in (
        "of its EUR peg",
        "0.90",
        "2-hour resolution",
        "481 paired samples",
        "peg is EUR",
        "EURCV has no Chainlink price feed",
        "missed",
        "at most medium",
        f"of {evidence.window_count} complete overlapping 30-day windows",
    ):
        assert expected in text, expected
    # EUR/USD is read from one heartbeat + 30 minutes before the window, so the
    # first prices have a standing answer.
    ((feed, read_from, _),) = chainlink.calls
    assert feed == "EUR/USD"
    assert read_from == START - timedelta(hours=24, minutes=30)


def test_usd_peg_needs_no_chainlink_reference():
    prices = FakePrices(flat_prices("1.0", overrides={400: "0.85"}))

    evidence = MarketPriceDepegHistory(prices, None, REGISTRY).estimate(
        USDC, "USDC", START, END, DepegEvent(Decimal("0.10")), now=NOW
    )

    assert evidence.probability.central > 0
    assert evidence.probability.source == f"defillama-coins:ethereum:{USDC.lower()}"
    assert "backed by USD reserves" in evidence.probability.explanation


def test_low_defillama_confidence_lowers_the_estimate_confidence():
    prices = FakePrices(flat_prices("1.17"), confidence="0.5")

    evidence = MarketPriceDepegHistory(prices, FakeChainlink(daily_eur_usd("1.17")), REGISTRY).estimate(
        EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW
    )

    assert evidence.probability.confidence is ConfidenceLevel.LOW


def test_recorded_eurcv_chunk_against_a_flat_euro():
    # 42 days of real 2-hour EURCV prices (1.150-1.172 USD) over EUR/USD 1.16.
    start = datetime.fromtimestamp(1785600000, UTC)
    end = start + timedelta(days=41)
    reference = [
        Observation(timestamp=start + timedelta(days=day - 1), value=Decimal("1.16"), block_number=0, log_index=0, round_id=day + 1)
        for day in range(43)
    ]
    coins = FakeCoinsApi().client(lambda: end + timedelta(days=30))

    evidence = MarketPriceDepegHistory(coins, FakeChainlink(reference), REGISTRY).estimate(
        EURCV, "EURCV", start, end, DepegEvent(Decimal("0.10")), now=end
    )

    assert evidence.probability.central == 0
    assert evidence.window_count > 0
    assert evidence.probability.confidence is ConfidenceLevel.MEDIUM
    assert evidence.missing_observation_ratio < Decimal("0.01")


# Unavailable cases


@pytest.mark.parametrize(
    ("address", "symbol"),
    [("0x00000000000000000000000000000000000000a1", "XYZ"), (WETH, "WETH"), (STEAK_EURCV, "steakEURCV")],
)
def test_no_registry_fiat_peg_is_unavailable(address, symbol):
    prices = FakePrices(flat_prices("1"))

    evidence = MarketPriceDepegHistory(prices, FakeChainlink(), REGISTRY).estimate(
        address, symbol, START, END, DepegEvent(Decimal("0.10")), now=NOW
    )

    assert evidence.probability.method is ProbabilityMethod.UNAVAILABLE
    assert evidence.probability.central is None
    assert "no fiat-reserve peg" in evidence.probability.explanation
    assert prices.calls == []


@pytest.mark.parametrize(
    ("prices", "chainlink", "reason"),
    [
        (
            FakePrices((), error=DefiLlamaUnavailableError("DeFiLlama coins chart for x unavailable: HTTP 500")),
            FakeChainlink(daily_eur_usd("1.17")),
            "HTTP 500",
        ),
        (FakePrices(()), FakeChainlink(daily_eur_usd("1.17")), "no EURCV price history"),
        (FakePrices(flat_prices("1.17")), FakeChainlink(error=EthereumProviderError("rpc down")), "EUR/USD history is unavailable"),
        (FakePrices(flat_prices("1.17")), FakeChainlink(()), "EUR/USD returned no history"),
        (FakePrices(flat_prices("1.17")), None, "EUR/USD history is not configured"),
        (FakePrices(flat_prices("1.17", days=10)), FakeChainlink(daily_eur_usd("1.17")), "insufficient"),
    ],
)
def test_missing_inputs_are_unavailable_with_a_reason(prices, chainlink, reason):
    evidence = MarketPriceDepegHistory(prices, chainlink, REGISTRY).estimate(
        EURCV, "EURCV", START, END, DepegEvent(Decimal("0.10")), now=NOW
    )

    assert evidence.probability.central is None
    assert evidence.probability.method is ProbabilityMethod.UNAVAILABLE
    assert reason in evidence.probability.explanation
    assert "rpc down" not in evidence.probability.explanation
