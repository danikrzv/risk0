from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest

from app.adapters.defillama import DefiLlamaHack, DefiLlamaProtocol
from app.contracts.models import ConfidenceLevel, FreshnessStatus, ProbabilityMethod
from app.evidence.defillama import (
    MATERIAL_LOSS_USD,
    TVL_FLOOR_USD,
    annual_to_horizon,
    base_rate_estimate,
    compute_base_rate,
    normalize_name,
    observation_window,
    protocol_incidents,
    protocol_tvl_snapshot,
    DEFILLAMA_PROTOCOLS,
)
from app.history.estimator import _wilson_interval
from tests.fixtures.defillama.recorded import FakeDefiLlamaApi


NOW = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)


@pytest.fixture
def data(clock):
    client = FakeDefiLlamaApi().client(clock)
    return client.hacks(), client.protocols(), client.chains()


def protocol(identifier, name, *, tvl="200000000", category="Lending", listed=None, previous=()):
    return DefiLlamaProtocol(
        id=identifier,
        name=name,
        slug=normalize_name(name),
        category=category,
        tvl_usd=Decimal(tvl),
        listed_at=listed,
        previous_names=previous,
    )


def hack(name, year, amount="5000000", *, defillama_id=None, month=6):
    return DefiLlamaHack(
        occurred_at=datetime(year, month, 1, tzinfo=UTC),
        name=name,
        amount_usd=None if amount is None else Decimal(amount),
        defillama_id=defillama_id,
    )


def test_window_is_the_last_five_complete_years():
    assert observation_window(NOW) == (2021, 2025)
    assert observation_window(datetime(2027, 1, 1, tzinfo=UTC)) == (2022, 2026)


def test_recorded_base_rate_counts_protocol_years_and_event_years(data):
    hacks, protocols, _ = data

    rate = compute_base_rate(hacks.value, protocols.value, now=NOW)

    # Population: Aave V3, Morpho Blue, Compound V2, Dolomite, Venus Core Pool,
    # Sky Lending (CDP), JustLend V1. Excluded: Morpho Midnight and Euler V1 (below
    # the floor), Uniswap V3 (a DEX).
    assert sorted(item.name for item in rate.population) == [
        "Aave V3",
        "Compound V2",
        "Dolomite",
        "JustLend V1",
        "Morpho Blue",
        "Sky Lending",
        "Venus Core Pool",
    ]
    # Aave V3 listed 2022-04 -> 2022-2025 (4); Morpho Blue 2024-01 -> 2 years;
    # Dolomite listed 2022-10 (after 1 July) -> 2023-2025 (3); four protocols
    # without a listing date -> 5 each.
    assert rate.protocol_years == 4 + 2 + 3 + 5 * 4
    assert rate.protocol_years_of("4025") == 2
    # Compound V2 2021 ($147M, by id) and Dolomite 2024 ($1.8M, by name). Venus
    # 2025 ($902k) is below the threshold; "Venus Protocol" does not match
    # "Venus Core Pool"; MakerDAO 2020 and Aave V3/Venus 2026 are outside the window.
    assert [(item.protocol.name, item.matched_by) for item in rate.incidents] == [
        ("Compound V2", "defillama_id"),
        ("Dolomite", "name"),
    ]
    assert rate.event_years == 2
    # Euler V1 (2023, $197M) is a lending protocol now below the floor: the
    # survivorship gap is reported, not counted.
    assert [(item.protocol.name, item.hack.amount_usd) for item in rate.below_floor_incidents] == [
        ("Euler V1", Decimal("197000000"))
    ]
    assert rate.annual_central == Decimal(2) / Decimal(29)
    assert (rate.annual_low, rate.annual_high) == _wilson_interval(2, 29)


def test_population_filter_uses_category_and_tvl_floor():
    protocols = [
        protocol("1", "Floor Lender", tvl=str(TVL_FLOOR_USD)),
        protocol("2", "Small Lender", tvl=str(TVL_FLOOR_USD - 1)),
        protocol("3", "Stable CDP", category="CDP"),
        protocol("4", "Big Dex", category="Dexs"),
    ]

    rate = compute_base_rate([], protocols, now=NOW)

    assert {item.name for item in rate.population} == {"Floor Lender", "Stable CDP"}
    assert rate.protocol_years == 10
    assert rate.event_years == 0
    assert rate.annual_central == 0
    assert rate.annual_high > 0


def test_materiality_threshold_is_inclusive_and_unknown_amounts_are_not_material():
    protocols = [protocol("1", "Alpha"), protocol("2", "Beta"), protocol("3", "Gamma")]
    hacks = [
        hack("Alpha", 2022, str(MATERIAL_LOSS_USD), defillama_id="1"),
        hack("Beta", 2022, str(MATERIAL_LOSS_USD - 1), defillama_id="2"),
        hack("Gamma", 2022, None, defillama_id="3"),
    ]

    rate = compute_base_rate(hacks, protocols, now=NOW)

    assert [item.protocol.name for item in rate.incidents] == ["Alpha"]


def test_several_incidents_in_one_year_are_one_event_year():
    protocols = [protocol("1", "Alpha")]
    hacks = [
        hack("Alpha", 2023, defillama_id="1", month=2),
        hack("Alpha", 2023, defillama_id="1", month=9),
        hack("Alpha", 2024, defillama_id="1"),
    ]

    rate = compute_base_rate(hacks, protocols, now=NOW)

    assert len(rate.incidents) == 3
    assert rate.event_years == 2
    assert rate.event_years_of("1") == 2


def test_matching_rule_by_id_then_exact_normalized_name_or_previous_name():
    protocols = [
        protocol("1", "Alpha Lend"),
        protocol("2", "Beta Money Market", previous=("Beta V1",)),
    ]
    hacks = [
        hack("ALPHA-lend", 2022),  # no id: normalized name matches
        hack("Beta v1", 2023),  # no id: previous name matches
        hack("Alpha Lend", 2024, defillama_id="999"),  # an id outside the population never name-matches
        hack("Alpha Lend Fork", 2025),  # not an exact name
    ]

    rate = compute_base_rate(hacks, protocols, now=NOW)

    assert [(item.protocol.id, item.hack.occurred_at.year, item.matched_by) for item in rate.incidents] == [
        ("1", 2022, "name"),
        ("2", 2023, "name"),
    ]


def test_exposure_starts_at_the_mid_year_listing_cutoff_and_includes_incident_years():
    protocols = [
        protocol("1", "Early", listed=datetime(2023, 6, 30, tzinfo=UTC)),
        protocol("2", "Late", listed=datetime(2023, 7, 1, tzinfo=UTC)),
    ]
    # An incident recorded before the listing date still counts its year.
    hacks = [hack("Late", 2022, defillama_id="2")]

    rate = compute_base_rate(hacks, protocols, now=NOW)

    assert rate.protocol_years_of("1") == 3  # 2023-2025
    assert rate.protocol_years_of("2") == 3  # 2022 (incident), 2024, 2025
    assert rate.event_years == 1


def test_no_population_means_no_base_rate():
    assert compute_base_rate([], [protocol("1", "Tiny", tvl="1")], now=NOW) is None


def test_estimate_is_a_low_confidence_30_day_conversion_of_the_annual_rate(data):
    hacks, protocols, _ = data

    estimate = base_rate_estimate("morpho", hacks, protocols, now=NOW)

    low, high = _wilson_interval(2, 29)
    with localcontext() as context:
        context.prec = 28
        expected = 1 - (1 - Decimal(2) / Decimal(29)) ** (Decimal(30) / Decimal(365))
    assert estimate.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert estimate.confidence is ConfidenceLevel.LOW
    assert estimate.horizon_days == 30
    assert estimate.central == expected
    assert estimate.low == annual_to_horizon(low)
    assert estimate.high == annual_to_horizon(high)
    assert estimate.low < estimate.central < estimate.high
    assert estimate.freshness is FreshnessStatus.FRESH
    assert estimate.source == (
        "defillama https://api.llama.fi/hacks; defillama https://api.llama.fi/protocols"
    )
    assert estimate.retrieved_at == hacks.fetched_at


def test_estimate_explanation_states_method_counts_record_and_caveats(data):
    hacks, protocols, _ = data

    text = base_rate_estimate("morpho", hacks, protocols, now=NOW).explanation

    for expected in (
        "Empirical industry base rate (protocol-base-rate-v1)",
        "not a Morpho Blue-specific score",
        "2 material exploit years in 29 protocol-years, 2021-2025",
        "Lending and CDP protocols with current TVL >= $100,000,000",
        "gross loss >= $1,000,000",
        "Annual rate 6.90% (Wilson 95%:",
        "1 - (1 - annual)^(30/365)",
        "the scenario severity is the loss given the event and does not scale it",
        "2021-09-29 Compound V2 $147,000,000",
        "2024-03-20 Dolomite $1,800,000",
        "2024-10-14 LeadBlock's Morpho Blue Market $250,000 (Oracle Manipulation: Oracle "
        "Misconfiguration) [below the materiality threshold; not attributed",
        "Morpho Blue itself contributes 2 protocol-years with 0 events",
        "survivorship bias",
        "DeFiLlama links 1 further material incidents to Lending and CDP protocols now below "
        "the floor (largest: 2023-03-13 Euler V1 $197,000,000",
        "LOW confidence",
    ):
        assert expected in text


def test_estimate_freshness_is_the_worst_input_freshness(data):
    hacks, protocols, _ = data
    stale = protocols.model_copy(update={"freshness": FreshnessStatus.STALE_USABLE})

    estimate = base_rate_estimate("morpho", hacks, stale, now=NOW)

    assert estimate.freshness is FreshnessStatus.STALE_USABLE


def test_protocol_incidents_lists_the_family_record_at_any_size(data):
    hacks, protocols, _ = data

    incidents = protocol_incidents(hacks.value, protocols.value, DEFILLAMA_PROTOCOLS["morpho"])

    assert [(item.name, item.amount_usd) for item in incidents] == [
        ("LeadBlock's Morpho Blue Market", Decimal("250000"))
    ]


def test_annual_to_horizon_matches_the_scoring_conversion():
    assert annual_to_horizon(Decimal(0)) == 0
    assert annual_to_horizon(Decimal(1)) == 1
    with localcontext() as context:
        context.prec = 28
        assert annual_to_horizon(Decimal("0.1")) == 1 - Decimal("0.9") ** (Decimal(30) / Decimal(365))


def test_protocol_tvl_snapshot_reports_chain_share(data):
    _, protocols, chains = data

    snapshot = protocol_tvl_snapshot("morpho", protocols.value, chains.value)

    assert snapshot.label == "Morpho Blue"
    assert snapshot.chain == "Ethereum"
    assert snapshot.tvl_usd == Decimal("11268588686.268003")
    assert snapshot.chain_tvl_usd == Decimal("5165760890.8268795")
    assert snapshot.chain_defi_tvl_usd == Decimal("51524257395.28339")
    assert snapshot.chain_tvl_share == Decimal("0.100259")
    assert snapshot.chain_gross_deposits_usd == Decimal("5165760890.8268795") + Decimal(
        "2605029925.226604"
    )


def test_protocol_tvl_snapshot_without_data_is_none_or_partial(data):
    _, protocols, chains = data

    assert protocol_tvl_snapshot("unknown", protocols.value, chains.value) is None
    assert protocol_tvl_snapshot("morpho", (), chains.value) is None
    partial = protocol_tvl_snapshot("morpho", protocols.value, ())
    assert partial.chain_defi_tvl_usd is None
    assert partial.chain_tvl_share is None
    assert partial.chain_tvl_usd is not None
    no_borrowed = partial.model_copy(update={"chain_borrowed_usd": None})
    assert no_borrowed.chain_gross_deposits_usd is None
