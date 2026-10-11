"""Hand-written ABI encoding for Multicall3 `aggregate3` and the static words Risk0 reads.

Only what the Chainlink and Morpho oracle readers need is implemented: static
32-byte words (uint, int, address), one dynamic `string` return value, and the
`aggregate3((address,bool,bytes)[])` call with its `(bool,bytes)[]` result. No
Keccak library is installed, so every function selector is a named constant
computed offline and verified against a live mainnet contract (see the
constants below).
"""

from collections.abc import Sequence
from dataclasses import dataclass


# Multicall3, deployed at the same address on every major EVM chain.
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
# keccak("aggregate3((address,bool,bytes)[])")[:4]
AGGREGATE3_SELECTOR = "0x82ad56cb"
WORD_BYTES = 32


class AbiDecodeError(ValueError):
    """A provider returned bytes that do not decode as the expected ABI type."""


@dataclass(frozen=True, slots=True)
class Call:
    target: str
    data: str
    allow_failure: bool = True


@dataclass(frozen=True, slots=True)
class CallResult:
    success: bool
    data: bytes


def encode_uint(value: int) -> str:
    if value < 0 or value >= 1 << 256:
        raise ValueError("uint256 out of range")
    return f"{value:064x}"


def call_data(selector: str, *arguments: int) -> str:
    """Selector followed by static uint arguments (uint80, uint16, uint256 all pad to a word)."""
    _require_selector(selector)
    return selector + "".join(encode_uint(argument) for argument in arguments)


def encode_aggregate3(calls: Sequence[Call]) -> str:
    """ABI-encode `aggregate3(calls)` as call data for Multicall3."""
    encoded_tuples: list[bytes] = []
    for call in calls:
        target = _address_bytes(call.target)
        payload = _hex_bytes(call.data)
        padding = (-len(payload)) % WORD_BYTES
        encoded_tuples.append(
            target.rjust(WORD_BYTES, b"\0")
            + _word(1 if call.allow_failure else 0)
            + _word(3 * WORD_BYTES)  # offset of `bytes` within the tuple
            + _word(len(payload))
            + payload
            + b"\0" * padding
        )
    offsets: list[bytes] = []
    position = WORD_BYTES * len(calls)
    for encoded in encoded_tuples:
        offsets.append(_word(position))
        position += len(encoded)
    body = _word(WORD_BYTES) + _word(len(calls)) + b"".join(offsets) + b"".join(encoded_tuples)
    return AGGREGATE3_SELECTOR + body.hex()


def decode_aggregate3(payload: str | bytes) -> tuple[CallResult, ...]:
    """Decode the `(bool success, bytes returnData)[]` returned by `aggregate3`."""
    data = payload if isinstance(payload, bytes) else _hex_bytes(payload)
    try:
        base = _read_word(data, 0)
        count = _read_word(data, base)
        if count > len(data) // WORD_BYTES:
            raise AbiDecodeError("aggregate3 result count exceeds the payload")
        items = base + WORD_BYTES
        results: list[CallResult] = []
        for index in range(count):
            start = items + _read_word(data, items + WORD_BYTES * index)
            success = _read_word(data, start)
            if success not in (0, 1):
                raise AbiDecodeError("aggregate3 success flag is not a bool")
            data_start = start + _read_word(data, start + WORD_BYTES)
            length = _read_word(data, data_start)
            end = data_start + WORD_BYTES + length
            if end > len(data):
                raise AbiDecodeError("aggregate3 return data exceeds the payload")
            results.append(CallResult(bool(success), data[data_start + WORD_BYTES : end]))
    except AbiDecodeError:
        raise
    except Exception:
        raise AbiDecodeError("aggregate3 result does not decode") from None
    return tuple(results)


def decode_uint(data: bytes, index: int = 0) -> int:
    return _read_word(data, index * WORD_BYTES)


def decode_int(data: bytes, index: int = 0) -> int:
    value = _read_word(data, index * WORD_BYTES)
    return value - (1 << 256) if value >= 1 << 255 else value


def decode_address(data: bytes, index: int = 0) -> str:
    value = _read_word(data, index * WORD_BYTES)
    if value >= 1 << 160:
        raise AbiDecodeError("word is not an address")
    return f"0x{value:040x}"


def decode_string(data: bytes) -> str:
    try:
        offset = _read_word(data, 0)
        length = _read_word(data, offset)
        end = offset + WORD_BYTES + length
        if end > len(data):
            raise AbiDecodeError("string exceeds the payload")
        return data[offset + WORD_BYTES : end].decode("utf-8")
    except AbiDecodeError:
        raise
    except Exception:
        raise AbiDecodeError("value is not an ABI string") from None


def hex_bytes(value: object) -> bytes:
    """`0x`-prefixed hex from a provider as bytes; anything else is a decode error."""
    if not isinstance(value, str):
        raise AbiDecodeError("value is not hexadecimal")
    return _hex_bytes(value)


def _read_word(data: bytes, offset: int) -> int:
    if offset < 0 or offset + WORD_BYTES > len(data):
        raise AbiDecodeError("ABI word is out of range")
    return int.from_bytes(data[offset : offset + WORD_BYTES], "big")


def _word(value: int) -> bytes:
    return value.to_bytes(WORD_BYTES, "big")


def _hex_bytes(value: str) -> bytes:
    if not value.startswith("0x") or len(value) % 2:
        raise AbiDecodeError("value is not 0x-prefixed hexadecimal")
    try:
        return bytes.fromhex(value[2:])
    except ValueError:
        raise AbiDecodeError("value is not 0x-prefixed hexadecimal") from None


def _address_bytes(value: str) -> bytes:
    raw = _hex_bytes(value)
    if len(raw) != 20:
        raise ValueError("address must be 20 bytes")
    return raw


def _require_selector(selector: str) -> None:
    if len(selector) != 10 or not selector.startswith("0x"):
        raise ValueError("selector must be 4 bytes of 0x-prefixed hex")
    int(selector[2:], 16)
