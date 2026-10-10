import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.main import app
from tests.conftest import MORPHO_BASE_URL
from tests.fixtures.morpho.rockawayx import BASE_BLOCK, ROCKAWAYX_BY_ID


ROOT = Path(__file__).resolve().parents[4]
CATALOG_PATH = ROOT / "data" / "catalog" / "ethereum.json"
CATALOG_SCHEMA_PATH = (
    ROOT / "packages" / "contracts" / "schemas" / "catalog.schema.json"
)
ENTITY_IDS = [
    "belem-rwa-eurcv",
    "mev-capital-weth",
    "wsteth-weth-965",
    "weth-usdc-860",
    "wbtc-usdc-860",
    "rockawayx-confidential-rwa-usdc",
    "rockawayx-fx-protocol-usdc",
    "rockawayx-yield-usdc",
    "rockawayx-tori-ecosystem",
    "huma-usdc-main",
    "rockawayx-weth",
    "rockawayx-nuva-ecosystem",
    "rockawayx-prime-usdc",
    "midas-usdc-prime-ethereum",
    "figure-usdc-main",
    "rockawayx-europ",
    "midas-usdc-prime-base",
]
BASE_ENTITY_ID = "midas-usdc-prime-base"
MAINNET_ENTITY_IDS = [entity_id for entity_id in ENTITY_IDS if entity_id != BASE_ENTITY_ID]
ADVISORY_WORDS = ("STAY", "EXIT", "should", "recommend")
WBTC_USDC = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
WETH_USDC = "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"


def build_service(catalog_path, morpho_client, clock):
    from app.catalog.repository import CatalogRepository
    from app.catalog.service import CatalogService

    return CatalogService(CatalogRepository(catalog_path), morpho_client, clock=clock)


@pytest.fixture
def api(morpho_client, clock):
    from app.api.catalog import get_catalog_service

    def use_service(service):
        app.dependency_overrides[get_catalog_service] = lambda: service
        return TestClient(app)

    use_service.default = build_service(CATALOG_PATH, morpho_client, clock)
    yield use_service
    app.dependency_overrides.clear()


def entries_by_id(response):
    assert response.status_code == 200
    return {entry["entity_id"]: entry for entry in response.json()["entries"]}


def write_catalog(tmp_path, mutate):
    catalog = json.loads(CATALOG_PATH.read_text())
    for entry in catalog["entries"]:
        mutate(entry)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    return path


def test_catalog_hydrates_all_entries_with_fresh_live_data(api, clock):
    response = api(api.default).get("/v1/catalog")
    payload = response.json()
    Draft202012Validator(json.loads(CATALOG_SCHEMA_PATH.read_text())).validate(payload)
    assert payload["schema_version"] == "1.0.0"
    entries = entries_by_id(response)

    assert list(entries) == ENTITY_IDS
    for entity_id in MAINNET_ENTITY_IDS:
        entry = entries[entity_id]
        assert entry["analysis_available"] is True
        assert entry["availability"] == "available"
        assert entry["freshness"] == "fresh"
        assert entry["block_number"] == 26161049
        assert entry["source"].startswith(f"{MORPHO_BASE_URL}/")
        assert entry["fetched_at"].startswith("2026-10-10T09:15:00")
        assert entry["warnings"] == []

    assert entries["belem-rwa-eurcv"]["provenance"] == "verified_belem"
    assert entries["wsteth-weth-965"]["provenance"] == "representative_proxy"
    assert entries["weth-usdc-860"]["metrics"] == {
        "asset_symbol": "USDC",
        "collateral_symbol": "WETH",
        "total_assets": "9408096.071094",
        "total_assets_usd": "9405598.688924348",
        "liquidity_assets": "1045943.100938",
        "liquidity_usd": "1045681.2413280633",
        "utilization": entries["weth-usdc-860"]["metrics"]["utilization"],
    }
    assert entries["weth-usdc-860"]["metrics"]["utilization"].startswith("0.88882")
    assert entries["mev-capital-weth"]["metrics"]["total_assets"] == (
        "1857.124314225046976544"
    )
    assert entries["mev-capital-weth"]["metrics"]["utilization"] is None


def test_catalog_schema_rejects_undeclared_hydration_fields(api):
    payload = api(api.default).get("/v1/catalog").json()
    payload["entries"][0]["undeclared"] = True

    with pytest.raises(ValidationError):
        Draft202012Validator(json.loads(CATALOG_SCHEMA_PATH.read_text())).validate(
            payload
        )


@pytest.mark.parametrize(
    ("entity_id", "mutate", "missing_field"),
    [
        (
            "weth-usdc-860",
            lambda api: api.market_graphql(WETH_USDC)["loanAsset"].update(
                {"price": None}
            ),
            "loan_asset.price_usd",
        ),
        (
            "weth-usdc-860",
            lambda api: api.market_graphql(WETH_USDC)["state"].pop(
                "liquidityAssetsUsd"
            ),
            "liquidity_usd",
        ),
        (
            "mev-capital-weth",
            lambda api: api.vault_graphql(
                "v1", "0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8"
            ).update({"listed": None}),
            "listed",
        ),
    ],
)
def test_partial_provider_payload_disables_analysis(
    api, morpho_api, entity_id, mutate, missing_field
):
    mutate(morpho_api)

    entry = entries_by_id(api(api.default).get("/v1/catalog"))[entity_id]

    assert entry["analysis_available"] is False
    assert entry["availability"] == "critical_data_missing"
    assert any(missing_field in warning for warning in entry["warnings"])


def test_provider_failure_with_cache_returns_stale_values_and_age(
    api, morpho_api, clock
):
    service = api.default
    fresh = service.list_hydrated()
    clock.advance(timedelta(minutes=5))
    morpho_api.mode = "down"

    entries = entries_by_id(api(service).get("/v1/catalog"))

    for entity_id, previous in zip(ENTITY_IDS, fresh.entries, strict=True):
        entry = entries[entity_id]
        assert entry["analysis_available"] is (entity_id != BASE_ENTITY_ID)
        assert entry["freshness"] == "stale_usable"
        assert entry["age_seconds"] == 300
        assert entry["fetched_at"].startswith("2026-10-10T09:15:00")
        assert entry["block_number"] == previous.block_number
        assert entry["metrics"]["total_assets"] == str(previous.metrics.total_assets)
        assert any("stale" in warning for warning in entry["warnings"])


def test_provider_failure_without_cache_never_fabricates_metrics(api, morpho_api):
    morpho_api.mode = "down"

    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    assert list(entries) == ENTITY_IDS
    for entity_id, entry in entries.items():
        assert entry["analysis_available"] is False
        assert entry["availability"] == (
            "catalog_inactive" if entity_id == BASE_ENTITY_ID else "provider_unavailable"
        )
        assert entry["metrics"] is None
        assert entry["freshness"] is None
        assert entry["block_number"] is None
        assert entry["warnings"]
        assert entry["provenance_label"]


def test_provider_failure_after_24_hours_does_not_use_expired_cache(
    api, morpho_api, clock
):
    service = api.default
    service.list_hydrated()
    clock.advance(timedelta(hours=24))
    morpho_api.mode = "down"

    entries = entries_by_id(api(service).get("/v1/catalog"))

    assert all(entry["analysis_available"] is False for entry in entries.values())
    assert all(
        entries[entity_id]["availability"] == "provider_unavailable"
        for entity_id in MAINNET_ENTITY_IDS
    )
    assert all(entry["freshness"] is None for entry in entries.values())


def test_inactive_entry_remains_identifiable_but_analysis_disabled(
    api, morpho_api, morpho_client, clock, tmp_path
):
    def deactivate(entry):
        if entry["entity_id"] == "wbtc-usdc-860":
            entry["supported"] = False

    service = build_service(write_catalog(tmp_path, deactivate), morpho_client, clock)

    entries = entries_by_id(api(service).get("/v1/catalog"))

    inactive = entries["wbtc-usdc-860"]
    assert inactive["address_or_market_id"] == WBTC_USDC
    assert inactive["supported"] is False
    assert inactive["analysis_available"] is False
    assert inactive["availability"] == "catalog_inactive"
    # Inactive entries are still hydrated for display; analysis stays disabled.
    assert inactive["metrics"]["asset_symbol"] == "USDC"
    assert any("inactive" in warning for warning in inactive["warnings"])
    assert entries["weth-usdc-860"]["analysis_available"] is True


def test_inactive_entry_provider_failure_keeps_inactive_reason(
    api, morpho_api, morpho_client, clock, tmp_path
):
    def deactivate(entry):
        if entry["entity_id"] == "wbtc-usdc-860":
            entry["supported"] = False

    morpho_api.remove_market(WBTC_USDC)
    service = build_service(write_catalog(tmp_path, deactivate), morpho_client, clock)

    inactive = entries_by_id(api(service).get("/v1/catalog"))["wbtc-usdc-860"]

    assert inactive["analysis_available"] is False
    assert inactive["availability"] == "catalog_inactive"
    assert inactive["metrics"] is None
    assert len(inactive["warnings"]) == 2


def test_removed_market_remains_listed_with_analysis_disabled(api, morpho_api):
    morpho_api.remove_market(WBTC_USDC)

    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    removed = entries["wbtc-usdc-860"]
    assert removed["analysis_available"] is False
    assert removed["availability"] == "not_found"
    assert removed["metrics"] is None
    assert removed["name"] == "WBTC collateral / USDC debt"


def test_delisted_market_disables_analysis_but_keeps_metrics(api, morpho_api):
    morpho_api.market_graphql(WETH_USDC)["listed"] = False

    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    delisted = entries["weth-usdc-860"]
    assert delisted["analysis_available"] is False
    assert delisted["availability"] == "delisted"
    assert delisted["metrics"]["total_assets"] == "9408096.071094"


def test_catalog_lltv_conflict_disables_analysis(
    api, morpho_client, clock, tmp_path
):
    def change_lltv(entry):
        if entry["entity_id"] == "weth-usdc-860":
            entry["lltv"] = "0.915"

    service = build_service(write_catalog(tmp_path, change_lltv), morpho_client, clock)

    entries = entries_by_id(api(service).get("/v1/catalog"))

    conflicted = entries["weth-usdc-860"]
    assert conflicted["analysis_available"] is False
    assert conflicted["availability"] == "data_conflict"
    assert any("lltv" in warning.lower() for warning in conflicted["warnings"])


def test_base_vault_is_listed_hydrated_on_base_with_analysis_disabled(
    api, morpho_api
):
    base_vault = ROCKAWAYX_BY_ID[BASE_ENTITY_ID]

    entry = entries_by_id(api(api.default).get("/v1/catalog"))[BASE_ENTITY_ID]

    assert entry["chain_id"] == 8453
    assert entry["supported"] is False
    assert entry["analysis_available"] is False
    assert entry["availability"] == "catalog_inactive"
    assert entry["block_number"] == BASE_BLOCK
    assert entry["source"] == f"{MORPHO_BASE_URL}{base_vault.state_path}"
    assert Decimal(entry["metrics"]["total_assets"]) == Decimal("585516.841575")
    assert entry["metrics"]["asset_symbol"] == "USDC"
    (reason,) = entry["warnings"]
    assert "mainnet-only" in reason
    assert not any(word in reason for word in ADVISORY_WORDS)
    base_requests = [
        request for request in morpho_api.requests if base_vault.address in str(request.url)
        or base_vault.address in request.content.decode()
    ]
    assert base_requests
    for request in base_requests:
        if request.method == "GET":
            assert request.url.path.split("/")[3].startswith("8453:")
        else:
            assert json.loads(request.content)["variables"]["chainId"] == 8453


def test_each_entry_dispatches_to_its_own_type_version_and_chain(api, morpho_api):
    api(api.default).get("/v1/catalog")
    catalog = json.loads(CATALOG_PATH.read_text())["entries"]
    rest_paths = {
        request.url.path for request in morpho_api.requests if request.method == "GET"
    }
    # Entries hydrate concurrently, so requests are matched by identity, not order.
    graphql = {
        (
            body["variables"].get("marketId") or body["variables"].get("address"),
            body["variables"]["chainId"],
        ): body
        for body in morpho_api.graphql_requests()
    }

    assert len(graphql) == len(morpho_api.graphql_requests()) == len(catalog)
    for entry in catalog:
        identifier = entry["address_or_market_id"]
        selector = f"{entry['chain_id']}:{identifier}"
        body = graphql[(identifier, entry["chain_id"])]
        if entry["entity_type"] == "morpho_market":
            expected_paths = {f"/v0/blue/markets/{selector}"}
            root, variable = "marketById(", "marketId"
        elif entry["entity_type"] == "morpho_vault_v2":
            expected_paths = {
                f"/v0/vaults-v2/{selector}",
                f"/v1/vaults-v2/{selector}/state",
            }
            root, variable = "vaultV2ByAddress(", "address"
        else:
            expected_paths = {
                f"/v0/vaults-v1/{selector}",
                f"/v0/vaults-v1/{selector}/state",
            }
            root, variable = "vaultByAddress(", "address"
        assert expected_paths <= rest_paths, entry["entity_id"]
        assert root in body["query"], entry["entity_id"]
        assert body["variables"][variable] == identifier
        assert body["variables"]["chainId"] == entry["chain_id"]


def test_catalog_converts_6_and_18_decimal_vault_assets(api):
    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    usdc = entries["rockawayx-confidential-rwa-usdc"]["metrics"]
    weth = entries["rockawayx-weth"]["metrics"]
    europ = entries["rockawayx-europ"]["metrics"]
    assert (usdc["asset_symbol"], Decimal(usdc["total_assets"])) == (
        "USDC",
        Decimal("26400000"),
    )
    assert Decimal(usdc["liquidity_assets"]) == Decimal("3550000")
    assert weth["asset_symbol"] == "WETH"
    assert weth["total_assets"] == "0.849860311194908620"
    assert (europ["asset_symbol"], Decimal(europ["total_assets"])) == (
        "EUROP",
        Decimal("9600"),
    )
    assert Decimal(entries["figure-usdc-main"]["metrics"]["total_assets"]) == Decimal(
        "11.358783"
    )


def test_one_vault_provider_failure_leaves_other_entries_hydrated(api, morpho_api):
    failing = ROCKAWAYX_BY_ID["rockawayx-tori-ecosystem"]
    morpho_api.failing_paths.add(failing.state_path)

    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    assert list(entries) == ENTITY_IDS
    failed = entries["rockawayx-tori-ecosystem"]
    assert failed["analysis_available"] is False
    assert failed["availability"] == "provider_unavailable"
    assert failed["metrics"] is None
    others = [entry for entity_id, entry in entries.items() if entity_id != failed["entity_id"]]
    assert len(others) == 16
    for entry in others:
        assert entry["metrics"] is not None, entry["entity_id"]
        assert entry["freshness"] == "fresh"
        assert entry["analysis_available"] is (entry["entity_id"] != BASE_ENTITY_ID)


def test_rockawayx_metrics_never_fabricate_zero_tvl_or_liquidity(api, morpho_api):
    vault = ROCKAWAYX_BY_ID["rockawayx-nuva-ecosystem"]
    morpho_api.vault_graphql("v2", vault.address)["totalAssetsUsd"] = None

    entries = entries_by_id(api(api.default).get("/v1/catalog"))

    missing = entries["rockawayx-nuva-ecosystem"]
    assert missing["availability"] == "critical_data_missing"
    assert missing["analysis_available"] is False
    assert missing["metrics"]["total_assets_usd"] is None
    assert any("total_assets_usd" in warning for warning in missing["warnings"])
    # A zero reported by the provider is real data and stays zero, not null.
    weth = entries["rockawayx-weth"]["metrics"]
    assert Decimal(weth["liquidity_usd"]) == 0
    assert Decimal(weth["liquidity_assets"]) == 0
