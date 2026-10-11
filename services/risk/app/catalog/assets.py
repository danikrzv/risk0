"""Version-controlled issuer and underlying registry (`data/catalog/assets.ethereum.json`).

The registry lets the graph builder look through an asset to what backs it and
who issues it:

- `issuers`: who stands behind an asset. `issuer_type` is `protocol` (an on-chain
  protocol such as Lido), `custodial` (an off-chain custodian or e-money issuer
  such as Circle), `wrapper` (a contract that wraps another asset, such as WETH9
  or an ERC-4626 vault), or `native` (the chain itself).
- `references`: underlyings that are not Ethereum tokens: native ETH, or
  off-chain reserves such as BTC held by a custodian or EUR cash.
- `assets`: Ethereum tokens keyed by checksummed address, each with its
  underlying (another registered token or a reference), issuer, redemption
  mechanism, and a source list per claim (`underlying`, `issuer`, `redemption`).

Validation is strict and runs at load time: unique keys, every reference
resolves, every claim has at least one source (an https URL or a recorded
`eth_call`), and the token-to-token underlying chain is acyclic and at most
`MAX_UNDERLYING_DEPTH` deep. Anything not in the registry is unknown, never safe.
"""

from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


DEFAULT_ASSET_REGISTRY_PATH = (
    Path(__file__).resolve().parents[4] / "data" / "catalog" / "assets.ethereum.json"
)
# Longest token -> token underlying chain accepted (wstETH -> stETH is 1).
MAX_UNDERLYING_DEPTH = 4

Address = Annotated[str, StringConstraints(pattern=r"^0x[0-9A-Fa-f]{40}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Source = Annotated[str, StringConstraints(pattern=r"^(https://\S+|eth_call \S.*)$")]
Sources = Annotated[tuple[Source, ...], Field(min_length=1)]


class AssetRegistryError(ValueError):
    """The asset registry file is malformed or inconsistent."""


class IssuerType(StrEnum):
    PROTOCOL = "protocol"
    CUSTODIAL = "custodial"
    WRAPPER = "wrapper"
    NATIVE = "native"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Issuer(_Model):
    issuer_id: Slug
    name: Text
    issuer_type: IssuerType
    redemption: Text
    defiscan_slug: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")] | None
    source_urls: Sources


class ReferenceAsset(_Model):
    """An underlying that is not an Ethereum token (native ETH, off-chain reserves)."""

    reference_id: Slug
    symbol: Text
    kind: Literal["native", "offchain"]
    description: Text
    source_urls: Sources


class Underlying(_Model):
    """Exactly one of a registered token address or a reference id."""

    asset: Address | None = None
    reference: Slug | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Underlying":
        if (self.asset is None) == (self.reference is None):
            raise ValueError("underlying must name exactly one of asset or reference")
        return self


class ClaimSources(_Model):
    underlying: Sources
    issuer: Sources
    redemption: Sources


class RegisteredAsset(_Model):
    address: Address
    symbol: Text
    underlying: Underlying
    issuer_id: Slug
    redemption: Text
    sources: ClaimSources


class AssetRegistry(_Model):
    schema_version: Literal[1]
    chain_id: Literal[1]
    reviewed_at: Text
    description: Text
    issuers: tuple[Issuer, ...]
    references: tuple[ReferenceAsset, ...]
    assets: tuple[RegisteredAsset, ...]

    @model_validator(mode="after")
    def _consistent(self) -> "AssetRegistry":
        issuers = _unique((item.issuer_id for item in self.issuers), "issuer_id")
        references = _unique((item.reference_id for item in self.references), "reference_id")
        addresses = _unique((item.address.lower() for item in self.assets), "asset address")
        for asset in self.assets:
            if asset.issuer_id not in issuers:
                raise ValueError(f"{asset.symbol} names unknown issuer {asset.issuer_id!r}")
            underlying = asset.underlying
            if underlying.reference is not None and underlying.reference not in references:
                raise ValueError(f"{asset.symbol} names unknown reference {underlying.reference!r}")
            if underlying.asset is not None:
                if underlying.asset.lower() not in addresses:
                    raise ValueError(
                        f"{asset.symbol} is backed by {underlying.asset}, which is not registered"
                    )
                if underlying.asset.lower() == asset.address.lower():
                    raise ValueError(f"{asset.symbol} cannot be its own underlying")
        for asset in self.assets:
            self._check_chain(asset)
        return self

    def _check_chain(self, asset: RegisteredAsset) -> None:
        seen = [asset.address.lower()]
        current = asset
        while current.underlying.asset is not None:
            key = current.underlying.asset.lower()
            if key in seen:
                raise ValueError(f"underlying cycle through {asset.symbol}")
            seen.append(key)
            if len(seen) - 1 > MAX_UNDERLYING_DEPTH:
                raise ValueError(
                    f"underlying chain of {asset.symbol} is deeper than {MAX_UNDERLYING_DEPTH}"
                )
            current = self._by_address[key]

    @cached_property
    def _by_address(self) -> dict[str, RegisteredAsset]:
        return {item.address.lower(): item for item in self.assets}

    def asset(self, address: str) -> RegisteredAsset | None:
        return self._by_address.get(address.lower())

    def issuer(self, issuer_id: str) -> Issuer | None:
        return next((item for item in self.issuers if item.issuer_id == issuer_id), None)

    def reference(self, reference_id: str) -> ReferenceAsset | None:
        return next((item for item in self.references if item.reference_id == reference_id), None)


def _unique(values, label: str) -> set[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label} {value!r}")
        seen.add(value)
    return seen


def load_asset_registry(path: Path = DEFAULT_ASSET_REGISTRY_PATH) -> AssetRegistry:
    try:
        return AssetRegistry.model_validate_json(path.read_text())
    except (OSError, ValueError) as error:
        raise AssetRegistryError(f"invalid asset registry {path.name}: {error}") from error


@lru_cache(maxsize=1)
def default_asset_registry() -> AssetRegistry:
    return load_asset_registry()
