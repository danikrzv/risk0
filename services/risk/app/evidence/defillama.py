"""Protocol evidence derived from DeFiLlama: an empirical impairment base rate and TVL shares.

Empirical protocol-impairment base rate (`compute_base_rate`)
-------------------------------------------------------------
Question: how often does a large lending protocol suffer a material exploit in a
year? Answered as the share of protocol-years with at least one material exploit.

- Population: DeFiLlama protocol entries in `LENDING_CATEGORIES` ("Lending"
  money markets and "CDP" collateralized-debt protocols) with current TVL of at
  least `TVL_FLOOR_USD`, on any chain. CDPs are included because they carry the
  same failure modes as a money market such as Morpho: overcollateralized loans,
  oracle-driven liquidations, governance- or admin-controlled parameters, and
  bad debt when liquidations fail. All chains are used because Ethereum alone
  leaves too few protocol-years for a usable rate. A DeFiLlama entry is one
  protocol version or deployment family (Aave V2 and Aave V3 are separate units).
- Window: the last `OBSERVATION_YEARS` complete calendar years before `now`.
- Exposure: a protocol-year is counted when the protocol was listed on DeFiLlama
  before `EXPOSURE_CUTOFF` (1 July, i.e. exposed for at least half the year).
  Protocols without a listing date predate DeFiLlama's listing records (the
  earliest recorded date is October 2021) and count for every year. A year with
  a matched incident always counts.
- Event: an incident in DeFiLlama `/hacks` with a gross loss of at least
  `MATERIAL_LOSS_USD` (1% of the TVL floor; incidents without an amount are not
  material) matched to a population protocol. A protocol-year is an event year
  however many incidents it had.
- Matching rule: the incident's `defillamaId` equals the protocol's id; an
  incident without an id matches a protocol whose name, or a previous name,
  equals the incident name after lowercasing and removing non-alphanumerics. An
  incident with an id outside the population never matches by name.
- Rate: events / protocol-years, with the Wilson 95% interval as low and high.
  The annual rate `a` becomes a 30-day probability as `1 - (1 - a)^(30/365)`,
  the conversion `app.evidence.scoring.map_score_to_probability` uses.

Caveats, stated in every explanation: the population is selected on *current*
TVL, so a protocol that an exploit drained or closed drops out (survivorship
bias, which pushes the rate down, so it is closer to a lower bound; the
explanation lists how many material incidents DeFiLlama links to lending
protocols now below the floor); incidents named differently from the protocol
entry (for example "Venus Protocol" versus "Venus Core Pool") or recorded
against another version are not matched (undercount); losses are gross of
recoveries. The estimate is an industry base rate, not a protocol-specific
score, so it is always LOW confidence.

The rate is the probability of an impairment *event* within 30 days. The
scenario severity is the loss fraction given that event; it does not scale the
probability.

Oracle-exploit prior (`oracle_exploit_estimate`)
-------------------------------------------------
The same population, window, exposure rule, matching rule, materiality
threshold, Wilson interval and 30-day conversion, restricted to oracle-related
incidents: DeFiLlama classification "Oracle Manipulation", or a technique that
mentions "oracle" (for example "Oracle Misconfiguration", "Stale Oracle
Price"). It answers "how often does a large lending protocol suffer a material
oracle-related exploit in a year" and is used as the oracle-failure probability
of a third-party feed whose staleness history cannot be measured. It is an
industry prior, not a feed-specific estimate, so it is always LOW confidence.

TVL concentration (`protocol_tvl_snapshot`): see `app.domain.protocol`.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext

from app.adapters.defillama import (
    DefiLlamaChain,
    DefiLlamaHack,
    DefiLlamaProtocol,
    chain_by_name,
    protocol_by_slug,
)
from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
)
from app.domain.protocol import SHARE_QUANTUM, ProtocolTvlSnapshot
from app.domain.snapshots import DataEnvelope
from app.history.estimator import _wilson_interval as wilson_interval


BASE_RATE_MODEL_VERSION = "protocol-base-rate-v1"
ORACLE_PRIOR_MODEL_VERSION = "oracle-exploit-prior-v1"
ORACLE_CLASSIFICATION = "oracle manipulation"
ORACLE_TECHNIQUE_MARKER = "oracle"
LENDING_CATEGORIES = frozenset({"Lending", "CDP"})
TVL_FLOOR_USD = Decimal(100_000_000)
MATERIAL_LOSS_USD = Decimal(1_000_000)
OBSERVATION_YEARS = 5
EXPOSURE_CUTOFF = (7, 1)  # (month, day)
BASE_RATE_CONFIDENCE = ConfidenceLevel.LOW
HORIZON_DAYS = 30
DAYS_PER_YEAR = 365
PRECISION = 28
CHAIN = "Ethereum"

_FRESHNESS_ORDER = (FreshnessStatus.FRESH, FreshnessStatus.STALE_USABLE, FreshnessStatus.EXPIRED)
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True, slots=True)
class DefiLlamaProtocolRef:
    """How a Risk0 protocol key maps onto DeFiLlama."""

    slug: str
    parent: str
    name_token: str
    label: str


DEFILLAMA_PROTOCOLS: Mapping[str, DefiLlamaProtocolRef] = {
    # Morpho Blue is the protocol the analysed markets and vaults deploy on.
    "morpho": DefiLlamaProtocolRef(
        slug="morpho-blue", parent="parent#morpho", name_token="morpho", label="Morpho Blue"
    ),
}


@dataclass(frozen=True, slots=True)
class MatchedIncident:
    protocol: DefiLlamaProtocol
    hack: DefiLlamaHack
    matched_by: str


@dataclass(frozen=True, slots=True)
class BaseRate:
    first_year: int
    last_year: int
    population: tuple[DefiLlamaProtocol, ...]
    protocol_years: int
    event_years: int
    incidents: tuple[MatchedIncident, ...]
    annual_central: Decimal
    annual_low: Decimal
    annual_high: Decimal

    # (protocol id, year) pairs counted as exposed.
    exposure: tuple[tuple[str, int], ...] = ()
    # Material in-window incidents of Lending/CDP protocols now below the TVL
    # floor, largest first: the survivorship gap the population cannot count.
    below_floor_incidents: tuple[MatchedIncident, ...] = ()

    def protocol_years_of(self, protocol_id: str) -> int:
        return sum(1 for key, _ in self.exposure if key == protocol_id)

    def event_years_of(self, protocol_id: str) -> int:
        return len({item.hack.occurred_at.year for item in self.incidents if item.protocol.id == protocol_id})


def normalize_name(name: str) -> str:
    return _NON_ALPHANUMERIC.sub("", name.lower())


def observation_window(now: datetime) -> tuple[int, int]:
    last = now.astimezone(UTC).year - 1
    return last - OBSERVATION_YEARS + 1, last


def compute_base_rate(
    hacks: Iterable[DefiLlamaHack],
    protocols: Iterable[DefiLlamaProtocol],
    *,
    now: datetime,
    incident_filter: Callable[[DefiLlamaHack], bool] | None = None,
) -> BaseRate | None:
    """The base rate over the window, or None when the population has no protocol-years.

    `incident_filter` restricts the counted incidents (exposure is unchanged).
    """
    first, last = observation_window(now)
    lending = {
        protocol.id: protocol for protocol in protocols if protocol.category in LENDING_CATEGORIES
    }
    population = tuple(
        protocol
        for protocol in lending.values()
        if protocol.tvl_usd is not None and protocol.tvl_usd >= TVL_FLOOR_USD
    )
    by_id = {protocol.id: protocol for protocol in population}
    by_name: dict[str, DefiLlamaProtocol] = {}
    for protocol in population:
        for name in (protocol.name, *protocol.previous_names):
            by_name.setdefault(normalize_name(name), protocol)

    incidents: list[MatchedIncident] = []
    outside: list[MatchedIncident] = []
    for hack in hacks:
        year = hack.occurred_at.astimezone(UTC).year
        if not first <= year <= last or not _material(hack):
            continue
        if incident_filter is not None and not incident_filter(hack):
            continue
        if hack.defillama_id is not None:
            protocol, matched_by = by_id.get(hack.defillama_id), "defillama_id"
            if protocol is None and hack.defillama_id in lending:
                outside.append(MatchedIncident(lending[hack.defillama_id], hack, matched_by))
        else:
            protocol, matched_by = by_name.get(normalize_name(hack.name)), "name"
        if protocol is not None:
            incidents.append(MatchedIncident(protocol, hack, matched_by))
    incidents.sort(key=lambda item: (item.hack.occurred_at, item.protocol.id))
    outside.sort(key=lambda item: (-item.hack.amount_usd, item.hack.occurred_at))

    event_keys = {(item.protocol.id, item.hack.occurred_at.year) for item in incidents}
    exposed = {
        (protocol.id, year)
        for protocol in population
        for year in range(first, last + 1)
        if _exposed(protocol, year) or (protocol.id, year) in event_keys
    }
    if not exposed:
        return None
    events = len(event_keys)
    trials = len(exposed)
    with localcontext() as context:
        context.prec = PRECISION
        central = Decimal(events) / Decimal(trials)
        low, high = wilson_interval(events, trials)
    return BaseRate(
        first_year=first,
        last_year=last,
        population=population,
        protocol_years=trials,
        event_years=events,
        incidents=tuple(incidents),
        annual_central=central,
        annual_low=low,
        annual_high=high,
        exposure=tuple(sorted(exposed)),
        below_floor_incidents=tuple(outside),
    )


def protocol_incidents(
    hacks: Iterable[DefiLlamaHack],
    protocols: Iterable[DefiLlamaProtocol],
    ref: DefiLlamaProtocolRef,
) -> tuple[DefiLlamaHack, ...]:
    """Every recorded incident of a protocol family, by id or by name token, at any size."""
    family = {
        protocol.id
        for protocol in protocols
        if protocol.parent_protocol == ref.parent or protocol.slug == ref.slug
    } | {ref.parent}
    token = normalize_name(ref.name_token)
    return tuple(
        sorted(
            (
                hack
                for hack in hacks
                if hack.defillama_id in family or token in normalize_name(hack.name)
            ),
            key=lambda hack: hack.occurred_at,
        )
    )


def base_rate_estimate(
    protocol: str,
    hacks: DataEnvelope[tuple[DefiLlamaHack, ...]],
    protocols: DataEnvelope[tuple[DefiLlamaProtocol, ...]],
    *,
    now: datetime,
) -> ProbabilityEstimate | None:
    """The 30-day impairment probability from the base rate, or None without protocol-years."""
    rate = compute_base_rate(hacks.value, protocols.value, now=now)
    if rate is None:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        central = annual_to_horizon(rate.annual_central)
        low = annual_to_horizon(rate.annual_low)
        high = annual_to_horizon(rate.annual_high)
    freshness = max((hacks.freshness, protocols.freshness), key=_FRESHNESS_ORDER.index)
    return ProbabilityEstimate(
        method=ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL,
        low=low,
        central=central,
        high=high,
        confidence=BASE_RATE_CONFIDENCE,
        horizon_days=HORIZON_DAYS,
        source=f"{hacks.source}; {protocols.source}",
        retrieved_at=min(hacks.fetched_at, protocols.fetched_at),
        freshness=freshness,
        explanation=_explanation(protocol, rate, central, low, high, hacks.value, protocols.value),
    )


def is_oracle_incident(hack: DefiLlamaHack) -> bool:
    """DeFiLlama classifies the incident as oracle manipulation or names an oracle technique."""
    classification = (hack.classification or "").strip().casefold()
    technique = (hack.technique or "").casefold()
    return classification == ORACLE_CLASSIFICATION or ORACLE_TECHNIQUE_MARKER in technique


def oracle_exploit_estimate(
    hacks: DataEnvelope[tuple[DefiLlamaHack, ...]],
    protocols: DataEnvelope[tuple[DefiLlamaProtocol, ...]],
    *,
    now: datetime,
) -> ProbabilityEstimate | None:
    """The 30-day oracle-exploit prior, or None without protocol-years."""
    rate = compute_base_rate(
        hacks.value, protocols.value, now=now, incident_filter=is_oracle_incident
    )
    if rate is None:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        central = annual_to_horizon(rate.annual_central)
        low = annual_to_horizon(rate.annual_low)
        high = annual_to_horizon(rate.annual_high)
    freshness = max((hacks.freshness, protocols.freshness), key=_FRESHNESS_ORDER.index)
    categories = " and ".join(sorted(LENDING_CATEGORIES, reverse=True))
    matched = "; ".join(
        f"{_incident(item.hack)} -> {item.protocol.name}" for item in rate.incidents
    ) or "none"
    explanation = " ".join(
        [
            f"Industry oracle-exploit prior ({ORACLE_PRIOR_MODEL_VERSION}) from DeFiLlama, not a "
            f"feed-specific estimate: {rate.event_years} protocol-years with a material "
            f"oracle-related exploit in {rate.protocol_years} protocol-years, "
            f"{rate.first_year}-{rate.last_year}.",
            f"Population: DeFiLlama {categories} protocols with current TVL >= "
            f"{_usd(TVL_FLOOR_USD)} on any chain ({len(rate.population)} protocols), the same "
            "population and exposure rule as the protocol-impairment base rate.",
            f"Event: a DeFiLlama /hacks incident classified \"Oracle Manipulation\" or with a "
            f"technique naming an oracle, gross loss >= {_usd(MATERIAL_LOSS_USD)}, matched to a "
            "population protocol by DeFiLlama id or exact normalized name.",
            f"Annual rate {_percent(rate.annual_central)} (Wilson 95%: "
            f"{_percent(rate.annual_low)} to {_percent(rate.annual_high)}), converted to 30 days "
            f"as 1 - (1 - annual)^(30/365): central {_percent(central, 3)}, range "
            f"{_percent(low, 3)} to {_percent(high, 3)}.",
            f"Matched incidents: {matched}.",
            "Caveats: survivorship (the population is selected on current TVL) and unmatched "
            "incident names push the rate down; an exploit is not the same event as a stale "
            "feed, so this stands in for a feed whose own update history cannot be measured. "
            "LOW confidence.",
        ]
    )
    return ProbabilityEstimate(
        method=ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL,
        low=low,
        central=central,
        high=high,
        confidence=BASE_RATE_CONFIDENCE,
        horizon_days=HORIZON_DAYS,
        source=f"{hacks.source}; {protocols.source}",
        retrieved_at=min(hacks.fetched_at, protocols.fetched_at),
        freshness=freshness,
        explanation=explanation,
    )


def annual_to_horizon(annual: Decimal, horizon_days: int = HORIZON_DAYS) -> Decimal:
    if annual >= 1:
        return Decimal(1)
    with localcontext() as context:
        context.prec = PRECISION
        return Decimal(1) - (Decimal(1) - annual) ** (Decimal(horizon_days) / Decimal(DAYS_PER_YEAR))


def protocol_tvl_snapshot(
    protocol: str,
    protocols: Iterable[DefiLlamaProtocol],
    chains: Iterable[DefiLlamaChain],
    *,
    chain: str = CHAIN,
) -> ProtocolTvlSnapshot | None:
    """The protocol's TVL and its share of the chain's DeFi TVL, or None if DeFiLlama lacks it."""
    ref = DEFILLAMA_PROTOCOLS.get(protocol)
    if ref is None:
        return None
    entry = protocol_by_slug(tuple(protocols), ref.slug)
    if entry is None:
        return None
    chain_entry = chain_by_name(tuple(chains), chain)
    chain_tvl = chain_entry.tvl_usd if chain_entry is not None else None
    protocol_chain_tvl = entry.ethereum_tvl_usd if chain == CHAIN else None
    share = None
    if protocol_chain_tvl is not None and chain_tvl is not None and chain_tvl > 0:
        with localcontext() as context:
            context.prec = PRECISION
            share = (protocol_chain_tvl / chain_tvl).quantize(SHARE_QUANTUM)
    return ProtocolTvlSnapshot(
        protocol=protocol,
        label=ref.label,
        chain=chain,
        tvl_usd=entry.tvl_usd,
        chain_tvl_usd=protocol_chain_tvl,
        chain_borrowed_usd=entry.ethereum_borrowed_usd if chain == CHAIN else None,
        chain_defi_tvl_usd=chain_tvl,
        chain_tvl_share=share,
    )


def _material(hack: DefiLlamaHack) -> bool:
    return hack.amount_usd is not None and hack.amount_usd >= MATERIAL_LOSS_USD


def _exposed(protocol: DefiLlamaProtocol, year: int) -> bool:
    if protocol.listed_at is None:
        return True
    month, day = EXPOSURE_CUTOFF
    return protocol.listed_at < datetime(year, month, day, tzinfo=UTC)


def _usd(value: Decimal | None) -> str:
    return "an unknown amount" if value is None else f"${value:,.0f}"


def _percent(value: Decimal, places: int = 2) -> str:
    return f"{value * 100:.{places}f}%"


def _incident(hack: DefiLlamaHack) -> str:
    kind = ": ".join(item for item in (hack.classification, hack.technique) if item)
    return (
        f"{hack.occurred_at.date().isoformat()} {hack.name} {_usd(hack.amount_usd)}"
        + (f" ({kind})" if kind else "")
    )


def _explanation(
    protocol: str,
    rate: BaseRate,
    central: Decimal,
    low: Decimal,
    high: Decimal,
    hacks: tuple[DefiLlamaHack, ...],
    protocols: tuple[DefiLlamaProtocol, ...],
) -> str:
    ref = DEFILLAMA_PROTOCOLS.get(protocol)
    label = ref.label if ref is not None else protocol
    categories = " and ".join(sorted(LENDING_CATEGORIES, reverse=True))
    matched = "; ".join(
        f"{_incident(item.hack)} -> {item.protocol.name} [by {item.matched_by.replace('_', ' ')}]"
        for item in rate.incidents
    ) or "none"
    parts = [
        f"Empirical industry base rate ({BASE_RATE_MODEL_VERSION}) from DeFiLlama, not a "
        f"{label}-specific score: {rate.event_years} material exploit years in "
        f"{rate.protocol_years} protocol-years, {rate.first_year}-{rate.last_year}.",
        f"Population: DeFiLlama {categories} protocols with current TVL >= "
        f"{_usd(TVL_FLOOR_USD)} on any chain ({len(rate.population)} protocols). "
        "A protocol-year counts when the protocol was listed on DeFiLlama before 1 July "
        "(unlisted-date protocols predate DeFiLlama's listing records and count every year).",
        f"Event: a DeFiLlama /hacks incident with gross loss >= {_usd(MATERIAL_LOSS_USD)} "
        "matched by DeFiLlama protocol id, or by exact normalized name when the incident has "
        "no id; one event per protocol-year.",
        f"Annual rate {_percent(rate.annual_central)} (Wilson 95%: "
        f"{_percent(rate.annual_low)} to {_percent(rate.annual_high)}), converted to 30 days "
        f"as 1 - (1 - annual)^(30/365): central {_percent(central, 3)}, range "
        f"{_percent(low, 3)} to {_percent(high, 3)}. This is the probability of an "
        "impairment event within 30 days; the scenario severity is the loss given the "
        "event and does not scale it.",
        f"Matched incidents: {matched}.",
    ]
    if ref is not None:
        own = protocol_incidents(hacks, protocols, ref)
        matched_hacks = {id(item.hack) for item in rate.incidents}
        notes = []
        for hack in own:
            flags = []
            if not _material(hack):
                flags.append("below the materiality threshold")
            if id(hack) not in matched_hacks:
                flags.append("not attributed to a population protocol by the matching rule")
            notes.append(_incident(hack) + (f" [{'; '.join(flags)}]" if flags else ""))
        entry = protocol_by_slug(protocols, ref.slug)
        years = rate.protocol_years_of(entry.id) if entry is not None else 0
        own_events = rate.event_years_of(entry.id) if entry is not None else 0
        parts.append(
            f"{ref.label} record in DeFiLlama /hacks: {'; '.join(notes) or 'no incidents'}. "
            f"{ref.label} itself contributes {years} protocol-years with {own_events} events."
        )
    survivorship = (
        "Caveats: the population is selected on current TVL, so protocols an exploit drained "
        "or closed drop out (survivorship bias, which lowers the rate; the rate is closer to a "
        "lower bound than a central estimate)"
    )
    if rate.below_floor_incidents:
        largest = "; ".join(_incident(item.hack) for item in rate.below_floor_incidents[:3])
        survivorship += (
            f": in the same window DeFiLlama links {len(rate.below_floor_incidents)} further "
            f"material incidents to {categories} protocols now below the floor (largest: "
            f"{largest}), which this population cannot count because their TVL at the time "
            "is not known"
        )
    parts.append(
        f"{survivorship}; incidents named differently from the protocol entry or recorded "
        "against another version are not matched (undercount); losses are gross of "
        "recoveries. LOW confidence."
    )
    return " ".join(parts)
