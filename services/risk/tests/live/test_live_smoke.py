"""Opt-in live smoke test (spec Task 12 step 6).

Runs only with `RISK0_DATA_MODE=live` and `ETHEREUM_RPC_URL` set, for example:

    RISK0_DATA_MODE=live uv run --project services/risk \\
        pytest services/risk/tests/live/test_live_smoke.py -v

It reads public Morpho data and calls the configured Ethereum RPC; nothing is
written outside a temporary cache. The RPC URL is never printed: provider
failures are reported by exception type only, and assertions never compare
against the URL.
"""

import os
from decimal import Decimal

import pytest

from app.contracts.models import CatalogAvailability


LIVE_REQUESTED = os.environ.get("RISK0_DATA_MODE", "").strip().lower() == "live"
HAS_RPC = bool(os.environ.get("ETHEREUM_RPC_URL", "").strip())

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_REQUESTED, reason="set RISK0_DATA_MODE=live to run live smoke tests"),
    pytest.mark.skipif(not HAS_RPC, reason="ETHEREUM_RPC_URL is not configured (see README)"),
]

WETH_USDC_MARKET = "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"


def _sanitized(action, label: str):
    """Run a provider call; on failure report only the exception type, never its text."""
    try:
        return action()
    except Exception as error:  # provider boundary: the message may name the RPC endpoint
        pytest.fail(f"{label} failed: {type(error).__name__}", pytrace=False)


@pytest.fixture
def settings():
    from app.config import Settings

    return Settings.from_environment()


@pytest.fixture
def morpho(tmp_path):
    from app.adapters.morpho import DEFAULT_BASE_URL, MorphoClient
    from app.infrastructure.cache import SQLiteCache

    return MorphoClient(
        os.environ.get("MORPHO_API_URL", DEFAULT_BASE_URL),
        cache=SQLiteCache(tmp_path / "live-smoke.sqlite3"),
    )


def test_morpho_catalog_hydrates_the_mainnet_entries(morpho):
    from app.api.catalog import REPOSITORY_ROOT
    from app.catalog.repository import CatalogRepository
    from app.catalog.service import CatalogService

    repository = CatalogRepository(REPOSITORY_ROOT / "data" / "catalog" / "ethereum.json")
    response = _sanitized(lambda: CatalogService(repository, morpho).list_hydrated(), "Morpho catalog")

    entries = {entry.entity_id: entry for entry in response.entries}
    assert len(entries) == len(repository.list_entries())
    mainnet = [entry for entry in entries.values() if entry.chain_id == 1 and entry.supported]
    available = [entry for entry in mainnet if entry.availability is CatalogAvailability.AVAILABLE]
    # Every golden-flow entry must resolve live, with metrics and a source timestamp.
    for entity_id in ("belem-rwa-eurcv", "wsteth-weth-965", "weth-usdc-860"):
        entry = entries[entity_id]
        assert entry.availability is CatalogAvailability.AVAILABLE, entity_id
        assert entry.metrics is not None and entry.fetched_at is not None, entity_id
    assert len(available) >= 3
    # The Base listing is never analyzed.
    assert all(not entry.analysis_available for entry in entries.values() if entry.chain_id != 1)


def test_ethereum_rpc_is_mainnet(settings):
    from app.adapters.ethereum import EthereumClient

    client = EthereumClient(settings)

    assert _sanitized(client.get_chain_id, "eth_chainId") == 1
    assert _sanitized(client.get_block_number, "eth_blockNumber") > 0


def test_uniswap_quote_for_a_weth_to_usdc_exit(settings, morpho):
    from app.adapters.uniswap import UniswapClient, UniswapQuoteUnavailableError
    from app.domain.exit import QuoteRequest

    market = _sanitized(lambda: morpho.get_market(WETH_USDC_MARKET), "Morpho market").value
    request = QuoteRequest(
        token_in=market.collateral_asset,
        token_out=market.loan_asset,
        amount_in=Decimal("1"),
        eth_price_usd=market.collateral_asset.price_usd,
    )
    try:
        envelope = UniswapClient(settings).quote_exact_input(request)
    except UniswapQuoteUnavailableError as error:
        # The message names only the tokens and candidate count.
        pytest.skip(f"unsupported route: {error}")
    except Exception as error:
        pytest.fail(f"Uniswap quote failed: {type(error).__name__}", pytrace=False)

    quote = envelope.value
    assert quote.amount_in == Decimal("1")
    assert quote.amount_out > 0
    assert quote.block_number > 0
    assert quote.route.symbols[0] == market.collateral_asset.symbol
