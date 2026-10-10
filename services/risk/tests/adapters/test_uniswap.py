import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.uniswap import (
    FEE_TIERS,
    QUOTE_EXACT_INPUT_SELECTOR,
    QUOTE_EXACT_INPUT_SINGLE_SELECTOR,
    QUOTE_SOURCE,
    QUOTER_V2_ADDRESS,
    UniswapCallReverted,
    UniswapClient,
    UniswapProviderError,
    UniswapQuoteUnavailableError,
    decode_quote_output,
    encode_path,
    encode_quote_exact_input,
    encode_quote_exact_input_single,
)
from app.config import Settings
from app.contracts.models import FreshnessStatus
from app.domain.exit import ExitQuote, QuoteRequest
from app.domain.snapshots import AssetInfo, DataEnvelope
from app.infrastructure.cache import SQLiteCache


FIXTURES = Path(__file__).parents[1] / "fixtures" / "rpc"
FIXTURE = json.loads((FIXTURES / "uniswap-quotes.json").read_text())
TOKENS = {
    symbol: AssetInfo(
        address=item["address"],
        symbol=symbol,
        decimals=item["decimals"],
        price_usd=None if symbol != "USDC" else Decimal("1"),
    )
    for symbol, item in FIXTURE["tokens"].items()
}
ADDRESS_SYMBOLS = {item["address"].lower(): symbol for symbol, item in FIXTURE["tokens"].items()}
BLOCK_NUMBER = int(FIXTURE["block_number"], 16)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def settings(monkeypatch: pytest.MonkeyPatch, secret: str = "placeholder") -> Settings:
    monkeypatch.setenv("ETHEREUM_RPC_URL", f"https://rpc.invalid/v2/{secret}")
    return Settings.from_environment()


def decode_call(data: str) -> tuple[str, int]:
    """Decode QuoterV2 calldata into a fixture key prefix and the amount in."""
    selector = data[:10]
    words = [data[10:][index : index + 64] for index in range(0, len(data) - 10, 64)]
    if selector == QUOTE_EXACT_INPUT_SINGLE_SELECTOR:
        token_in = "0x" + words[0][-40:]
        token_out = "0x" + words[1][-40:]
        amount = int(words[2], 16)
        fee = int(words[3], 16)
        assert int(words[4], 16) == 0, "sqrtPriceLimitX96 must be zero"
        return f"{ADDRESS_SYMBOLS[token_in]}/{fee}/{ADDRESS_SYMBOLS[token_out]}", amount
    assert selector == QUOTE_EXACT_INPUT_SELECTOR, selector
    assert int(words[0], 16) == 64, "path offset"
    amount = int(words[1], 16)
    length = int(words[2], 16)
    path = bytes.fromhex("".join(words[3:]))[:length]
    parts = []
    cursor = 0
    while cursor < len(path):
        parts.append(ADDRESS_SYMBOLS["0x" + path[cursor : cursor + 20].hex()])
        cursor += 20
        if cursor < len(path):
            parts.append(str(int.from_bytes(path[cursor : cursor + 3], "big")))
            cursor += 3
    return "/".join(parts), amount


def encode_output(single: bool, amount_out: int, gas_estimate: int, hops: int) -> str:
    if single:
        words = [amount_out, 1 << 96, 1, gas_estimate]
        return "0x" + "".join(f"{word:064x}" for word in words)
    head = [amount_out, 0x80, 0x80 + 32 * (hops + 1), gas_estimate]
    arrays = [hops, *([1 << 96] * hops), hops, *([1] * hops)]
    return "0x" + "".join(f"{word:064x}" for word in head + arrays)


class FakeQuoter:
    def __init__(self, fixture: dict = FIXTURE) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, list[object]]] = []
        self.quoted: list[str] = []

    def __call__(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return self.fixture["block_number"]
        if method == "eth_gasPrice":
            return self.fixture["gas_price_wei"]
        if method == "eth_call":
            call, block = params
            assert call["to"] == QUOTER_V2_ADDRESS
            assert block == self.fixture["block_number"], "quotes must use the current block"
            prefix, amount = decode_call(call["data"])
            key = f"{prefix}:{amount}"
            self.quoted.append(key)
            entry = self.fixture["quotes"].get(key)
            if entry is None:
                raise UniswapCallReverted()
            hops = prefix.count("/") // 2
            return encode_output(
                call["data"].startswith(QUOTE_EXACT_INPUT_SINGLE_SELECTOR),
                int(entry["amount_out"]),
                int(entry["gas_estimate"]),
                hops,
            )
        raise AssertionError(f"unexpected method {method}")


def request(
    token_in: str = "WBTC",
    token_out: str = "USDC",
    amount: str = "1",
    eth_price_usd: str | None = "3000",
) -> QuoteRequest:
    return QuoteRequest(
        token_in=TOKENS[token_in],
        token_out=TOKENS[token_out],
        amount_in=Decimal(amount),
        eth_price_usd=None if eth_price_usd is None else Decimal(eth_price_usd),
    )


def client(monkeypatch, rpc, cache=None, clock=None) -> UniswapClient:
    return UniswapClient(
        settings(monkeypatch), rpc_call=rpc, cache=cache, clock=clock or FakeClock(NOW)
    )


# ABI helpers


def test_single_hop_calldata_is_abi_encoded_by_hand():
    data = encode_quote_exact_input_single(
        TOKENS["WETH"].address, TOKENS["USDC"].address, 10**18, 500
    )

    assert data == (
        QUOTE_EXACT_INPUT_SINGLE_SELECTOR
        + "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        + "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        + "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
        + "00000000000000000000000000000000000000000000000000000000000001f4"
        + "0000000000000000000000000000000000000000000000000000000000000000"
    )
    assert QUOTE_EXACT_INPUT_SINGLE_SELECTOR == "0xc6a5026a"


def test_multi_hop_calldata_packs_path_and_pads_bytes():
    path = encode_path(
        (TOKENS["WBTC"].address, TOKENS["WETH"].address, TOKENS["USDC"].address),
        (3000, 500),
    )
    assert path.hex() == (
        "2260fac5e5542a773aa44fbcfedf7c193bc2c599"
        "000bb8"
        "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        "0001f4"
        "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    )
    assert len(path) == 66

    data = encode_quote_exact_input(path, 10**8)

    assert data.startswith(QUOTE_EXACT_INPUT_SELECTOR)
    assert QUOTE_EXACT_INPUT_SELECTOR == "0xcdca1753"
    body = data[10:]
    assert len(body) % 64 == 0
    assert int(body[0:64], 16) == 0x40
    assert int(body[64:128], 16) == 10**8
    assert int(body[128:192], 16) == 66
    assert bytes.fromhex(body[192:]) == path + bytes(96 - 66)


def test_quote_output_decoding_reads_amount_and_gas_for_both_shapes():
    assert decode_quote_output(encode_output(True, 123, 456, 1)) == (123, 456)
    assert decode_quote_output(encode_output(False, 789, 1011, 2)) == (789, 1011)
    with pytest.raises(UniswapProviderError):
        decode_quote_output("0x")
    with pytest.raises(UniswapProviderError):
        decode_quote_output("0x" + "00" * 64)


# Route search and selection


def test_best_net_output_after_gas_wins_and_reverted_pools_are_skipped(monkeypatch):
    quoter = FakeQuoter()

    envelope = client(monkeypatch, quoter).quote_exact_input(request())

    assert isinstance(envelope, DataEnvelope)
    assert isinstance(envelope.value, ExitQuote)
    quote = envelope.value
    assert quote.route.symbols == ("WBTC", "WETH", "USDC")
    assert quote.route.fee_tiers == (3000, 500)
    assert quote.route.steps() == ("WBTC", "WETH@0.3%", "USDC@0.05%")
    assert quote.amount_in == Decimal("1")
    assert quote.amount_out == Decimal("65010")
    assert quote.gas_estimate == 180000
    assert quote.gas_price_wei == Decimal("30000000000")
    assert quote.gas_cost_eth == Decimal("0.0054")
    assert quote.gas_cost_out == Decimal("16.2")
    assert quote.net_amount_out == Decimal("64993.8")
    # 4 direct tiers + 4 x 4 via WETH; USDC is the output so it is not an intermediary.
    assert quote.candidates == len(FEE_TIERS) + len(FEE_TIERS) ** 2
    assert quote.reverted == quote.candidates - 5
    assert quote.block_number == BLOCK_NUMBER
    assert quote.quoted_at == NOW
    assert envelope.block_number == BLOCK_NUMBER
    assert envelope.fetched_at == NOW
    assert envelope.freshness is FreshnessStatus.FRESH
    assert envelope.source == QUOTE_SOURCE
    assert quote.warnings == ()


def test_gas_can_flip_the_selected_route(monkeypatch):
    quote = client(monkeypatch, FakeQuoter()).quote_exact_input(
        request(eth_price_usd="100000")
    ).value

    assert quote.route.symbols == ("WBTC", "USDC")
    assert quote.route.fee_tiers == (3000,)
    assert quote.gas_cost_out == Decimal("375")
    assert quote.net_amount_out == Decimal("64615")


def test_without_an_eth_price_routes_rank_by_gross_output_with_a_warning(monkeypatch):
    quote = client(monkeypatch, FakeQuoter()).quote_exact_input(
        request(eth_price_usd=None)
    ).value

    assert quote.route.fee_tiers == (3000, 500)
    assert quote.gas_cost_out is None
    assert quote.net_amount_out == quote.amount_out
    assert any("gross output" in warning for warning in quote.warnings)


def test_price_impact_is_derived_from_a_marginal_probe_quote(monkeypatch):
    quoter = FakeQuoter()

    quote = client(monkeypatch, quoter).quote_exact_input(request()).value

    assert quoter.quoted[-1] == "WBTC/3000/WETH/500/USDC:10000"
    assert quote.marginal_amount_out == Decimal("65020")
    assert quote.price_impact_cost_out == Decimal("10")
    assert quote.price_impact == (Decimal("10") / Decimal("65020")).quantize(
        Decimal("1E-18")
    )
    # Combined fee of a 0.3% then 0.05% hop: 1 - (1 - 0.003)(1 - 0.0005).
    assert quote.fee_fraction == Decimal("0.0034985")
    assert quote.fee_cost_out == (
        Decimal("65020") / (1 - Decimal("0.0034985")) - Decimal("65020")
    ).quantize(Decimal("1E-18"))
    assert quote.minimum_amount_out == Decimal("64684.95")


def test_token_decimals_stay_exact_for_18_and_6_decimal_tokens(monkeypatch):
    quoter = FakeQuoter()

    quote = client(monkeypatch, quoter).quote_exact_input(
        request("WETH", "USDC", "1")
    ).value

    assert quote.route.symbols == ("WETH", "USDC")
    assert quote.candidates == len(FEE_TIERS)
    assert quote.reverted == 2
    assert quote.amount_out == Decimal("2999.123456")
    assert str(quote.amount_out) == "2999.123456"
    assert quote.marginal_amount_out == Decimal("3000")
    assert quote.price_impact == Decimal("0.000292181333333333")
    assert "WETH/500/USDC:1000000000000000000" in quoter.quoted
    assert "WETH/500/USDC:100000000000000" in quoter.quoted


def test_weth_output_prices_gas_without_any_usd_price(monkeypatch):
    quote = client(monkeypatch, FakeQuoter()).quote_exact_input(
        request("USDC", "WETH", "1000", eth_price_usd=None)
    ).value

    # Both intermediaries are endpoints, so only the four direct tiers are tried.
    assert quote.candidates == len(FEE_TIERS)
    assert quote.route.steps() == ("USDC", "WETH@0.05%")
    assert quote.amount_out == Decimal("0.333")
    assert quote.gas_cost_eth == Decimal("0.0033")
    assert quote.gas_cost_out == quote.gas_cost_eth
    assert quote.net_amount_out == Decimal("0.3297")
    assert quote.marginal_amount_out == Decimal("0.3334")
    assert quote.price_impact_cost_out == Decimal("0.0004")
    assert quote.warnings == ()


def test_amount_in_must_be_representable_in_token_units():
    with pytest.raises(ValidationError, match="decimals"):
        request("USDC", "WETH", "1.0000001")
    with pytest.raises(ValidationError):
        request(amount="0")
    with pytest.raises(ValidationError):
        request(amount="-1")

    one_wei = request("WETH", "USDC", "0.000000000000000001")
    assert one_wei.amount_in_units() == 1
    assert request("WETH", "USDC", "1.5").amount_in_units() == 1_500_000_000_000_000_000


def test_no_viable_route_is_an_explicit_error(monkeypatch):
    with pytest.raises(UniswapQuoteUnavailableError, match="no viable Uniswap route"):
        client(monkeypatch, FakeQuoter()).quote_exact_input(request("USDC", "WBTC", "1"))


def test_empty_call_data_skips_the_pool_instead_of_aborting_the_quote(monkeypatch):
    quoter = FakeQuoter()

    def rpc(method: str, params: list[object]) -> object:
        if method == "eth_call" and decode_call(params[0]["data"])[0] == "WBTC/500/USDC":
            return "0x"
        return quoter(method, params)

    quote = client(monkeypatch, rpc).quote_exact_input(request()).value

    assert quote.route.steps() == ("WBTC", "WETH@0.3%", "USDC@0.05%")
    # The 0.05% direct pool is viable in the fixture; its empty reply counts as a revert.
    assert quote.reverted == quote.candidates - 4
    assert quote.price_impact_cost_out == Decimal("10")


def test_empty_call_data_from_every_pool_is_no_viable_route(monkeypatch):
    def rpc(method: str, params: list[object]) -> object:
        if method == "eth_call":
            return "0x"
        return FIXTURE[{"eth_blockNumber": "block_number", "eth_gasPrice": "gas_price_wei"}[method]]

    with pytest.raises(UniswapQuoteUnavailableError, match="no viable Uniswap route"):
        client(monkeypatch, rpc).quote_exact_input(request())


def test_malformed_non_empty_call_data_is_still_a_provider_error(monkeypatch):
    def rpc(method: str, params: list[object]) -> object:
        if method == "eth_call":
            return "0x" + "00" * 64
        return FIXTURE[{"eth_blockNumber": "block_number", "eth_gasPrice": "gas_price_wei"}[method]]

    with pytest.raises(UniswapProviderError, match="truncated"):
        client(monkeypatch, rpc).quote_exact_input(request())


# Price-impact probe sizing


def probe_fixture(quotes: dict[str, dict[str, str]]) -> dict:
    return {
        "block_number": FIXTURE["block_number"],
        "gas_price_wei": FIXTURE["gas_price_wei"],
        "tokens": FIXTURE["tokens"],
        "quotes": quotes,
    }


def test_probe_is_enlarged_until_it_yields_1000_output_units(monkeypatch):
    # 100 USDC -> 0.001538 WBTC. A 1/10,000 probe (0.01 USDC) would return 15 sats, which
    # rounds the marginal rate down below the full quote and floors the impact at zero.
    # 1000 sats at the full quote's average rate need ceil(1000 * 1e8 / 153800) = 650196 units.
    quoter = FakeQuoter(
        probe_fixture(
            {
                "USDC/500/WBTC:100000000": {"amount_out": "153800", "gas_estimate": "100000"},
                "USDC/500/WBTC:10000": {"amount_out": "15", "gas_estimate": "100000"},
                "USDC/500/WBTC:650196": {"amount_out": "1001", "gas_estimate": "100000"},
            }
        )
    )

    quote = client(monkeypatch, quoter).quote_exact_input(request("USDC", "WBTC", "100")).value

    assert quoter.quoted[-1] == "USDC/500/WBTC:650196"
    expected_marginal = (
        Decimal("100") * (Decimal("0.00001001") / Decimal("0.650196"))
    ).quantize(Decimal("1E-18"))
    assert quote.amount_out == Decimal("0.001538")
    assert quote.marginal_amount_out == expected_marginal
    assert quote.price_impact_cost_out == expected_marginal - Decimal("0.001538")
    assert quote.price_impact_cost_out > 0
    assert quote.fee_cost_out == (
        expected_marginal / (1 - Decimal("0.0005")) - expected_marginal
    ).quantize(Decimal("1E-18"))
    assert not any("floored" in warning or "rounding" in warning for warning in quote.warnings)


def test_rounding_limited_probe_reports_an_upper_bound_not_zero(monkeypatch):
    # 0.01 USDC -> 15 sats: no probe within the trade can yield 1000 sats, so the probe is
    # the whole trade and the marginal output is taken at the top of its rounding range
    # (16 sats), making the impact a conservative 1 sat instead of zero.
    quoter = FakeQuoter(
        probe_fixture({"USDC/500/WBTC:10000": {"amount_out": "15", "gas_estimate": "100000"}})
    )

    quote = client(monkeypatch, quoter).quote_exact_input(request("USDC", "WBTC", "0.01")).value

    assert quoter.quoted[-1] == "USDC/500/WBTC:10000"
    assert quote.amount_out == Decimal("0.00000015")
    assert quote.marginal_amount_out == Decimal("0.00000016")
    assert quote.price_impact_cost_out == Decimal("0.00000001")
    assert quote.price_impact > 0
    assert any("upper bound" in warning for warning in quote.warnings)


# Caching (15 second TTL, expire immediately after)


def test_identical_requests_are_served_from_cache_for_15_seconds(monkeypatch, tmp_path):
    clock = FakeClock(NOW)
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    quoter = FakeQuoter()
    uniswap = client(monkeypatch, quoter, cache=cache, clock=clock)

    first = uniswap.quote_exact_input(request())
    calls_after_first = len(quoter.calls)
    clock.advance(14)
    second = uniswap.quote_exact_input(request())

    assert len(quoter.calls) == calls_after_first
    assert second == first
    assert second.fetched_at == NOW
    assert second.freshness is FreshnessStatus.FRESH


def test_quotes_older_than_15_seconds_are_never_served(monkeypatch, tmp_path):
    clock = FakeClock(NOW)
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    quoter = FakeQuoter()
    uniswap = client(monkeypatch, quoter, cache=cache, clock=clock)

    uniswap.quote_exact_input(request())
    calls_after_first = len(quoter.calls)
    clock.advance(15)
    refreshed = uniswap.quote_exact_input(request())

    assert len(quoter.calls) > calls_after_first
    assert refreshed.fetched_at == NOW + timedelta(seconds=15)
    assert cache.get("uniswap", request().cache_key(), clock()) is not None
    clock.advance(15)
    assert cache.get("uniswap", request().cache_key(), clock()) is None


def test_cache_keys_are_exact_request_keys(monkeypatch, tmp_path):
    clock = FakeClock(NOW)
    quoter = FakeQuoter()
    uniswap = client(
        monkeypatch, quoter, cache=SQLiteCache(tmp_path / "cache.sqlite3"), clock=clock
    )

    uniswap.quote_exact_input(request())
    calls_after_first = len(quoter.calls)
    uniswap.quote_exact_input(request(eth_price_usd="100000"))

    assert len(quoter.calls) > calls_after_first
    assert request().cache_key() != request(eth_price_usd="100000").cache_key()
    assert request().cache_key() == request().cache_key()
    assert "placeholder" not in request().cache_key()


# Sanitized errors


def test_provider_failures_never_expose_rpc_url_or_body(monkeypatch):
    secret = "do-not-leak-token"

    def failing_rpc(method: str, params: list[object]) -> object:
        raise RuntimeError(f"provider failed at https://rpc.invalid/v2/{secret}")

    uniswap = UniswapClient(settings(monkeypatch, secret), rpc_call=failing_rpc)

    with pytest.raises(UniswapProviderError) as error:
        uniswap.quote_exact_input(request())

    rendered = f"{error.value!r} {error.value} {error.value.args}"
    assert secret not in rendered
    assert "https://" not in rendered
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_reverts_raised_inside_handlers_do_not_carry_context(monkeypatch):
    secret = "revert-body-token"
    seen: list[BaseException | None] = []

    def rpc(method: str, params: list[object]) -> object:
        if method != "eth_call":
            return FIXTURE[{"eth_blockNumber": "block_number", "eth_gasPrice": "gas_price_wei"}[method]]
        try:
            raise ValueError(f"execution reverted at https://rpc.invalid/{secret}")
        except ValueError:
            raise UniswapCallReverted()

    uniswap = UniswapClient(settings(monkeypatch, secret), rpc_call=rpc)
    with pytest.raises(UniswapQuoteUnavailableError) as error:
        uniswap.quote_exact_input(request())

    chain = error.value
    while chain is not None:
        seen.append(chain)
        assert secret not in repr(chain)
        chain = chain.__cause__ or chain.__context__
    assert len(seen) == 1


def test_json_rpc_transport_sanitizes_error_bodies_and_detects_reverts(monkeypatch):
    secret = "transport-error-token"
    payloads = iter(
        [
            {"jsonrpc": "2.0", "id": 1, "result": FIXTURE["block_number"]},
            {"jsonrpc": "2.0", "id": 1, "result": FIXTURE["gas_price_wei"]},
        ]
    )
    posted: list[dict] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def post(url: str, *, json: dict, timeout: float) -> Response:
        posted.append(json)
        if json["method"] == "eth_call":
            return Response(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 3, "message": f"execution reverted {secret}", "data": "0x"},
                }
            )
        return Response(next(payloads))

    monkeypatch.setattr("app.adapters.uniswap.httpx.post", post)
    uniswap = UniswapClient(settings(monkeypatch, secret))

    with pytest.raises(UniswapQuoteUnavailableError) as error:
        uniswap.quote_exact_input(request())

    assert secret not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert all(call["method"] in {"eth_blockNumber", "eth_gasPrice", "eth_call"} for call in posted)
    assert len(posted) == 2 + len(FEE_TIERS) + len(FEE_TIERS) ** 2


def test_non_revert_json_rpc_errors_are_provider_errors_without_bodies(monkeypatch):
    secret = "rate-limit-token"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": secret}}

    monkeypatch.setattr("app.adapters.uniswap.httpx.post", lambda *args, **kwargs: Response())
    uniswap = UniswapClient(settings(monkeypatch, secret))

    with pytest.raises(UniswapProviderError) as error:
        uniswap.quote_exact_input(request())

    assert secret not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def json_rpc_stub(monkeypatch, respond) -> list[dict]:
    """Route `httpx.post` to `respond(method) -> payload`; returns the posted bodies."""
    posted: list[dict] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def post(url: str, *, json: dict, timeout: float) -> Response:
        posted.append(json)
        return Response(respond(json["method"]))

    monkeypatch.setattr("app.adapters.uniswap.httpx.post", post)
    return posted


def control_result(method: str) -> dict:
    key = {"eth_blockNumber": "block_number", "eth_gasPrice": "gas_price_wei"}[method]
    return {"jsonrpc": "2.0", "id": 1, "result": FIXTURE[key]}


def assert_sanitized(error: BaseException, secret: str) -> None:
    assert secret not in f"{error!r} {error} {error.args}"
    assert "https://" not in f"{error!r} {error} {error.args}"
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("code", [-32000, -32015])
def test_generic_server_errors_on_eth_call_are_node_problems_not_reverts(monkeypatch, code):
    secret = "header-not-found-token"

    def respond(method: str) -> dict:
        if method == "eth_call":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": code, "message": f"header not found {secret}"},
            }
        return control_result(method)

    json_rpc_stub(monkeypatch, respond)
    uniswap = UniswapClient(settings(monkeypatch, secret))

    with pytest.raises(UniswapProviderError) as error:
        uniswap.quote_exact_input(request())

    assert not isinstance(error.value, UniswapQuoteUnavailableError)
    assert_sanitized(error.value, secret)


def test_generic_server_error_on_a_control_call_is_a_sanitized_provider_error(monkeypatch):
    secret = "gas-price-node-token"

    def respond(method: str) -> dict:
        if method == "eth_gasPrice":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": f"missing trie node {secret}"},
            }
        return control_result(method)

    json_rpc_stub(monkeypatch, respond)
    uniswap = UniswapClient(settings(monkeypatch, secret))

    with pytest.raises(UniswapProviderError) as error:
        uniswap.quote_exact_input(request())

    assert "eth_gasPrice" in str(error.value)
    assert_sanitized(error.value, secret)


def test_a_revert_from_a_control_call_never_escapes_as_a_bare_revert(monkeypatch):
    def rpc(method: str, params: list[object]) -> object:
        raise UniswapCallReverted()

    with pytest.raises(UniswapProviderError) as error:
        client(monkeypatch, rpc).quote_exact_input(request())

    assert "eth_blockNumber" in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "error",
    [
        {"code": 3, "message": "execution reverted", "data": "0x"},
        {"code": -32000, "message": "execution reverted"},
        {"code": -32000, "message": "execution reverted: STF", "data": "0x08c379a0"},
        {"code": -32015, "message": "VM execution error.", "data": "0x"},
    ],
    ids=["code-3", "server-error-message", "server-error-message-and-data", "server-error-data"],
)
def test_reverts_need_code_3_or_a_server_error_with_revert_evidence(monkeypatch, error):
    def respond(method: str) -> dict:
        if method == "eth_call":
            return {"jsonrpc": "2.0", "id": 1, "error": error}
        return control_result(method)

    json_rpc_stub(monkeypatch, respond)
    uniswap = UniswapClient(settings(monkeypatch))

    with pytest.raises(UniswapQuoteUnavailableError, match="no viable Uniswap route"):
        uniswap.quote_exact_input(request())


def test_a_revert_message_under_another_code_is_not_a_revert(monkeypatch):
    def respond(method: str) -> dict:
        if method == "eth_call":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32603, "message": "internal error: reverted upstream"},
            }
        return control_result(method)

    json_rpc_stub(monkeypatch, respond)
    uniswap = UniswapClient(settings(monkeypatch))

    with pytest.raises(UniswapProviderError):
        uniswap.quote_exact_input(request())


def test_client_repr_and_serialized_quotes_never_contain_the_rpc_url(monkeypatch):
    secret = "repr-secret-token"
    uniswap = UniswapClient(settings(monkeypatch, secret), rpc_call=FakeQuoter())

    envelope = uniswap.quote_exact_input(request())

    assert secret not in repr(uniswap)
    assert secret not in envelope.model_dump_json()
    assert "https://" not in envelope.model_dump_json()
