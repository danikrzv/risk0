import pytest

from app.adapters.multicall import (
    AbiDecodeError,
    Call,
    CallResult,
    call_data,
    decode_address,
    decode_aggregate3,
    decode_int,
    decode_string,
    decode_uint,
    encode_aggregate3,
)
from tests.fixtures.rpc.fake_chain import encode_results, string_result, word

ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"


def words(*values: str) -> str:
    return "".join(value.rjust(64, "0") for value in values)


def test_aggregate3_encoding_matches_the_abi_layout():
    encoded = encode_aggregate3([Call(ETH_USD, "0x7284e416")])

    assert encoded == (
        "0x82ad56cb"
        + words("20", "1", "20")  # array offset, length, offset of tuple 0
        + words("5f4ec3df9cbd43714fe2740f5e3616155c5b8419", "1", "60", "4")
        + "7284e416".ljust(64, "0")
    )


def test_aggregate3_encoding_of_several_calls_offsets_each_tuple():
    encoded = encode_aggregate3(
        [Call(ETH_USD, call_data("0x9a6fc8f5", 5)), Call(ETH_USD, "0x313ce567", allow_failure=False)]
    )
    body = bytes.fromhex(encoded[10:])
    read = lambda offset: int.from_bytes(body[offset : offset + 32], "big")  # noqa: E731

    assert read(32) == 2
    first, second = read(64), read(96)
    assert first == 64
    # Tuple 0: address, flag, offset and length words, then 36 bytes padded to 2 words.
    assert second == first + 6 * 32
    assert read(64 + second + 32) == 0  # allowFailure false


def test_aggregate3_result_round_trips_success_flags_and_data():
    payload = encode_results([(True, word(8)), (False, b""), (True, string_result("ETH / USD"))])

    results = decode_aggregate3("0x" + payload.hex())

    assert results[0] == CallResult(True, word(8))
    assert results[1] == CallResult(False, b"")
    assert decode_string(results[2].data) == "ETH / USD"


def test_truncated_aggregate3_result_is_a_decode_error():
    payload = encode_results([(True, word(8))])

    with pytest.raises(AbiDecodeError):
        decode_aggregate3("0x" + payload[:-40].hex())
    with pytest.raises(AbiDecodeError):
        decode_aggregate3("0xzz")


def test_static_word_decoders():
    data = word(7) + word(-5) + bytes.fromhex(ETH_USD[2:].rjust(64, "0"))

    assert decode_uint(data, 0) == 7
    assert decode_int(data, 1) == -5
    assert decode_address(data, 2) == ETH_USD.lower()
    with pytest.raises(AbiDecodeError):
        decode_uint(data, 3)
    with pytest.raises(AbiDecodeError):
        decode_address(word(1 << 200))


def test_call_data_pads_static_arguments():
    assert call_data("0xc1597304", 3) == "0xc1597304" + "3".rjust(64, "0")
    with pytest.raises(ValueError):
        call_data("0x123", 1)
