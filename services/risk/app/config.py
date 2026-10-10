from dataclasses import dataclass
from datetime import timedelta
import os


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
    ethereum_rpc_url: str

    def __post_init__(self) -> None:
        if not self.ethereum_rpc_url.strip():
            raise ValueError("ETHEREUM_RPC_URL must be configured")

    @classmethod
    def from_environment(cls) -> "Settings":
        rpc_url = os.environ.get("ETHEREUM_RPC_URL", "")
        if not rpc_url:
            raise ValueError("ETHEREUM_RPC_URL must be configured")
        return cls(ethereum_rpc_url=rpc_url)
