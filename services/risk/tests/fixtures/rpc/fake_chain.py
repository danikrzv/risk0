"""An in-memory Ethereum `eth_call` responder: Chainlink proxies, rate adapters, Morpho oracles,
third-party AggregatorV3 feeds and feeds that compute their answer when read.

Multicall3's `getBlockNumber()` / `getCurrentBlockTimestamp()` answer only when
`block_timestamp` is set (12 s per block); otherwise they revert, as on a chain
without them. An `eth_call` pinned to an earlier block sees that block's time.

Contracts are answered by selector, Multicall3 `aggregate3` is executed call by
call (with per-call failure), and aggregators refuse reads that arrive through
Multicall3, as mainnet access-controlled aggregators do. No network is used.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.adapters.ethereum import EthereumCallReverted
from app.adapters.multicall import MULTICALL3_ADDRESS

MULTICALL = MULTICALL3_ADDRESS.lower()
PHASE_OFFSET = 64


class Revert(Exception):
    pass


def word(value: int) -> bytes:
    return (value % (1 << 256)).to_bytes(32, "big")


def address_word(address: str) -> bytes:
    return bytes.fromhex(address[2:].rjust(64, "0"))


def string_result(text: str) -> bytes:
    raw = text.encode()
    return word(32) + word(len(raw)) + raw + b"\0" * ((-len(raw)) % 32)


def round_result(round_id: int, answer: int, updated_at: int) -> bytes:
    return word(round_id) + word(answer) + word(updated_at) + word(updated_at) + word(round_id)


@dataclass
class Feed:
    """A Chainlink EACAggregatorProxy. `phases[p]` lists (aggregator round, answer, updated_at)."""

    description: str
    decimals: int
    phases: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)
    aggregators: dict[int, str] = field(default_factory=dict)

    @property
    def phase_id(self) -> int:
        return max(self.phases)

    def latest(self) -> tuple[int, int, int]:
        phase = self.phase_id
        k, answer, updated = self.phases[phase][-1]
        return (phase << PHASE_OFFSET) | k, answer, updated

    def get_round(self, round_id: int) -> tuple[int, int, int] | None:
        phase, k = round_id >> PHASE_OFFSET, round_id & ((1 << PHASE_OFFSET) - 1)
        version = tuple((key, len(value)) for key, value in sorted(self.phases.items()))
        if getattr(self, "_version", None) != version:
            self._index = {(p, item[0]): item for p, items in self.phases.items() for item in items}
            self._version = version
        item = self._index.get((phase, k))
        return (round_id, item[1], item[2]) if item else None


class FakeChain:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[str, bool], bytes]] = {}
        self.calls: list[tuple[str, str]] = []
        self.eth_calls = 0
        self.fail = False
        self.block_number = 1000
        self.block_timestamp: int | None = None
        self.blocks_called: list[str] = []
        self._at_block: int | None = None

    def now(self) -> int:
        """The timestamp of the block the current call executes in."""
        if self.block_timestamp is None:
            raise Revert("no block time")
        block = self.block_number if self._at_block is None else self._at_block
        return self.block_timestamp - 12 * (self.block_number - block)

    # Contracts

    def add_feed(self, address: str, feed: Feed) -> Feed:
        key = address.lower()
        for phase in feed.phases:
            aggregator = feed.aggregators.setdefault(
                phase, f"0x{(int(key, 16) + phase) % (1 << 160):040x}"
            )
            self._add_aggregator(aggregator, feed, phase)

        def handle(data: str, via_multicall: bool) -> bytes:
            selector = data[:10]
            if selector == "0x7284e416":
                return string_result(feed.description)
            if selector == "0x313ce567":
                return word(feed.decimals)
            if selector == "0x58303b10":
                return word(feed.phase_id)
            if selector == "0xc1597304":
                phase = int(data[10:], 16)
                aggregator = feed.aggregators.get(phase)
                return address_word(aggregator) if aggregator else word(0)
            if selector == "0xfeaf968c":
                return round_result(*feed.latest())
            if selector == "0x9a6fc8f5":
                found = feed.get_round(int(data[10:], 16))
                return round_result(*found) if found else round_result(0, 0, 0)
            raise Revert(selector)

        self.handlers[key] = handle
        return feed

    def _add_aggregator(self, address: str, feed: Feed, phase: int) -> None:
        def handle(data: str, via_multicall: bool) -> bytes:
            if via_multicall:
                raise Revert("No access")
            if data[:10] == "0x668a0f02":
                rounds = feed.phases[phase]
                return word(rounds[-1][0] if rounds else 0)
            raise Revert(data[:10])

        self.handlers[address.lower()] = handle

    def add_rate_adapter(self, address: str, description: str, answer: int, decimals: int = 18) -> None:
        def handle(data: str, via_multicall: bool) -> bytes:
            selector = data[:10]
            if selector == "0x7284e416":
                return string_result(description)
            if selector == "0x313ce567":
                return word(decimals)
            if selector == "0xfeaf968c":
                return round_result(0, answer, 0)
            raise Revert(selector)

        self.handlers[address.lower()] = handle

    def add_third_party_feed(
        self,
        address: str,
        description: str,
        decimals: int,
        rounds: list[tuple[int, int, int]],
        *,
        history: bool = True,
    ) -> list[tuple[int, int, int]]:
        """An AggregatorV3 feed without `phaseId()`; `rounds` are (round id, answer, updated_at)."""

        def handle(data: str, via_multicall: bool) -> bytes:
            selector = data[:10]
            if selector == "0x7284e416":
                return string_result(description)
            if selector == "0x313ce567":
                return word(decimals)
            if selector == "0xfeaf968c":
                return round_result(*rounds[-1])
            if selector == "0x9a6fc8f5" and history:
                wanted = int(data[10:], 16)
                found = next((item for item in rounds if item[0] == wanted), None)
                if found is None:
                    raise Revert("no round")
                return round_result(*found)
            raise Revert(selector)

        self.handlers[address.lower()] = handle
        return rounds

    def add_computed_feed(
        self,
        address: str,
        description: str,
        answer: int,
        *,
        decimals: int = 18,
        round_id: int = 0,
        zero_time: bool = False,
        any_round: bool = False,
    ) -> dict:
        """A feed computing its answer when read: `updatedAt` is 0 or the block's own time."""
        state = {"answer": answer}

        def handle(data: str, via_multicall: bool) -> bytes:
            selector = data[:10]
            if selector == "0x7284e416":
                return string_result(description)
            if selector == "0x313ce567":
                return word(decimals)
            if selector == "0xfeaf968c" or (selector == "0x9a6fc8f5" and any_round):
                return round_result(round_id, state["answer"], 0 if zero_time else self.now())
            raise Revert(selector)

        self.handlers[address.lower()] = handle
        return state

    def add_oracle(
        self,
        address: str,
        *,
        base_feed_1: str = "0x" + "0" * 40,
        base_feed_2: str = "0x" + "0" * 40,
        quote_feed_1: str = "0x" + "0" * 40,
        quote_feed_2: str = "0x" + "0" * 40,
        price: int = 10**36,
        scale_factor: int = 10**18,
        version: str = "v2",
    ) -> dict:
        state = {"price": price}
        getters = {
            "0xf50a4718": address_word(base_feed_1),
            "0xdc53858c": address_word(base_feed_2),
            "0x56095e11": address_word(quote_feed_1),
            "0xacfbd39e": address_word(quote_feed_2),
            "0xce4b5bbe": word(scale_factor),
        }
        if version == "v2":
            getters["0xeaa2d7b4"] = word(0)
            getters["0x2e6f20a6"] = word(0)
        else:
            getters["0x411557d1"] = word(0)

        def handle(data: str, via_multicall: bool) -> bytes:
            selector = data[:10]
            if selector == "0xa035b1fe":
                return word(state["price"])
            if selector in getters:
                return getters[selector]
            raise Revert(selector)

        self.handlers[address.lower()] = handle
        return state

    # JSON-RPC

    def rpc(self, method: str, params: list[object]) -> object:
        if self.fail:
            raise RuntimeError("request to https://rpc.example.invalid/v2/fake-rpc-credential failed")
        if method == "eth_call":
            self.eth_calls += 1
            target = params[0]["to"].lower()
            data = params[0]["data"]
            tag = params[1] if len(params) > 1 else "latest"
            self.blocks_called.append(str(tag))
            self._at_block = None if tag == "latest" else int(str(tag), 16)
            try:
                if target == MULTICALL:
                    return "0x" + self._aggregate3(data).hex()
                return "0x" + self._execute(target, data, via_multicall=False).hex()
            except Revert:
                raise EthereumCallReverted("execution reverted") from None
            finally:
                self._at_block = None
        if method == "eth_blockNumber":
            return hex(1)
        raise AssertionError(f"unexpected method {method}")

    def _execute(self, target: str, data: str, *, via_multicall: bool) -> bytes:
        self.calls.append((target, data[:10]))
        if target == MULTICALL:
            if data[:10] == "0x42cbb15c" and self.block_timestamp is not None:
                return word(self.block_number if self._at_block is None else self._at_block)
            if data[:10] == "0x0f28c97d":
                return word(self.now())
            raise Revert(data[:10])
        handler = self.handlers.get(target)
        if handler is None:
            raise Revert("no code")
        return handler(data, via_multicall)

    def _aggregate3(self, data: str) -> bytes:
        assert data[:10] == "0x82ad56cb"
        raw = bytes.fromhex(data[10:])
        read = lambda offset: int.from_bytes(raw[offset : offset + 32], "big")  # noqa: E731
        base = read(0)
        count = read(base)
        results: list[tuple[bool, bytes]] = []
        for index in range(count):
            start = base + 32 + read(base + 32 + 32 * index)
            target = "0x" + raw[start + 12 : start + 32].hex()
            data_start = start + read(start + 64)
            length = read(data_start)
            call = "0x" + raw[data_start + 32 : data_start + 32 + length].hex()
            try:
                results.append((True, self._execute(target, call, via_multicall=True)))
            except Revert:
                results.append((False, b""))
        return encode_results(results)


def encode_results(results: list[tuple[bool, bytes]]) -> bytes:
    """ABI-encode `(bool,bytes)[]` as aggregate3 returns it."""
    tuples = []
    for success, payload in results:
        tuples.append(
            word(1 if success else 0) + word(64) + word(len(payload)) + payload + b"\0" * ((-len(payload)) % 32)
        )
    offsets = []
    position = 32 * len(tuples)
    for item in tuples:
        offsets.append(word(position))
        position += len(item)
    return word(32) + word(len(tuples)) + b"".join(offsets) + b"".join(tuples)
