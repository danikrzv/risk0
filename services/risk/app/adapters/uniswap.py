"""Read-only Uniswap V3 exit quotes through QuoterV2 `eth_call` simulations.

The client enumerates candidate routes (direct pools on every supported fee tier,
plus two-hop routes through WETH or USDC), simulates each with QuoterV2 at the
current block, skips pools that revert, and keeps the route with the highest
output net of gas. Price impact is measured against a "marginal" probe quote of
1/10,000 of the trade on the winning route, so it isolates depth-driven loss from
the pool fee. Quotes are cached by exact request key for 15 seconds and are never
served once older than that.

Calldata is ABI-encoded by hand (no web3 dependency). The function selectors are
constants because the standard library has no keccak-256; they are the public
QuoterV2 selectors for `quoteExactInputSingle((address,address,uint256,uint24,
uint160))` and `quoteExactInput(bytes,uint256)`.

Secrets: the RPC URL lives only in `Settings`; it never appears in errors, reprs,
logs, cache payloads, or serialized quotes. Provider error bodies are discarded.
Sanitized errors are raised outside `except` blocks so `__context__` stays empty.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, localcontext

import httpx

from app.config import CachePolicy, Settings
from app.contracts.models import FreshnessStatus
from app.domain.exit import (
    NUMERAIRE_PLACES,
    QUOTE_TTL,
    WEI_PER_ETH,
    ExitQuote,
    ExitRoute,
    QuoteRequest,
)
from app.domain.snapshots import DataEnvelope
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


QUOTER_V2_ADDRESS = "0x61ffe014ba17989e743c5f6cb21bf9697530b21e"
QUOTE_EXACT_INPUT_SINGLE_SELECTOR = "0xc6a5026a"
QUOTE_EXACT_INPUT_SELECTOR = "0xcdca1753"
FEE_TIERS = (100, 500, 3000, 10000)
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC_ADDRESS = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
INTERMEDIARY_TOKENS = (("WETH", WETH_ADDRESS), ("USDC", USDC_ADDRESS))
QUOTE_SOURCE = "uniswap-v3:quoter-v2"
CACHE_NAMESPACE = "uniswap"
# Spec § 9: quotes refresh every 15 seconds and expire immediately after.
QUOTE_POLICY = CachePolicy(ttl=QUOTE_TTL, retention=QUOTE_TTL)
PRICE_IMPACT_PROBE_DIVISOR = 10_000
CALCULATION_PRECISION = 60
_REVERT_ERROR_CODES = frozenset({3, -32000, -32015})
_WORD_HEX = 64

RpcCall = Callable[[str, list[object]], object]


class UniswapProviderError(RuntimeError):
    """The RPC provider failed or returned malformed data (message is sanitized)."""


class UniswapCallReverted(Exception):
    """An `eth_call` reverted; raised by transports, carries no provider data."""


class UniswapQuoteUnavailableError(RuntimeError):
    """No candidate route produced a usable quote."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    index: int
    route: ExitRoute
    amount_out_units: int
    gas_estimate: int


class UniswapClient:
    def __init__(
        self,
        settings: Settings,
        *,
        rpc_call: RpcCall | None = None,
        cache: SQLiteCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        quoter_address: str = QUOTER_V2_ADDRESS,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._rpc_url = settings.ethereum_rpc_url
        self._rpc_call = rpc_call or self._request
        self._cache = cache
        self._clock = clock
        self._quoter = _checked_address(quoter_address)
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return f"UniswapClient(quoter={self._quoter}, rpc_url=<redacted>)"

    def quote_exact_input(self, request: QuoteRequest) -> DataEnvelope[ExitQuote]:
        key = request.cache_key()
        if self._cache is not None:
            cached = self._cache.get(CACHE_NAMESPACE, key, self._clock())
            if cached is not None and cached.freshness is Freshness.FRESH:
                return DataEnvelope[ExitQuote].model_validate_json(cached.payload)

        envelope = self._fetch(request)
        if self._cache is not None:
            self._cache.set(
                CACHE_NAMESPACE,
                key,
                envelope.model_dump_json().encode(),
                envelope.fetched_at,
                QUOTE_POLICY,
            )
        return envelope

    # Quote simulation

    def _fetch(self, request: QuoteRequest) -> DataEnvelope[ExitQuote]:
        quoted_at = self._clock()
        block_number = _hex_integer(self._call("eth_blockNumber", []))
        gas_price_wei = Decimal(_hex_integer(self._call("eth_gasPrice", [])))
        block_tag = hex(block_number)
        amount_units = request.amount_in_units()

        routes = candidate_routes(request)
        viable: list[_Candidate] = []
        reverted = 0
        for index, route in enumerate(routes):
            simulated = self._simulate(route, amount_units, block_tag)
            if simulated is None or simulated[0] <= 0:
                reverted += 1
                continue
            viable.append(_Candidate(index, route, simulated[0], simulated[1]))
        if not viable:
            raise UniswapQuoteUnavailableError(
                f"no viable Uniswap route from {request.token_in.symbol} to "
                f"{request.token_out.symbol} across {len(routes)} candidates"
            )

        warnings: list[str] = []
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            ranked = {
                candidate.index: _gas_adjusted(request, candidate, gas_price_wei)
                for candidate in viable
            }
            if any(gas_cost_out is None for _, gas_cost_out, _ in ranked.values()):
                warnings.append(
                    f"Gas could not be expressed in {request.token_out.symbol}; "
                    "routes were ranked by gross output."
                )
            best = max(
                viable,
                key=lambda candidate: (
                    ranked[candidate.index][2],
                    -candidate.route.hops,
                    -candidate.route.fee_fraction(),
                    -candidate.index,
                ),
            )
            amount_out = _units(best.amount_out_units, request.token_out.decimals)
            gas_cost_eth, gas_cost_out, net_amount_out = ranked[best.index]
            probe_units = max(1, amount_units // PRICE_IMPACT_PROBE_DIVISOR)
            probe = self._simulate(best.route, probe_units, block_tag)
            marginal_out: Decimal | None = None
            fee_cost_out: Decimal | None = None
            impact_cost_out: Decimal | None = None
            price_impact: Decimal | None = None
            if probe is None or probe[0] <= 0:
                warnings.append(
                    "Marginal probe quote reverted; price impact is unavailable."
                )
            else:
                marginal_rate = _units(probe[0], request.token_out.decimals) / _units(
                    probe_units, request.token_in.decimals
                )
                marginal_out = (request.amount_in * marginal_rate).quantize(NUMERAIRE_PLACES)
                impact_cost_out = marginal_out - amount_out
                if impact_cost_out < 0:
                    warnings.append(
                        "Marginal probe output was below the full quote; price impact "
                        "was floored at zero."
                    )
                    impact_cost_out = Decimal(0)
                impact_cost_out = impact_cost_out.quantize(NUMERAIRE_PLACES)
                price_impact = (
                    (impact_cost_out / marginal_out).quantize(NUMERAIRE_PLACES)
                    if marginal_out > 0
                    else Decimal(0)
                )
                fee_cost_out = (
                    marginal_out / (Decimal(1) - best.route.fee_fraction()) - marginal_out
                ).quantize(NUMERAIRE_PLACES)
            minimum_out = (amount_out * (Decimal(1) - request.slippage_tolerance)).quantize(
                Decimal(1).scaleb(-request.token_out.decimals), rounding=ROUND_DOWN
            )

        quote = ExitQuote(
            request=request,
            route=best.route,
            amount_in=request.amount_in,
            amount_out=amount_out,
            minimum_amount_out=minimum_out,
            marginal_amount_out=marginal_out,
            fee_fraction=best.route.fee_fraction(),
            fee_cost_out=fee_cost_out,
            price_impact=price_impact,
            price_impact_cost_out=impact_cost_out,
            gas_estimate=best.gas_estimate,
            gas_price_wei=gas_price_wei,
            gas_cost_eth=gas_cost_eth,
            gas_cost_out=gas_cost_out,
            net_amount_out=net_amount_out,
            block_number=block_number,
            quoted_at=quoted_at,
            candidates=len(routes),
            reverted=reverted,
            warnings=tuple(warnings),
        )
        return DataEnvelope[ExitQuote](
            value=quote,
            source=QUOTE_SOURCE,
            fetched_at=quoted_at,
            freshness=FreshnessStatus.FRESH,
            block_number=block_number,
        )

    def _simulate(
        self, route: ExitRoute, amount_units: int, block_tag: str
    ) -> tuple[int, int] | None:
        """Return (amount_out_units, gas_estimate), or None when the pool reverted."""
        if route.hops == 1:
            data = encode_quote_exact_input_single(
                route.path[0], route.path[1], amount_units, route.fee_tiers[0]
            )
        else:
            data = encode_quote_exact_input(
                encode_path(route.path, route.fee_tiers), amount_units
            )
        reverted = False
        output: object = None
        try:
            output = self._call("eth_call", [{"to": self._quoter, "data": data}, block_tag])
        except UniswapCallReverted:
            reverted = True
        if reverted:
            return None
        return decode_quote_output(output)

    # Transport

    def _call(self, method: str, params: list[object]) -> object:
        reverted = False
        failed = False
        result: object = None
        try:
            result = self._rpc_call(method, params)
        except UniswapCallReverted:
            reverted = True
        except Exception:
            failed = True
        if reverted:
            raise UniswapCallReverted()
        if failed:
            raise UniswapProviderError(f"Uniswap provider request failed for {method}")
        return result

    def _request(self, method: str, params: list[object]) -> object:
        failed = False
        payload: object = None
        try:
            response = httpx.post(
                self._rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            failed = True
        if failed:
            raise UniswapProviderError(f"Uniswap provider request failed for {method}")
        if not isinstance(payload, dict):
            raise UniswapProviderError(
                f"Uniswap provider returned an invalid response for {method}"
            )
        error = payload.get("error")
        if isinstance(error, dict):
            if _is_revert(error):
                raise UniswapCallReverted()
            raise UniswapProviderError(f"Uniswap provider returned an error for {method}")
        if "result" not in payload:
            raise UniswapProviderError(
                f"Uniswap provider returned an invalid response for {method}"
            )
        return payload["result"]


def candidate_routes(request: QuoteRequest) -> tuple[ExitRoute, ...]:
    """Direct routes on every fee tier, then two-hop routes via WETH and USDC."""
    token_in = request.token_in.address.lower()
    token_out = request.token_out.address.lower()
    routes = [
        ExitRoute(
            path=(token_in, token_out),
            fee_tiers=(fee,),
            symbols=(request.token_in.symbol, request.token_out.symbol),
        )
        for fee in FEE_TIERS
    ]
    for symbol, address in INTERMEDIARY_TOKENS:
        if address in (token_in, token_out):
            continue
        routes.extend(
            ExitRoute(
                path=(token_in, address, token_out),
                fee_tiers=(fee_in, fee_out),
                symbols=(request.token_in.symbol, symbol, request.token_out.symbol),
            )
            for fee_in in FEE_TIERS
            for fee_out in FEE_TIERS
        )
    return tuple(routes)


def _gas_adjusted(
    request: QuoteRequest, candidate: _Candidate, gas_price_wei: Decimal
) -> tuple[Decimal, Decimal | None, Decimal]:
    """(gas_cost_eth, gas_cost_out, net_amount_out) for a candidate."""
    amount_out = _units(candidate.amount_out_units, request.token_out.decimals)
    gas_cost_eth = (Decimal(candidate.gas_estimate) * gas_price_wei / WEI_PER_ETH).quantize(
        NUMERAIRE_PLACES
    )
    token_out = request.token_out
    gas_cost_out: Decimal | None
    if token_out.address.lower() == WETH_ADDRESS:
        gas_cost_out = gas_cost_eth
    elif (
        request.eth_price_usd is not None
        and token_out.price_usd is not None
        and token_out.price_usd > 0
    ):
        gas_cost_out = (gas_cost_eth * request.eth_price_usd / token_out.price_usd).quantize(
            NUMERAIRE_PLACES
        )
    else:
        gas_cost_out = None
    net = amount_out if gas_cost_out is None else amount_out - gas_cost_out
    return gas_cost_eth, gas_cost_out, net


# ABI helpers


def encode_quote_exact_input_single(
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    sqrt_price_limit_x96: int = 0,
) -> str:
    """Calldata for QuoterV2.quoteExactInputSingle(QuoteExactInputSingleParams)."""
    return QUOTE_EXACT_INPUT_SINGLE_SELECTOR + "".join(
        (
            _address_word(token_in),
            _address_word(token_out),
            _uint_word(amount_in, 256),
            _uint_word(fee, 24),
            _uint_word(sqrt_price_limit_x96, 160),
        )
    )


def encode_quote_exact_input(path: bytes, amount_in: int) -> str:
    """Calldata for QuoterV2.quoteExactInput(bytes path, uint256 amountIn)."""
    if not path:
        raise ValueError("path must not be empty")
    padded = path + bytes(-len(path) % 32)
    return QUOTE_EXACT_INPUT_SELECTOR + "".join(
        (_uint_word(64, 256), _uint_word(amount_in, 256), _uint_word(len(path), 256), padded.hex())
    )


def encode_path(tokens: Sequence[str], fees: Sequence[int]) -> bytes:
    """Uniswap V3 packed path: token (20 bytes), fee (3 bytes), token, ..."""
    if len(tokens) < 2 or len(fees) != len(tokens) - 1:
        raise ValueError("a path needs n tokens and n - 1 fees")
    path = bytes.fromhex(_checked_address(tokens[0])[2:])
    for fee, token in zip(fees, tokens[1:], strict=True):
        if not 0 <= fee < 1 << 24:
            raise ValueError("fee tiers must fit in uint24")
        path += fee.to_bytes(3, "big") + bytes.fromhex(_checked_address(token)[2:])
    return path


def decode_quote_output(data: object) -> tuple[int, int]:
    """(amountOut, gasEstimate) from either QuoterV2 quote return encoding.

    `quoteExactInputSingle` returns four static words; `quoteExactInput` returns
    `amountOut`, two dynamic-array offsets, and `gasEstimate` as its head words.
    Both place `amountOut` first and `gasEstimate` fourth.
    """
    if not isinstance(data, str) or not data.startswith("0x"):
        raise UniswapProviderError("Uniswap quoter returned invalid call data")
    body = data[2:]
    if len(body) < 4 * _WORD_HEX or len(body) % _WORD_HEX:
        raise UniswapProviderError("Uniswap quoter returned a truncated quote")
    try:
        amount_out = int(body[0:_WORD_HEX], 16)
        gas_estimate = int(body[3 * _WORD_HEX : 4 * _WORD_HEX], 16)
    except ValueError:
        amount_out = -1
        gas_estimate = -1
    if amount_out < 0 or gas_estimate < 0:
        raise UniswapProviderError("Uniswap quoter returned invalid call data")
    return amount_out, gas_estimate


def _address_word(address: str) -> str:
    return _checked_address(address)[2:].rjust(_WORD_HEX, "0")


def _uint_word(value: int, bits: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("ABI integers must be int")
    if not 0 <= value < 1 << bits:
        raise ValueError(f"value does not fit in uint{bits}")
    return f"{value:064x}"


def _checked_address(address: str) -> str:
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError("addresses must be 0x-prefixed 20-byte hex strings")
    try:
        int(address[2:], 16)
    except ValueError:
        raise ValueError("addresses must be 0x-prefixed 20-byte hex strings") from None
    return address.lower()


def _hex_integer(value: object) -> int:
    valid = isinstance(value, str) and value.startswith("0x")
    parsed: int | None = None
    if valid:
        try:
            parsed = int(value, 16)
        except ValueError:
            parsed = None
    if parsed is None or parsed < 0:
        raise UniswapProviderError("Uniswap provider returned an invalid hexadecimal value")
    return parsed


def _units(raw_units: int, decimals: int) -> Decimal:
    """Exact token units from an integer amount, independent of context precision."""
    return Decimal(f"{int(raw_units)}E-{decimals}")


def _is_revert(error: dict[str, object]) -> bool:
    code = error.get("code")
    message = error.get("message")
    return (isinstance(code, int) and code in _REVERT_ERROR_CODES) or (
        isinstance(message, str) and "revert" in message.lower()
    )


__all__ = [
    "CACHE_NAMESPACE",
    "FEE_TIERS",
    "INTERMEDIARY_TOKENS",
    "QUOTE_EXACT_INPUT_SELECTOR",
    "QUOTE_EXACT_INPUT_SINGLE_SELECTOR",
    "QUOTE_POLICY",
    "QUOTE_SOURCE",
    "QUOTER_V2_ADDRESS",
    "UniswapCallReverted",
    "UniswapClient",
    "UniswapProviderError",
    "UniswapQuoteUnavailableError",
    "candidate_routes",
    "decode_quote_output",
    "encode_path",
    "encode_quote_exact_input",
    "encode_quote_exact_input_single",
]
