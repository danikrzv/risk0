import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class CachePolicy:
    ttl: timedelta
    retention: timedelta

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0):
            raise ValueError("cache TTL must be positive")
        if self.retention < self.ttl:
            raise ValueError("cache retention must be at least the TTL")


@dataclass(frozen=True, slots=True)
class Settings:
    ethereum_rpc_url: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.ethereum_rpc_url.strip():
            raise ValueError("ETHEREUM_RPC_URL must be configured")

    @classmethod
    def from_environment(cls) -> "Settings":
        rpc_url = os.environ.get("ETHEREUM_RPC_URL", "")
        if not rpc_url:
            raise ValueError("ETHEREUM_RPC_URL must be configured")
        return cls(ethereum_rpc_url=rpc_url)

    def __repr__(self) -> str:
        return "Settings(ethereum_rpc_url=<redacted>)"


# Credential-free public endpoints, tried after any configured URLs. Private or
# keyed URLs belong only in `.env` as RISK0_RPC_<CHAIN_ID>_URLS (comma-separated).
PUBLIC_RPC_URLS: dict[int, tuple[str, ...]] = {
    1: ("https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"),
    10: ("https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"),
    130: ("https://mainnet.unichain.org",),
    137: ("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"),
    999: ("https://rpc.hyperliquid.xyz/evm",),
    8453: ("https://base-rpc.publicnode.com", "https://mainnet.base.org"),
    42161: ("https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"),
    747474: ("https://rpc.katana.network",),
}
_RPC_URLS_VARIABLE = re.compile(r"^RISK0_RPC_([1-9][0-9]*)_URLS$")


@dataclass(frozen=True, slots=True)
class RpcSettings:
    """Ordered RPC URLs per chain: RISK0_RPC_<ID>_URLS, ETHEREUM_RPC_URL, then defaults."""

    urls: Mapping[int, tuple[str, ...]] = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        defaults: Mapping[int, tuple[str, ...]] = PUBLIC_RPC_URLS,
    ) -> "RpcSettings":
        environ = os.environ if environ is None else environ
        configured: dict[int, list[str]] = {}
        for name in sorted(environ):
            match = _RPC_URLS_VARIABLE.match(name)
            if match is None:
                continue
            urls = [url.strip() for url in environ[name].split(",") if url.strip()]
            configured[int(match.group(1))] = [_validated_rpc_url(url, name) for url in urls]
        legacy = environ.get("ETHEREUM_RPC_URL", "").strip()
        if legacy:
            configured.setdefault(1, []).append(
                _validated_rpc_url(legacy, "ETHEREUM_RPC_URL")
            )
        merged: dict[int, tuple[str, ...]] = {}
        for chain_id in sorted(set(configured) | set(defaults)):
            ordered = [*configured.get(chain_id, ()), *defaults.get(chain_id, ())]
            merged[chain_id] = tuple(dict.fromkeys(ordered))
        return cls(urls=merged)

    def __repr__(self) -> str:
        return f"RpcSettings(chains={sorted(self.urls)}, urls=<redacted>)"


def _validated_rpc_url(url: str, variable: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        # The value may embed a key, so only the variable name is reported.
        raise ValueError(f"{variable} must contain absolute HTTP(S) URLs")
    return url


RADAR_CHAIN_IDS_ENV = "RISK0_RADAR_CHAIN_IDS"
# Ethereum mainnet only, unless more chains are opted into explicitly.
DEFAULT_RADAR_CHAIN_IDS: tuple[int, ...] = (1,)
_CHAIN_ID = re.compile(r"^[1-9][0-9]*$")


def radar_chain_ids(environ: Mapping[str, str] | None = None) -> tuple[int, ...]:
    """Chains the radar discovers and monitors: RISK0_RADAR_CHAIN_IDS (comma-separated)."""
    environ = os.environ if environ is None else environ
    value = environ.get(RADAR_CHAIN_IDS_ENV, "").strip()
    if not value:
        return DEFAULT_RADAR_CHAIN_IDS
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items or any(_CHAIN_ID.match(item) is None for item in items):
        raise ValueError(
            f"{RADAR_CHAIN_IDS_ENV} must be a comma-separated list of positive "
            "integer chain IDs (for example '1' or '1,8453')"
        )
    return tuple(sorted({int(item) for item in items}))
