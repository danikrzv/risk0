import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.config import RpcSettings


@dataclass(frozen=True, slots=True)
class RpcProvider:
    chain_id: int
    # Scheme and host only: paths, queries, and user info may carry API keys.
    label: str
    url: str = field(repr=False)

    def __repr__(self) -> str:
        return f"RpcProvider(chain_id={self.chain_id}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class RpcResult:
    chain_id: int
    result: object
    provider: str
    failures: tuple[str, ...] = ()


class RpcUnavailableError(RuntimeError):
    """Every provider of a chain failed; `failures` holds redacted reasons in order."""

    def __init__(self, chain_id: int, method: str, failures: tuple[str, ...]) -> None:
        self.chain_id = chain_id
        self.method = method
        self.failures = failures
        reason = "; ".join(failures) if failures else "no RPC provider configured"
        super().__init__(f"RPC unavailable for chain {chain_id} ({method}): {reason}")


class _ProviderFailure(Exception):
    pass


class RpcRegistry:
    """Chain-aware JSON-RPC access with ordered fallback and credential redaction."""

    def __init__(
        self,
        urls: Mapping[int, Sequence[str]],
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        self._providers = {
            chain_id: _providers(chain_id, chain_urls)
            for chain_id, chain_urls in urls.items()
        }
        self._http = http_client or httpx.Client(
            headers={"user-agent": "risk0-risk/0.1.0"}
        )
        self._timeout = httpx.Timeout(timeout_seconds)
        self._verified: set[tuple[int, str]] = set()
        self._request_ids = itertools.count(1)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, **kwargs: object
    ) -> "RpcRegistry":
        return cls(RpcSettings.from_environment(environ).urls, **kwargs)

    def chain_ids(self) -> tuple[int, ...]:
        return tuple(sorted(chain for chain, providers in self._providers.items() if providers))

    def providers(self, chain_id: int) -> tuple[RpcProvider, ...]:
        return self._providers.get(chain_id, ())

    def call(self, chain_id: int, method: str, params: Sequence[object]) -> RpcResult:
        failures: list[str] = []
        for provider in self.providers(chain_id):
            try:
                self._verify_chain(provider)
                result = self._request(provider, method, params)
            except _ProviderFailure as failure:
                failures.append(f"{provider.label}: {failure}")
                continue
            return RpcResult(
                chain_id=chain_id,
                result=result,
                provider=provider.label,
                failures=tuple(failures),
            )
        raise RpcUnavailableError(chain_id, method, tuple(failures))

    def __repr__(self) -> str:
        return f"RpcRegistry(chains={list(self.chain_ids())}, urls=<redacted>)"

    def _verify_chain(self, provider: RpcProvider) -> None:
        key = (provider.chain_id, provider.label)
        if key in self._verified:
            return
        reported = self._request(provider, "eth_chainId", [])
        try:
            chain_id = int(str(reported), 16)
        except ValueError:
            raise _ProviderFailure("invalid chain id") from None
        if chain_id != provider.chain_id:
            raise _ProviderFailure(f"chain id mismatch ({chain_id})")
        self._verified.add(key)

    def _request(
        self, provider: RpcProvider, method: str, params: Sequence[object]
    ) -> object:
        request_id = next(self._request_ids)
        try:
            response = self._http.post(
                provider.url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": list(params),
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            # httpx messages embed the full URL, so only the error type is kept.
            raise _ProviderFailure(type(error).__name__) from None
        if response.is_error:
            raise _ProviderFailure(f"HTTP {response.status_code}")
        try:
            payload = json.loads(response.content)
        except ValueError:
            raise _ProviderFailure("invalid JSON-RPC response") from None
        if not isinstance(payload, dict):
            raise _ProviderFailure("invalid JSON-RPC response")
        if payload.get("error") is not None:
            # Provider error messages are not echoed; they can repeat credentials.
            code = (
                payload["error"].get("code")
                if isinstance(payload["error"], dict)
                else None
            )
            raise _ProviderFailure(f"JSON-RPC error {code}")
        if "result" not in payload or payload.get("id") != request_id:
            raise _ProviderFailure("invalid JSON-RPC response")
        return payload["result"]


def _providers(chain_id: int, urls: Sequence[str]) -> tuple[RpcProvider, ...]:
    providers: list[RpcProvider] = []
    seen: dict[str, int] = {}
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"RPC URLs for chain {chain_id} must be absolute HTTP(S) URLs")
        port = f":{parts.port}" if parts.port else ""
        base = f"{parts.scheme}://{parts.hostname}{port}"
        seen[base] = seen.get(base, 0) + 1
        label = base if seen[base] == 1 else f"{base}#{seen[base]}"
        providers.append(RpcProvider(chain_id=chain_id, label=label, url=url))
    return tuple(providers)
