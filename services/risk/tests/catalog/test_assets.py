import copy
import json
from pathlib import Path

import pytest

from app.catalog.assets import (
    DEFAULT_ASSET_REGISTRY_PATH,
    MAX_UNDERLYING_DEPTH,
    AssetRegistry,
    AssetRegistryError,
    IssuerType,
    default_asset_registry,
    load_asset_registry,
)


ROOT = Path(__file__).resolve().parents[4]
MORPHO_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "morpho"
REQUIRED_SYMBOLS = {"USDC", "WETH", "wstETH", "stETH", "WBTC", "cbBTC", "EURCV"}


# Keccak-256 (the standard library only has SHA3-256, which pads differently),
# used here to check the registry's EIP-55 checksummed addresses offline.
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROTATIONS = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))
_MASK = (1 << 64) - 1


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(state: list[list[int]]) -> list[list[int]]:
    for constant in _RC:
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        state = [[state[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROTATIONS[x][y])
        state = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        state[0][0] ^= constant
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01"
    padded += b"\x00" * (-len(padded) % rate)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate):
        for lane in range(rate // 8):
            start = offset + 8 * lane
            state[lane % 5][lane // 5] ^= int.from_bytes(padded[start : start + 8], "little")
        state = _keccak_f(state)
    return b"".join(state[lane % 5][lane // 5].to_bytes(8, "little") for lane in range(4))


def to_checksum_address(address: str) -> str:
    lowered = address.lower().removeprefix("0x")
    digest = keccak256(lowered.encode()).hex()
    return "0x" + "".join(char.upper() if int(digest[i], 16) >= 8 else char for i, char in enumerate(lowered))


def test_keccak_matches_known_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    # EIP-55 reference vector.
    assert to_checksum_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


# Registry file


def raw_registry() -> dict:
    return json.loads(DEFAULT_ASSET_REGISTRY_PATH.read_text())


def test_default_registry_path_is_the_version_controlled_file():
    assert DEFAULT_ASSET_REGISTRY_PATH == ROOT / "data" / "catalog" / "assets.ethereum.json"


def test_registry_loads_and_covers_the_catalog_assets():
    registry = load_asset_registry()

    assert registry.chain_id == 1
    assert REQUIRED_SYMBOLS <= {asset.symbol for asset in registry.assets}
    assert default_asset_registry() is default_asset_registry()


def test_every_registry_address_is_eip55_checksummed():
    for asset in raw_registry()["assets"]:
        assert asset["address"] == to_checksum_address(asset["address"]), asset["symbol"]
        if "asset" in asset["underlying"]:
            underlying = asset["underlying"]["asset"]
            assert underlying == to_checksum_address(underlying), asset["symbol"]


def _fixture_addresses() -> dict[str, str]:
    """Checksummed spellings of every asset address in the recorded Morpho fixtures."""
    found: dict[str, str] = {}

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"address", "loan_token", "collateral_token"} and isinstance(item, str):
                    found.setdefault(item.lower(), item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in sorted(MORPHO_FIXTURES.glob("*.json")):
        walk(json.loads(path.read_text()))
    return found


def test_registry_addresses_match_the_morpho_fixtures():
    fixtures = _fixture_addresses()
    registry = load_asset_registry()
    matched = {asset.symbol for asset in registry.assets if asset.address.lower() in fixtures}

    for asset in registry.assets:
        if asset.address.lower() in fixtures:
            assert asset.address == fixtures[asset.address.lower()], asset.symbol
    assert {"USDC", "WETH", "wstETH", "WBTC", "cbBTC", "EURCV", "steakEURCV"} <= matched


def test_known_look_through_chains():
    registry = load_asset_registry()
    wsteth = registry.asset("0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0")
    assert wsteth is not None and wsteth.underlying.asset is not None
    steth = registry.asset(wsteth.underlying.asset)
    assert steth is not None and steth.symbol == "stETH"
    assert steth.underlying.reference == "eth"
    lido = registry.issuer(steth.issuer_id)
    assert lido is not None
    assert lido.issuer_type is IssuerType.PROTOCOL
    assert lido.defiscan_slug == "lido"

    usdc = registry.asset("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    assert usdc is not None
    circle = registry.issuer(usdc.issuer_id)
    assert circle is not None and circle.issuer_type is IssuerType.CUSTODIAL


def test_every_claim_cites_a_source():
    for asset in load_asset_registry().assets:
        for claim in (asset.sources.underlying, asset.sources.issuer, asset.sources.redemption):
            assert claim and all(
                source.startswith("https://") or source.startswith("eth_call ") for source in claim
            ), asset.symbol


# Validation


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "assets.json"
    path.write_text(json.dumps(payload))
    return path


def mutated(mutate) -> dict:
    payload = copy.deepcopy(raw_registry())
    mutate(payload)
    return payload


def _asset(payload: dict, symbol: str) -> dict:
    return next(item for item in payload["assets"] if item["symbol"] == symbol)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["assets"].append(copy.deepcopy(_asset(p, "USDC"))), "duplicate asset address"),
        (lambda p: _asset(p, "USDC").update(issuer_id="nobody"), "unknown issuer"),
        (lambda p: _asset(p, "USDC").update(underlying={"reference": "gold"}), "unknown reference"),
        (
            lambda p: _asset(p, "USDC").update(underlying={"asset": "0x" + "1" * 40}),
            "not registered",
        ),
        (
            lambda p: _asset(p, "USDC").update(
                underlying={"asset": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"}
            ),
            "own underlying",
        ),
        (
            lambda p: _asset(p, "stETH").update(
                underlying={"asset": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"}
            ),
            "cycle",
        ),
        (
            lambda p: _asset(p, "USDC").update(
                underlying={"asset": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", "reference": "eth"}
            ),
            "exactly one",
        ),
        (lambda p: _asset(p, "USDC")["sources"].update(redemption=[]), "at least 1"),
        (lambda p: _asset(p, "USDC")["sources"].update(issuer=["http://insecure.example"]), "pattern"),
        (lambda p: _asset(p, "USDC").update(address="0x1234"), "pattern"),
        (lambda p: p["issuers"][0].update(issuer_type="bank"), "issuer_type"),
        (lambda p: p.update(chain_id=8453), "chain_id"),
        (lambda p: _asset(p, "USDC").update(rating="AAA"), "Extra inputs"),
    ],
)
def test_invalid_registries_are_rejected(tmp_path, mutate, message):
    path = write(tmp_path, mutated(mutate))

    with pytest.raises(AssetRegistryError, match=message):
        load_asset_registry(path)


def test_missing_registry_file_is_a_registry_error(tmp_path):
    with pytest.raises(AssetRegistryError):
        load_asset_registry(tmp_path / "absent.json")


def _token(index: int) -> str:
    return "0x" + f"{index:040x}"


def test_underlying_chains_deeper_than_the_bound_are_rejected():
    issuer = raw_registry()["issuers"][0]
    depth = MAX_UNDERLYING_DEPTH + 2
    assets = [
        {
            "address": _token(index),
            "symbol": f"T{index}",
            "underlying": {"asset": _token(index + 1)} if index < depth else {"reference": "eth"},
            "issuer_id": issuer["issuer_id"],
            "redemption": "synthetic",
            "sources": {"underlying": ["https://x.test"], "issuer": ["https://x.test"], "redemption": ["https://x.test"]},
        }
        for index in range(1, depth + 1)
    ]
    payload = {**raw_registry(), "assets": assets}

    with pytest.raises(ValueError, match="deeper than"):
        AssetRegistry.model_validate(payload)
