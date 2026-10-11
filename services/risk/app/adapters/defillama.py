"""DeFiLlama public API adapter: exploit history, protocol TVL, and chain TVL.

Reads three keyless endpoints of `https://api.llama.fi`:

- `/hacks`: every incident DeFiLlama records (date, name, classification,
  technique, gross amount lost in USD, chains, and the DeFiLlama protocol id when
  DeFiLlama linked the incident to a listed protocol).
- `/protocols`: every listed protocol with its category, current TVL, per-chain
  TVL, parent protocol, and listing date. The payload is about 9 MB, so only the
  fields Risk0 uses are kept (`DefiLlamaProtocol`) and only that projection is
  cached.
- `/v2/chains`: current DeFi TVL per chain.

Each endpoint is cached in `SQLiteCache` for a day (spec § 9: risk evidence
refreshes daily) and kept usable as stale data for a week while DeFiLlama is
unreachable. Parsed envelopes are also memoized in process until they expire,
so a request does not re-validate thousands of rows. After a failed fetch with
no usable cache, further fetches are skipped for `FAILURE_BACKOFF` so an outage
does not add network timeouts to every analysis.

Errors are sanitized: messages name the endpoint and an HTTP status or an
exception type, never a response body.

TVL figures follow DeFiLlama's default view, which excludes borrowed assets;
the `<Chain>-borrowed` breakdown is kept separately for lending protocols.
"""

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import CachePolicy
from app.contracts.models import DecimalValue, FreshnessStatus
from app.domain.snapshots import DataEnvelope, SnapshotModel
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


DEFILLAMA_BASE_URL = "https://api.llama.fi"
HACKS_PATH = "/hacks"
PROTOCOLS_PATH = "/protocols"
CHAINS_PATH = "/v2/chains"
CACHE_NAMESPACE = "defillama"
DEFILLAMA_POLICY = CachePolicy(ttl=timedelta(days=1), retention=timedelta(days=7))
FAILURE_BACKOFF = timedelta(minutes=5)
DEFAULT_TIMEOUT_SECONDS = 30
ETHEREUM = "Ethereum"
BORROWED_SUFFIX = "-borrowed"

ModelT = TypeVar("ModelT", bound=BaseModel)


class DefiLlamaUnavailableError(RuntimeError):
    """DeFiLlama could not be reached or returned unusable data, and no cache applies."""


class _ProviderError(Exception):
    pass


class DefiLlamaHack(SnapshotModel):
    occurred_at: datetime
    name: str = Field(min_length=1)
    classification: str | None = None
    technique: str | None = None
    # Gross loss in USD as DeFiLlama reports it; None when DeFiLlama has no figure.
    amount_usd: DecimalValue | None = None
    returned_funds_usd: DecimalValue | None = None
    chains: tuple[str, ...] = ()
    target_type: str | None = None
    bridge_hack: bool = False
    defillama_id: str | None = None


class DefiLlamaProtocol(SnapshotModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    slug: str | None = None
    category: str | None = None
    tvl_usd: DecimalValue | None = None
    ethereum_tvl_usd: DecimalValue | None = None
    ethereum_borrowed_usd: DecimalValue | None = None
    parent_protocol: str | None = None
    listed_at: datetime | None = None
    dead_from: str | None = None
    previous_names: tuple[str, ...] = ()


class DefiLlamaChain(SnapshotModel):
    name: str = Field(min_length=1)
    chain_id: int | None = None
    tvl_usd: DecimalValue


class DefiLlamaClient:
    def __init__(
        self,
        base_url: str = DEFILLAMA_BASE_URL,
        *,
        http_client: httpx.Client | None = None,
        cache: SQLiteCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(headers={"user-agent": "risk0-risk/0.1.0"})
        self._cache = cache
        self._clock = clock
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._memo: dict[str, tuple[datetime, BaseModel]] = {}
        self._failed_until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def source_url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def hacks(self) -> DataEnvelope[tuple[DefiLlamaHack, ...]]:
        return self._load(HACKS_PATH, DataEnvelope[tuple[DefiLlamaHack, ...]], _parse_hacks)

    def protocols(self) -> DataEnvelope[tuple[DefiLlamaProtocol, ...]]:
        return self._load(
            PROTOCOLS_PATH, DataEnvelope[tuple[DefiLlamaProtocol, ...]], _parse_protocols
        )

    def chains(self) -> DataEnvelope[tuple[DefiLlamaChain, ...]]:
        return self._load(CHAINS_PATH, DataEnvelope[tuple[DefiLlamaChain, ...]], _parse_chains)

    def _load(
        self,
        path: str,
        envelope_type: type[ModelT],
        parse: Callable[[Any], tuple],
    ) -> ModelT:
        now = self._clock()
        with self._lock:
            memo = self._memo.get(path)
        if memo is not None and now < memo[0]:
            return memo[1]  # type: ignore[return-value]

        cached = self._cache.get(CACHE_NAMESPACE, path, now) if self._cache is not None else None
        if cached is not None and cached.freshness is Freshness.FRESH:
            envelope = _restore(envelope_type, cached.payload, cached.freshness)
            self._remember(path, envelope, cached.expires_at)
            return envelope

        with self._lock:
            failed_until = self._failed_until.get(path)
        try:
            if failed_until is not None and now < failed_until:
                raise _ProviderError("recent failure; retry deferred")
            fetched_at = self._clock()
            rows = parse(self._request(path))
            envelope = envelope_type.model_validate(
                {
                    "value": rows,
                    "source": f"defillama {self.source_url(path)}",
                    "fetched_at": fetched_at,
                    "freshness": FreshnessStatus.FRESH,
                }
            )
        except (_ProviderError, ValidationError, ValueError, TypeError, KeyError) as error:
            if failed_until is None or now >= failed_until:
                with self._lock:
                    self._failed_until[path] = now + FAILURE_BACKOFF
            if cached is not None and cached.freshness is not Freshness.EXPIRED:
                stale = _restore(envelope_type, cached.payload, cached.freshness)
                self._remember(path, stale, now + FAILURE_BACKOFF)
                return stale
            reason = str(error) if isinstance(error, _ProviderError) else type(error).__name__
            raise DefiLlamaUnavailableError(
                f"DeFiLlama {path} unavailable: {reason}"
            ) from None

        with self._lock:
            self._failed_until.pop(path, None)
        if self._cache is not None:
            self._cache.set(
                CACHE_NAMESPACE,
                path,
                envelope.model_dump_json().encode(),
                fetched_at,
                DEFILLAMA_POLICY,
            )
        self._remember(path, envelope, fetched_at + DEFILLAMA_POLICY.ttl)
        return envelope

    def _remember(self, path: str, envelope: BaseModel, expires_at: datetime) -> None:
        with self._lock:
            self._memo[path] = (expires_at, envelope)

    def _request(self, path: str) -> Any:
        failure = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http.get(self.source_url(path), timeout=self._timeout)
            except httpx.TransportError as error:
                failure = type(error).__name__
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    failure = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise _ProviderError(f"HTTP {response.status_code}")
                else:
                    try:
                        return json.loads(response.content, parse_float=Decimal)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise _ProviderError("invalid JSON") from None
            if attempt < self._max_attempts:
                self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
        raise _ProviderError(f"failed after {self._max_attempts} attempts: {failure}")


def _parse_hacks(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, list):
        raise _ProviderError("hacks payload is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict) or not _text(item.get("name")):
            continue
        date = _number(item.get("date"))
        if date is None:
            continue
        rows.append(
            {
                "occurred_at": datetime.fromtimestamp(int(date), UTC),
                "name": item["name"].strip(),
                "classification": _text(item.get("classification")),
                "technique": _text(item.get("technique")),
                "amount_usd": _number(item.get("amount")),
                "returned_funds_usd": _number(item.get("returnedFunds")),
                "chains": tuple(
                    chain for chain in item.get("chain") or () if isinstance(chain, str)
                ),
                "target_type": _text(item.get("targetType")),
                "bridge_hack": item.get("bridgeHack") is True,
                "defillama_id": _text(item.get("defillamaId")),
            }
        )
    return tuple(rows)


def _parse_protocols(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, list):
        raise _ProviderError("protocols payload is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        identifier, name = _text(item.get("id")), _text(item.get("name"))
        if identifier is None or name is None:
            continue
        chain_tvls = item.get("chainTvls") if isinstance(item.get("chainTvls"), dict) else {}
        listed = _number(item.get("listedAt"))
        rows.append(
            {
                "id": identifier,
                "name": name,
                "slug": _text(item.get("slug")),
                "category": _text(item.get("category")),
                "tvl_usd": _number(item.get("tvl")),
                "ethereum_tvl_usd": _number(chain_tvls.get(ETHEREUM)),
                "ethereum_borrowed_usd": _number(chain_tvls.get(ETHEREUM + BORROWED_SUFFIX)),
                "parent_protocol": _text(item.get("parentProtocol")),
                "listed_at": datetime.fromtimestamp(int(listed), UTC) if listed is not None else None,
                "dead_from": _text(item.get("deadFrom")),
                "previous_names": tuple(
                    value for value in item.get("previousNames") or () if _text(value)
                ),
            }
        )
    return tuple(rows)


def _parse_chains(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, list):
        raise _ProviderError("chains payload is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name, tvl = _text(item.get("name")), _number(item.get("tvl"))
        if name is None or tvl is None:
            continue
        chain_id = item.get("chainId")
        rows.append(
            {
                "name": name,
                "chain_id": chain_id if isinstance(chain_id, int) and not isinstance(chain_id, bool) else None,
                "tvl_usd": tvl,
            }
        )
    return tuple(rows)


def protocol_by_slug(
    protocols: tuple[DefiLlamaProtocol, ...], slug: str
) -> DefiLlamaProtocol | None:
    return next((item for item in protocols if item.slug == slug), None)


def chain_by_name(chains: tuple[DefiLlamaChain, ...], name: str) -> DefiLlamaChain | None:
    return next((item for item in chains if item.name == name), None)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None


def _restore(envelope_type: type[ModelT], payload: bytes, freshness: Freshness) -> ModelT:
    envelope = envelope_type.model_validate_json(payload)
    return envelope.model_copy(update={"freshness": FreshnessStatus(freshness.value)})

