from datetime import UTC, datetime
from decimal import Decimal

from app.radar.analysis.similarity import VaultProfile, VaultSimilarity


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def profile(entity_id: str, **overrides) -> VaultProfile:
    values = {
        "entity_id": entity_id,
        "market_allocations": {"market-a": Decimal("1")},
        "loan_assets": frozenset({"USDC"}),
        "collateral_assets": frozenset({"WETH"}),
        "oracle_groups": frozenset({"ETH/USD"}),
        "irms": frozenset({"adaptive-curve"}),
        "lltvs": (Decimal("0.86"),),
        "chain_id": 1,
        "curator": "rockawayx",
    }
    values.update(overrides)
    return VaultProfile(**values)


def test_identical_profiles_receive_every_weighted_component():
    record = VaultSimilarity().score(
        profile("vault:1:catalog"), profile("vault:1:candidate")
    )

    assert record.score == Decimal("1.00")
    assert record.components == {
        "shared_markets": Decimal("0.35"),
        "asset_overlap": Decimal("0.20"),
        "oracle_overlap": Decimal("0.15"),
        "irm_match": Decimal("0.10"),
        "lltv_proximity": Decimal("0.10"),
        "same_chain": Decimal("0.05"),
        "same_curator": Decimal("0.05"),
    }
    assert record.stored is True
    assert record.normally_visible is True


def test_missing_dimensions_contribute_zero_and_are_explained():
    record = VaultSimilarity().score(
        profile("vault:1:catalog"),
        profile(
            "vault:1:candidate",
            oracle_groups=frozenset(),
            irms=frozenset(),
            lltvs=(),
            curator=None,
        ),
    )

    assert record.score == Decimal("0.60")
    assert record.missing_dimensions == (
        "oracle_overlap",
        "irm_match",
        "lltv_proximity",
        "same_curator",
    )
    assert record.stored is True
    assert record.normally_visible is False


def test_thresholds_are_strict_and_visible_selection_is_top_ten_deterministically():
    similarity = VaultSimilarity(clock=lambda: NOW)
    catalog = profile("vault:1:catalog")
    records = []
    for index in range(12):
        candidate = profile(
            f"vault:1:candidate-{index:02d}",
            curator="other" if index == 11 else "rockawayx",
        )
        records.append(similarity.score(catalog, candidate))

    boundary = similarity.score(
        catalog,
        profile(
            "vault:10:boundary",
            market_allocations={},
            loan_assets=frozenset({"USDC"}),
            collateral_assets=frozenset(),
            oracle_groups=frozenset(),
            irms=frozenset(),
            lltvs=(),
            chain_id=10,
            curator=None,
        ),
    ).model_copy(update={"score": Decimal("0.33"), "stored": False, "normally_visible": False})

    selected = similarity.select_neighbors(catalog.entity_id, (*records, boundary))

    assert len(selected.visible) == 10
    assert [item.candidate_entity_id for item in selected.visible] == [
        f"vault:1:candidate-{index:02d}" for index in range(10)
    ]
    assert boundary not in selected.stored


def test_weighted_market_overlap_uses_allocations():
    record = VaultSimilarity().score(
        profile(
            "vault:1:catalog",
            market_allocations={"a": Decimal("0.75"), "b": Decimal("0.25")},
        ),
        profile(
            "vault:1:candidate",
            market_allocations={"a": Decimal("0.25"), "c": Decimal("0.75")},
        ),
    )

    assert record.components["shared_markets"] == Decimal("0.05")


def test_market_overlap_is_normalized_by_each_vaults_allocated_total():
    record = VaultSimilarity().score(
        profile("vault:1:catalog", market_allocations={"a": Decimal("0.60")}),
        profile("vault:1:candidate", market_allocations={"a": Decimal("1")}),
    )

    assert record.components["shared_markets"] == Decimal("0.35")


def test_thresholds_compare_the_unrounded_total():
    sparse = {
        "oracle_groups": frozenset(),
        "irms": frozenset(),
        "lltvs": (),
        "curator": None,
    }
    # shared_markets 0.35 * 0.89 / 1.11 = 0.2806..., same_chain 0.05: total 0.3306...
    record = VaultSimilarity().score(
        profile(
            "vault:1:catalog",
            market_allocations={"a": Decimal("1")},
            loan_assets=frozenset({"USDC"}),
            collateral_assets=frozenset({"WETH"}),
            **sparse,
        ),
        profile(
            "vault:1:candidate",
            market_allocations={"a": Decimal("0.89"), "b": Decimal("0.11")},
            loan_assets=frozenset({"DAI"}),
            collateral_assets=frozenset({"WBTC"}),
            **sparse,
        ),
    )

    assert Decimal("0.33") < record.score < Decimal("0.335")
    assert record.stored is True


def test_score_just_above_storage_threshold_is_stored_and_selected():
    from app.radar.contracts import SimilarityRecord

    similarity = VaultSimilarity(clock=lambda: NOW)
    record = SimilarityRecord(
        catalog_entity_id="vault:1:catalog",
        candidate_entity_id="vault:1:near",
        score=Decimal("0.3349"),
        components={},
        stored=True,
        normally_visible=False,
        computed_at=NOW,
    )

    selected = similarity.select_neighbors("vault:1:catalog", (record,))

    assert [item.candidate_entity_id for item in selected.stored] == ["vault:1:near"]


def test_select_neighbors_drops_self_records_and_duplicate_candidates():
    similarity = VaultSimilarity(clock=lambda: NOW)
    catalog = profile("vault:1:catalog")
    good = similarity.score(catalog, profile("vault:1:candidate"))
    weaker = good.model_copy(update={"score": Decimal("0.40")})
    self_record = good.model_copy(update={"candidate_entity_id": "vault:1:catalog"})

    selected = similarity.select_neighbors(
        "vault:1:catalog", (weaker, good, self_record, good)
    )

    assert [item.candidate_entity_id for item in selected.stored] == ["vault:1:candidate"]
    assert selected.stored[0].score == good.score
    assert len(selected.visible) == 1
