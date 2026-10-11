from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.defillama import (
    DEFILLAMA_POLICY,
    FAILURE_BACKOFF,
    DefiLlamaUnavailableError,
    chain_by_name,
    protocol_by_slug,
)
from app.contracts.models import FreshnessStatus
from app.infrastructure.cache import SQLiteCache
from tests.fixtures.defillama.recorded import FakeDefiLlamaApi


@pytest.fixture
def api():
    return FakeDefiLlamaApi()


@pytest.fixture
def cache(tmp_path):
    return SQLiteCache(tmp_path / "cache.sqlite3")


def test_hacks_are_parsed_with_decimal_amounts_and_utc_dates(api, clock):
    envelope = api.client(clock).hacks()

    assert envelope.source == "defillama https://api.llama.fi/hacks"
    assert envelope.freshness is FreshnessStatus.FRESH
    assert envelope.fetched_at == clock()
    assert len(envelope.value) == 13
    compound = next(item for item in envelope.value if item.name == "Compound V2")
    assert compound.occurred_at == datetime(2021, 9, 29, tzinfo=UTC)
    assert compound.amount_usd == Decimal("147000000")
    assert compound.defillama_id == "114"
    assert compound.classification == "Token & Share Accounting"
    assert compound.technique == "Arithmetic Error"
    assert compound.chains == ("Ethereum",)
    morpho = next(item for item in envelope.value if "Morpho" in item.name)
    assert morpho.defillama_id is None
    assert morpho.technique == "Oracle Misconfiguration"
    assert any(item.amount_usd is None for item in envelope.value)


def test_protocols_keep_only_the_projected_fields(api, clock):
    protocols = api.client(clock).protocols().value

    morpho = protocol_by_slug(protocols, "morpho-blue")
    assert morpho is not None
    assert morpho.id == "4025"
    assert morpho.category == "Lending"
    assert morpho.tvl_usd == Decimal("11268588686.268003")
    assert morpho.ethereum_tvl_usd == Decimal("5165760890.8268795")
    assert morpho.ethereum_borrowed_usd == Decimal("2605029925.226604")
    assert morpho.parent_protocol == "parent#morpho"
    assert morpho.listed_at == datetime.fromtimestamp(1705261273, UTC)
    assert morpho.previous_names == ("Morpho V1",)
    compound = protocol_by_slug(protocols, "compound-v2")
    assert compound is not None and compound.listed_at is None
    justlend = protocol_by_slug(protocols, "justlend-v1")
    assert justlend is not None and justlend.ethereum_tvl_usd is None


def test_chains_are_parsed(api, clock):
    chains = api.client(clock).chains().value

    ethereum = chain_by_name(chains, "Ethereum")
    assert ethereum is not None
    assert ethereum.chain_id == 1
    assert ethereum.tvl_usd == Decimal("51524257395.28339")
    assert chain_by_name(chains, "Solana").chain_id is None


def test_responses_are_memoized_in_process_until_the_daily_ttl(api, clock):
    client = api.client(clock)

    client.protocols()
    client.protocols()
    assert api.requests == ["/protocols"]

    clock.advance(DEFILLAMA_POLICY.ttl)
    client.protocols()
    assert api.requests == ["/protocols", "/protocols"]


def test_a_new_client_reads_the_daily_sqlite_cache_without_a_request(api, clock, cache):
    api.client(clock, cache).hacks()
    clock.advance(timedelta(hours=23))

    envelope = api.client(clock, cache).hacks()

    assert api.requests == ["/hacks"]
    assert envelope.freshness is FreshnessStatus.FRESH
    assert len(envelope.value) == 13


def test_outage_serves_stale_cache_then_fails_once_retention_ends(api, clock, cache):
    api.client(clock, cache).chains()
    api.mode = "down"
    clock.advance(timedelta(days=2))

    stale = api.client(clock, cache).chains()
    assert stale.freshness is FreshnessStatus.STALE_USABLE

    clock.advance(DEFILLAMA_POLICY.retention)
    with pytest.raises(DefiLlamaUnavailableError):
        api.client(clock, cache).chains()


@pytest.mark.parametrize("mode", ["down", "error"])
def test_errors_are_sanitized(api, clock, mode):
    api.mode = mode

    with pytest.raises(DefiLlamaUnavailableError) as raised:
        api.client(clock).hacks()

    message = str(raised.value)
    assert message.startswith("DeFiLlama /hacks unavailable")
    for leaked in ("secret", "stack trace", "https://"):
        assert leaked not in message
    assert ("ConnectError" if mode == "down" else "HTTP 500") in message


def test_not_found_and_invalid_json_are_unavailable(api, clock):
    api.bodies["/v2/chains"] = b"<html>oops</html>"
    with pytest.raises(DefiLlamaUnavailableError, match="invalid JSON"):
        api.client(clock).chains()

    del api.bodies["/hacks"]
    with pytest.raises(DefiLlamaUnavailableError, match="HTTP 404"):
        api.client(clock).hacks()


def test_unexpected_payload_shape_is_unavailable(api, clock):
    api.bodies["/protocols"] = b'{"protocols": []}'

    with pytest.raises(DefiLlamaUnavailableError, match="not a list"):
        api.client(clock).protocols()


def test_failed_fetch_is_not_retried_until_the_backoff_passes(api, clock):
    client = api.client(clock)
    api.mode = "down"
    with pytest.raises(DefiLlamaUnavailableError):
        client.protocols()
    attempts = len(api.requests)

    api.mode = "ok"
    with pytest.raises(DefiLlamaUnavailableError, match="retry deferred"):
        client.protocols()
    assert len(api.requests) == attempts

    clock.advance(FAILURE_BACKOFF)
    assert client.protocols().value
    assert len(api.requests) == attempts + 1


def test_malformed_rows_are_skipped(api, clock):
    api.bodies["/hacks"] = (
        b'[{"name": "No date"}, {"date": 1700000000}, "text", '
        b'{"date": 1700000000, "name": " Real ", "amount": 1.5e6, "chain": ["Ethereum", 3]}]'
    )

    (hack,) = api.client(clock).hacks().value

    assert hack.name == "Real"
    assert hack.amount_usd == Decimal("1.5E+6")
    assert hack.chains == ("Ethereum",)
