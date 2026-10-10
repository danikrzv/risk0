import json
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def repository():
    from app.catalog.repository import CatalogRepository

    return CatalogRepository(ROOT / "data" / "catalog" / "ethereum.json")


ORIGINAL_ENTITY_IDS = [
    "belem-rwa-eurcv",
    "mev-capital-weth",
    "wsteth-weth-965",
    "weth-usdc-860",
    "wbtc-usdc-860",
]
ROCKAWAYX_VAULTS = [
    ("rockawayx-confidential-rwa-usdc", "0xc207d3f66537D7F66456808379A0E560cF20Da36"),
    ("rockawayx-fx-protocol-usdc", "0x2cA22cb25558fa2018ecb1CE4eD8AF92Ee7ea423"),
    ("rockawayx-yield-usdc", "0xE0181090c22579B6A217f1522cbf8c9f1F0C1965"),
    ("rockawayx-tori-ecosystem", "0x3BD9AdAE6643dDcddD02746b8B60075E56DF9478"),
    ("huma-usdc-main", "0x8aC91877b93330f52b2979a31a4879506021475c"),
    ("rockawayx-weth", "0x64C18DCC4Ccb3b8D27877a4aeBB4C3126CB39cB9"),
    ("rockawayx-nuva-ecosystem", "0x93e0F9d502eEfce8D34924ab3478C7EA0CBC5a2E"),
    ("rockawayx-prime-usdc", "0x5f829B1B473cBA86838E1B7BB7E144DbDE228e21"),
    ("midas-usdc-prime-ethereum", "0xe99A27169c2aA26a8f2757949d09Fa3f9A8f0B3B"),
    ("figure-usdc-main", "0xd65d6E8dbC3Cd3D12418199E6f4014dB3aaa0097"),
    ("rockawayx-europ", "0x9C57E02bC31e7e463f439353D97bd8f42F5a7F9c"),
    ("midas-usdc-prime-base", "0xAE4181CFB5aaA08bbE77d269c6B595672b9F9Edc"),
]
ROCKAWAYX_ENTITY_IDS = [entity_id for entity_id, _ in ROCKAWAYX_VAULTS]
BASE_CHAIN_ID = 8453


def test_catalog_contains_exact_approved_entities(repository):
    entries = repository.list_entries()
    assert [entry.entity_id for entry in entries] == [
        *ORIGINAL_ENTITY_IDS,
        *ROCKAWAYX_ENTITY_IDS,
    ]
    assert all(entry.chain_id in (1, BASE_CHAIN_ID) for entry in entries)
    assert entries[0].provenance == "verified_belem"
    assert entries[2].provenance == "representative_proxy"


def test_catalog_ids_and_addresses_are_unique(repository):
    entries = repository.list_entries()

    assert len({entry.entity_id for entry in entries}) == len(entries)
    assert len({entry.address_or_market_id.lower() for entry in entries}) == len(
        entries
    )


def test_only_the_base_vault_is_outside_mainnet(repository):
    entries = repository.list_entries()

    assert [
        entry.entity_id for entry in entries if entry.chain_id == BASE_CHAIN_ID
    ] == ["midas-usdc-prime-base"]
    assert all(
        entry.chain_id == 1
        for entry in entries
        if entry.entity_id != "midas-usdc-prime-base"
    )


def test_base_vault_is_listed_but_unsupported_with_mainnet_only_note(repository):
    base = {entry.entity_id: entry for entry in repository.list_entries()}[
        "midas-usdc-prime-base"
    ]

    assert base.supported is False
    assert "mainnet-only" in base.description.lower()


def test_rockawayx_entries_are_lender_only_v2_vaults(repository):
    entries = {entry.entity_id: entry for entry in repository.list_entries()}

    for entity_id, address in ROCKAWAYX_VAULTS:
        entry = entries[entity_id]
        assert entry.address_or_market_id == address
        assert entry.entity_type == "morpho_vault_v2"
        assert entry.provenance == "rockawayx_curated"
        assert "RockawayX" in entry.provenance_label
        assert entry.supported_modes == ("lender",)
        assert entry.lltv is None
    assert all(
        entries[entity_id].supported
        for entity_id in ROCKAWAYX_ENTITY_IDS
        if entity_id != "midas-usdc-prime-base"
    )


def test_original_entries_preserve_exact_identifiers_and_lltvs(repository):
    entries = repository.list_entries()[: len(ORIGINAL_ENTITY_IDS)]

    assert [entry.address_or_market_id for entry in entries] == [
        "0xD6d4b804014EF27836dBe9f8f6Bf6c71251548Ec",
        "0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8",
        "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e",
        "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd",
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
    ]
    assert all(entry.chain_id == 1 for entry in entries)
    assert [entry.lltv for entry in entries] == [
        None,
        None,
        Decimal("0.965"),
        Decimal("0.86"),
        Decimal("0.86"),
    ]
    assert [entry.provenance for entry in entries] == [
        "verified_belem",
        "belem_distributed",
        "representative_proxy",
        "representative_proxy",
        "representative_proxy",
    ]


def test_default_scenarios_have_approved_ids_and_severities():
    with (ROOT / "data" / "scenarios" / "default.json").open() as scenario_file:
        scenarios = json.load(scenario_file)["scenarios"]

    assert [(scenario["scenario_id"], scenario["severity"]) for scenario in scenarios] == [
        ("stablecoin-depeg", "0.10"),
        ("collateral-shock", "0.15"),
        ("oracle-failure", "0.05"),
        ("liquidity-collapse", "0.80"),
        ("protocol-impairment", "0.10"),
    ]
