import json
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def repository():
    from app.catalog.repository import CatalogRepository

    return CatalogRepository(ROOT / "data" / "catalog" / "ethereum.json")


def test_catalog_contains_exact_approved_entities(repository):
    entries = repository.list_entries()
    assert [entry.entity_id for entry in entries] == [
        "belem-rwa-eurcv",
        "mev-capital-weth",
        "wsteth-weth-965",
        "weth-usdc-860",
        "wbtc-usdc-860",
    ]
    assert all(entry.chain_id == 1 for entry in entries)
    assert entries[0].provenance == "verified_belem"
    assert entries[2].provenance == "representative_proxy"


def test_catalog_preserves_exact_identifiers_and_lltvs(repository):
    entries = repository.list_entries()

    assert [entry.address_or_market_id for entry in entries] == [
        "0xD6d4b804014EF27836dBe9f8f6Bf6c71251548Ec",
        "0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8",
        "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e",
        "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd",
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
    ]
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
