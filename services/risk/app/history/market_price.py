"""Empirical depeg probability from market prices, for stablecoins without a Chainlink feed.

A stablecoin such as EURCV has no Chainlink price feed, so the Chainlink path
(`HistoricalProbabilityService`) cannot measure its depeg rate. This module
measures it from DeFiLlama's aggregated market prices instead:

1. Peg. The asset registry (`data/catalog/assets.ethereum.json`) gives the
   asset's underlying. Only a direct off-chain fiat reserve names a peg:
   `usd-reserves` -> USD (reference 1), `eur-reserves` -> EUR (reference: the
   Chainlink EUR/USD answer). Any other asset has no fallback.
2. Ratio. Each DeFiLlama USD price is divided by the peg's USD value at the same
   time: the latest EUR/USD answer at or before the price's timestamp (the
   answer standing then), dropped when that answer is older than the feed's
   heartbeat + 30 minutes. A ratio of 1 is on peg.
3. Event. The ratio series is normalized hourly and fed to the same
   `RollingWindowEstimator` with the same event predicate as the Chainlink path
   (`DepegEvent`: the ratio at or below 1 - severity), over rolling 30-day
   windows with a Wilson interval. An hour carried forward within one sampling
   interval of a price is that price's value (the sample stands until the next
   one), like a Chainlink answer within its heartbeat; longer fills up to six
   hours count as missing, and longer gaps exclude their windows.

Confidence is at most MEDIUM: the prices are aggregated market prices sampled
every `period` (2 hours by default), not an on-chain oracle, and a depeg that
starts and ends between two samples is not seen.
"""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.adapters.defillama import DefiLlamaUnavailableError
from app.adapters.defillama_coins import TokenPrice, TokenPriceHistory
from app.adapters.ethereum import EthereumProviderError, UnsupportedFeedError
from app.catalog.assets import AssetRegistry
from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
)
from app.history.estimator import (
    DEFAULT_FORWARD_FILL,
    ChainlinkHistoryClient,
    EmpiricalProbabilityEvidence,
    EventPredicate,
    RollingWindowEstimator,
)
from app.history.normalization import (
    HOUR,
    NormalizedSeries,
    Observation,
    normalize_hourly,
    normalize_period,
)
from app.history.staleness import STALENESS_ALLOWANCE_SECONDS
from app.infrastructure.freshness import Freshness


ONE = Decimal(1)
MAXIMUM_CONFIDENCE = ConfidenceLevel.MEDIUM
# DeFiLlama marks a price below this confidence as unreliable; such a series
# gives a LOW-confidence estimate.
LOW_PRICE_CONFIDENCE = Decimal("0.9")
EUR_USD_FEED = "EUR/USD"
# Chainlink's published EUR/USD heartbeat (as `app.analysis.service.EUR_USD_HEARTBEAT_SECONDS`).
EUR_USD_HEARTBEAT_SECONDS = 86400
_CONFIDENCE_ORDER = [
    ConfidenceLevel.UNAVAILABLE,
    ConfidenceLevel.LOW,
    ConfidenceLevel.MEDIUM,
    ConfidenceLevel.HIGH,
]


@dataclass(frozen=True, slots=True)
class Peg:
    currency: str
    reserve: str
    # Chainlink feed pricing one unit of the currency in USD; None for USD.
    feed: str | None
    heartbeat_seconds: int | None = None


FIAT_PEGS: Mapping[str, Peg] = {
    "usd-reserves": Peg("USD", "usd-reserves", None),
    "eur-reserves": Peg("EUR", "eur-reserves", EUR_USD_FEED, EUR_USD_HEARTBEAT_SECONDS),
}


class TokenPriceSource(Protocol):
    def token_price_history(
        self, address: str, start: datetime, end: datetime
    ) -> TokenPriceHistory: ...


def registry_peg(registry: AssetRegistry, address: str) -> Peg | None:
    """The fiat peg of a registered asset backed directly by fiat reserves, else None."""
    asset = registry.asset(address)
    if asset is None or asset.underlying.reference is None:
        return None
    return FIAT_PEGS.get(asset.underlying.reference)


def peg_ratios(
    prices: Sequence[TokenPrice],
    reference: Sequence[Observation] | None,
    *,
    maximum_reference_age: timedelta | None = None,
) -> tuple[Observation, ...]:
    """Price / peg USD value at each price timestamp, as observations.

    `reference` None means a USD peg (value 1). Otherwise each price is divided
    by the latest reference answer at or before its timestamp; a price with no
    such answer, or whose answer is older than `maximum_reference_age`, is
    dropped rather than compared with a stale peg.
    """
    ordered = sorted(reference or (), key=lambda item: item.timestamp)
    stamps = [item.timestamp for item in ordered]
    ratios: list[Observation] = []
    for price in prices:
        if reference is None:
            peg_value = ONE
        else:
            index = bisect_right(stamps, price.timestamp) - 1
            if index < 0:
                continue
            answer = ordered[index]
            if (
                maximum_reference_age is not None
                and price.timestamp - answer.timestamp > maximum_reference_age
            ):
                continue
            if answer.value <= 0:
                continue
            peg_value = answer.value
        stamp = int(price.timestamp.timestamp())
        ratios.append(
            Observation(
                timestamp=price.timestamp,
                value=price.price_usd / peg_value,
                block_number=0,
                # Unique identity per sample for `normalize_hourly`; a later
                # sample in the same hour wins.
                log_index=stamp,
            )
        )
    return tuple(ratios)


def hourly_ratio_series(
    ratios: Sequence[Observation],
    start: datetime,
    end: datetime,
    *,
    sampling_interval: timedelta,
    source: str,
    retrieved_at: datetime,
    freshness: Freshness,
) -> NormalizedSeries:
    """Hourly series of the ratios; hours within one sampling interval of a sample are observed."""
    normalized = normalize_hourly(
        ratios,
        maximum_gap=max(DEFAULT_FORWARD_FILL, sampling_interval),
        start=start,
        end=end,
    )
    observations: list[Observation] = []
    last_sample: datetime | None = None
    for item in normalized.observations:
        if not item.is_forward_filled:
            last_sample = item.timestamp
            observations.append(item)
            continue
        within = last_sample is not None and item.timestamp - last_sample <= sampling_interval
        observations.append(replace(item, is_forward_filled=False) if within else item)
    return replace(
        normalized,
        observations=tuple(observations),
        source=source,
        retrieved_at=retrieved_at,
        freshness=freshness,
    )


class MarketPriceDepegHistory:
    """Depeg probability of a fiat-backed stablecoin from DeFiLlama market prices."""

    def __init__(
        self,
        prices: TokenPriceSource,
        chainlink: ChainlinkHistoryClient | None,
        registry: AssetRegistry,
    ) -> None:
        self._prices = prices
        self._chainlink = chainlink
        self._registry = registry
        self._estimator = RollingWindowEstimator()

    def estimate(
        self,
        address: str,
        symbol: str,
        start: datetime,
        end: datetime,
        event: EventPredicate,
        *,
        now: datetime,
    ) -> EmpiricalProbabilityEvidence:
        normalized_start, normalized_end = normalize_period(start, end)
        source = f"defillama-coins:ethereum:{address.lower()}"

        def unavailable(reason: str) -> EmpiricalProbabilityEvidence:
            return _unavailable(source, normalized_start, normalized_end, now, event, reason)

        peg = registry_peg(self._registry, address)
        if peg is None:
            return unavailable(
                f"{symbol} has no fiat-reserve peg in the asset registry, so its market "
                "price has no reference value."
            )
        if peg.feed is not None and self._chainlink is None:
            return unavailable(f"Chainlink {peg.feed} history is not configured.")
        try:
            history = self._prices.token_price_history(address, normalized_start, normalized_end)
        except DefiLlamaUnavailableError as error:
            return unavailable(str(error))
        if not history.prices:
            return unavailable(f"DeFiLlama has no {symbol} price history for the period.")

        reference: tuple[Observation, ...] | None = None
        maximum_age: timedelta | None = None
        if peg.feed is not None:
            maximum_age = timedelta(
                seconds=(peg.heartbeat_seconds or 0) + STALENESS_ALLOWANCE_SECONDS
            )
            try:
                reference = self._chainlink.get_chainlink_observations(  # type: ignore[union-attr]
                    peg.feed, normalized_start - maximum_age, normalized_end
                )
            except UnsupportedFeedError:
                return unavailable(f"Chainlink {peg.feed} is not a readable feed.")
            except EthereumProviderError:
                return unavailable(f"Chainlink {peg.feed} history is unavailable.")
            if not reference:
                return unavailable(f"Chainlink {peg.feed} returned no history.")
            source = f"{source}+chainlink:{peg.feed}"

        ratios = peg_ratios(history.prices, reference, maximum_reference_age=maximum_age)
        if not ratios:
            return unavailable(
                f"No {symbol} price could be paired with a current {peg.feed} answer."
            )
        try:
            series = hourly_ratio_series(
                ratios,
                normalized_start,
                normalized_end,
                sampling_interval=history.period,
                source=source,
                retrieved_at=now,
                freshness=history.freshness,
            )
        except ValueError:
            return unavailable(f"No {symbol} market price falls inside the period.")
        evidence = self._estimator.estimate_with_evidence(series, event)
        probability = evidence.probability
        if probability.central is None:
            return replace(
                evidence,
                probability=probability.model_copy(
                    update={
                        "explanation": (
                            f"Market-price history for {symbol} is insufficient: "
                            f"{probability.explanation}"
                        )
                    }
                ),
            )
        cap = (
            ConfidenceLevel.LOW
            if history.confidence is not None and history.confidence < LOW_PRICE_CONFIDENCE
            else MAXIMUM_CONFIDENCE
        )
        confidence = min(probability.confidence, cap, key=_CONFIDENCE_ORDER.index)
        return replace(
            evidence,
            probability=probability.model_copy(
                update={
                    "confidence": confidence,
                    "explanation": _explain(
                        symbol, peg, history, reference, ratios, evidence, event
                    ),
                }
            ),
        )


def _explain(
    symbol: str,
    peg: Peg,
    history: TokenPriceHistory,
    reference: Sequence[Observation] | None,
    ratios: Sequence[Observation],
    evidence: EmpiricalProbabilityEvidence,
    event: EventPredicate,
) -> str:
    hours = int(history.period / HOUR)
    resolution = f"{hours}-hour" if hours < 24 else f"{hours // 24}-day"
    start, end = evidence.observation_period
    days = (end - start).days
    threshold = getattr(event, "threshold", None)
    outcome = (
        f"had {symbol} at or below {threshold:f} of its {peg.currency} peg"
        if isinstance(threshold, Decimal)
        else f"contained the event ({evidence.threshold_definition}) on the {symbol}/{peg.currency} ratio"
    )
    if peg.feed is None:
        ratio = (
            f"{symbol} is compared with 1 USD: the asset registry lists it as backed by USD "
            "reserves, so its peg is USD."
        )
    else:
        ratio = (
            f"Each {symbol} USD price is divided by the latest Chainlink {peg.feed} answer at "
            f"or before it ({len(reference or ())} rounds read); the peg is {peg.currency} "
            f"because the asset registry lists {symbol} as backed by {peg.currency} reserves "
            f"({peg.reserve})."
        )
    confidence = (
        f", DeFiLlama price confidence {history.confidence:f}"
        if history.confidence is not None
        else ""
    )
    return (
        f"{evidence.event_window_count} of {evidence.window_count} complete overlapping 30-day "
        f"windows (hourly starts over {days} days) {outcome}. "
        f"Market-price fallback, because {symbol} has no Chainlink price feed: DeFiLlama "
        f"coins aggregated market prices at {resolution} resolution ({len(ratios)} paired "
        f"samples of {len(history.prices)} read in {history.chunk_count} chunks{confidence}, "
        f"{evidence.missing_observation_ratio:.1%} of hours missing). {ratio} A depeg that "
        f"starts and ends between two {resolution} samples is missed, and market prices are "
        "not an on-chain oracle, so confidence is at most medium."
    )


def _unavailable(
    source: str,
    start: datetime,
    end: datetime,
    now: datetime,
    event: EventPredicate,
    reason: str,
) -> EmpiricalProbabilityEvidence:
    expected = max(0, int((end - start) / HOUR) + 1)
    return EmpiricalProbabilityEvidence(
        probability=ProbabilityEstimate(
            method=ProbabilityMethod.UNAVAILABLE,
            low=None,
            central=None,
            high=None,
            confidence=ConfidenceLevel.UNAVAILABLE,
            horizon_days=30,
            source=source,
            retrieved_at=now.astimezone(UTC),
            freshness=FreshnessStatus.EXPIRED,
            explanation=reason,
        ),
        observation_count=0,
        expected_observation_count=expected,
        window_count=0,
        event_window_count=0,
        sample_dates=(),
        gaps=(),
        missing_observation_ratio=Decimal(1) if expected else Decimal(0),
        observation_period=(start, end),
        sampling_interval=HOUR,
        threshold_definition=str(
            getattr(event, "description", getattr(event, "__name__", "event"))
        ),
    )
