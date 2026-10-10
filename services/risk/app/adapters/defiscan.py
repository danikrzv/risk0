"""DeFiScan evidence adapter.

Reads the public DeFiScan index and compiled reviews, preserves the raw admin,
upgradeability, governance, dependency, audit, and update records, and
normalizes the dimensions DeFiScan covers into 0-100 evidence factors. Rules
are deliberately simple and named so the factor breakdown stays explainable.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext

from app.evidence.models import (
    EvidenceBundle,
    EvidenceDimension,
    EvidenceFactor,
    EvidenceFormatError,
    EvidenceNotFoundError,
    EvidenceObservation,
    EvidenceSource,
    EvidenceStatus,
    parse_timestamp,
)


DEFISCAN_BASE_URL = "https://www.defiscan.info"
DEFISCAN_METHODOLOGY_URL = "https://www.defiscan.info/about"
FETCH_TIMEOUT_SECONDS = 10
SUPPORTED_CHAIN = "Ethereum"
NORMALIZATION_VERSION = "defiscan-normalization-v1"

CRITICAL_IMPACT = "critical"
DELAY_MITIGATION = "delay"
UNMITIGATED_CRITICAL_PENALTY = Decimal(15)
UNMITIGATED_CRITICAL_PENALTY_CAP = Decimal(60)
EOA_CRITICAL_ADMIN_PENALTY = Decimal(20)
UPGRADEABLE_CONTRACT_PENALTY = Decimal(20)
# Any proxy type outside this set is conservatively treated as upgradeable.
NON_UPGRADEABLE_PROXY_TYPES = frozenset({"immutable", "EIP1167 proxy", "EOA", "gnosis safe"})
NAMED_DEPENDENCY_PENALTY = Decimal(15)
UNKNOWN_DEPENDENCY_PENALTY = Decimal(30)
AUDIT_POINTS = Decimal(15)
AUDIT_POINTS_CAP = Decimal(60)
VERIFICATION_POINTS = Decimal(40)
FACTOR_QUANTUM = Decimal("0.01")

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

JsonFetcher = Callable[[str], bytes]


class DefiScanUnavailableError(RuntimeError):
    """Raised when DeFiScan cannot be reached; messages never include bodies."""


@dataclass(frozen=True, slots=True)
class DefiScanProject:
    slug: str
    name: str
    chain: str
    project_type: str | None
    verified: bool
    coverage: Decimal | None


def urllib_fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "risk0-risk"}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise EvidenceNotFoundError(url) from None
        raise DefiScanUnavailableError(
            f"DeFiScan request failed with HTTP {error.code}"
        ) from None
    except (urllib.error.URLError, OSError) as error:
        raise DefiScanUnavailableError(
            f"DeFiScan request failed: {type(error).__name__}"
        ) from None


class DefiScanClient:
    def __init__(
        self,
        fetch: JsonFetcher = urllib_fetch,
        base_url: str = DEFISCAN_BASE_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch = fetch
        self._base_url = base_url.rstrip("/")
        self._clock = clock

    def get_index(self) -> tuple[DefiScanProject, ...]:
        payload = self._get_json(f"{self._base_url}/data/index.json")
        protocols = payload.get("protocols")
        if not isinstance(protocols, list):
            raise EvidenceFormatError("DeFiScan index must list protocols")
        return tuple(_parse_project(item) for item in protocols)

    def get_review(self, slug: str) -> EvidenceBundle:
        if not SLUG_PATTERN.fullmatch(slug):
            raise ValueError(f"invalid DeFiScan slug {slug!r}")
        review_url = f"{self._base_url}/data/{slug}/compiled-review.json"

        project = next((item for item in self.get_index() if item.slug == slug), None)
        if project is None:
            return self._unavailable(slug, review_url, f"DeFiScan index has no review for {slug!r}")
        if project.chain != SUPPORTED_CHAIN:
            return self._unavailable(
                slug,
                review_url,
                f"DeFiScan review for {slug!r} covers {project.chain}, not Ethereum mainnet",
            )

        retrieved_at = self._clock()
        try:
            review = self._get_json(review_url)
        except EvidenceNotFoundError:
            return self._unavailable(slug, review_url, f"DeFiScan review for {slug!r} not found")
        if review.get("project") != slug:
            raise EvidenceFormatError(f"DeFiScan review does not describe {slug!r}")

        updated_at = _optional_time(review, "lastModified")
        return EvidenceBundle(
            protocol=slug,
            source=EvidenceSource.DEFISCAN,
            status=EvidenceStatus.AVAILABLE,
            source_url=review_url,
            methodology_url=DEFISCAN_METHODOLOGY_URL,
            retrieved_at=retrieved_at,
            source_updated_at=updated_at,
            factors=_factors(review, review_url, updated_at),
            observations=_observations(review),
        )

    def _get_json(self, url: str) -> Mapping[str, object]:
        try:
            payload = json.loads(self._fetch(url), parse_float=Decimal)
        except json.JSONDecodeError as error:
            raise EvidenceFormatError(f"DeFiScan returned invalid JSON: {error.msg}") from None
        if not isinstance(payload, dict):
            raise EvidenceFormatError("DeFiScan payload must be a JSON object")
        return payload

    def _unavailable(self, slug: str, review_url: str, reason: str) -> EvidenceBundle:
        return EvidenceBundle.unavailable(
            protocol=slug,
            source=EvidenceSource.DEFISCAN,
            source_url=review_url,
            methodology_url=DEFISCAN_METHODOLOGY_URL,
            retrieved_at=self._clock(),
            reason=reason,
        )


def _parse_project(item: object) -> DefiScanProject:
    if not isinstance(item, dict):
        raise EvidenceFormatError("DeFiScan index entries must be objects")
    slug, name, chain = item.get("slug"), item.get("name"), item.get("chain")
    if not all(isinstance(value, str) and value for value in (slug, name, chain)):
        raise EvidenceFormatError("DeFiScan index entries require slug, name, and chain")
    totals = item.get("totals")
    coverage = totals.get("coverage") if isinstance(totals, dict) else None
    project_type = item.get("projectType")
    return DefiScanProject(
        slug=slug,
        name=name,
        chain=chain,
        project_type=project_type if isinstance(project_type, str) else None,
        verified=item.get("verified") is True,
        coverage=_decimal_or_none(coverage),
    )


def _factors(
    review: Mapping[str, object], review_url: str, updated_at: datetime | None
) -> tuple[EvidenceFactor, ...]:
    factors = []
    for dimension, rule in (
        (EvidenceDimension.CONTROLS_GOVERNANCE, _controls_factor),
        (EvidenceDimension.DEPENDENCIES, _dependencies_factor),
        (EvidenceDimension.AUDITS_TESTING, _audits_factor),
    ):
        result = rule(review)
        if result is None:
            continue
        value, explanation = result
        factors.append(
            EvidenceFactor(
                dimension=dimension,
                value=value,
                source=EvidenceSource.DEFISCAN,
                source_url=review_url,
                observed_at=updated_at,
                rule=f"{NORMALIZATION_VERSION}: {explanation}",
            )
        )
    return tuple(factors)


def _controls_factor(review: Mapping[str, object]) -> tuple[Decimal, str] | None:
    functions, admins, contracts = (
        review.get("functions"),
        review.get("admins"),
        review.get("contracts"),
    )
    if not all(isinstance(value, list) for value in (functions, admins, contracts)):
        return None

    unmitigated = sum(
        1
        for function in _objects(functions)
        if function.get("impact") == CRITICAL_IMPACT and not _has_delay(function)
    )
    eoa_critical = any(
        admin.get("adminType") == "EOA"
        and any(
            function.get("impact") == CRITICAL_IMPACT
            for function in _objects(admin.get("functions"))
        )
        for admin in _objects(admins)
    )
    upgradeable = [
        str(contract.get("name"))
        for contract in _objects(contracts)
        if contract.get("isExternal") is not True
        and contract.get("proxyType") not in NON_UPGRADEABLE_PROXY_TYPES
    ]

    penalty = min(UNMITIGATED_CRITICAL_PENALTY_CAP, UNMITIGATED_CRITICAL_PENALTY * unmitigated)
    if eoa_critical:
        penalty += EOA_CRITICAL_ADMIN_PENALTY
    if upgradeable:
        penalty += UPGRADEABLE_CONTRACT_PENALTY
    value = max(Decimal(0), Decimal(100) - penalty)
    return value, (
        f"100 - 15 x {unmitigated} critical functions without delay (cap 60)"
        f" - {20 if eoa_critical else 0} EOA admin with critical powers"
        f" - {20 if upgradeable else 0} upgradeable contracts ({', '.join(upgradeable) or 'none'})"
    )


def _dependencies_factor(review: Mapping[str, object]) -> tuple[Decimal, str] | None:
    if isinstance(review.get("dependencyEntityGroups"), list):
        records = _objects(review["dependencyEntityGroups"])
    elif isinstance(review.get("dependencies"), list):
        records = _objects(review["dependencies"])
    else:
        return None
    entities = [record.get("entity") for record in records]
    named = sorted({entity for entity in entities if isinstance(entity, str) and entity})
    unknown = any(not (isinstance(entity, str) and entity) for entity in entities)

    penalty = NAMED_DEPENDENCY_PENALTY * len(named)
    if unknown:
        penalty += UNKNOWN_DEPENDENCY_PENALTY
    value = max(Decimal(0), Decimal(100) - penalty)
    return value, (
        f"100 - 15 x {len(named)} named dependency entities ({', '.join(named) or 'none'})"
        f" - {30 if unknown else 0} unknown dependency entity"
    )


def _audits_factor(review: Mapping[str, object]) -> tuple[Decimal, str] | None:
    audits = review.get("audits")
    if not isinstance(audits, list):
        return None
    totals = review.get("totals") if isinstance(review.get("totals"), dict) else {}
    contract_count = _decimal_or_none(totals.get("contractCount"))
    verified_count = _decimal_or_none(totals.get("verifiedContractCount"))

    with localcontext() as context:
        context.prec = 28
        audit_points = min(AUDIT_POINTS_CAP, AUDIT_POINTS * len(audits))
        if contract_count and verified_count is not None and contract_count > 0:
            share = min(Decimal(1), verified_count / contract_count)
            verification = VERIFICATION_POINTS * share
            verification_note = f"40 x {verified_count}/{contract_count} verified contracts"
        else:
            verification = Decimal(0)
            verification_note = "0 for unknown contract verification"
        value = (audit_points + verification).quantize(FACTOR_QUANTUM, rounding=ROUND_FLOOR)
    return value, f"min(60, 15 x {len(audits)} audits) + {verification_note}"


def _observations(review: Mapping[str, object]) -> tuple[EvidenceObservation, ...]:
    items: list[EvidenceObservation] = []
    for key in ("publishedAt", "lastModified", "compiledAt"):
        observed = _optional_time(review, key)
        if observed is not None:
            items.append(EvidenceObservation("timestamp", key, str(review[key]), observed))

    for admin in _objects(review.get("admins")):
        items.append(
            EvidenceObservation(
                "admin",
                str(admin.get("name")),
                str(admin.get("adminType")),
                reference=_str_or_none(admin.get("address")),
            )
        )
    for contract in _objects(review.get("contracts")):
        proxy_type = str(contract.get("proxyType"))
        if contract.get("isExternal") is True:
            proxy_type += " (external)"
        items.append(
            EvidenceObservation(
                "upgradeability",
                str(contract.get("name")),
                proxy_type,
                reference=_str_or_none(contract.get("address")),
            )
        )

    governance = review.get("governance")
    if isinstance(governance, dict):
        for key, value in governance.items():
            if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
                items.append(EvidenceObservation("governance", key, str(value)))
            elif isinstance(value, dict):
                for nested in ("seconds", "value"):
                    if nested in value:
                        items.append(
                            EvidenceObservation("governance", f"{key}.{nested}", str(value[nested]))
                        )
                        break

    for dependency in _objects(review.get("dependencies")):
        entity = dependency.get("entity")
        items.append(
            EvidenceObservation(
                "dependency",
                str(dependency.get("name")),
                entity if isinstance(entity, str) and entity else "unknown entity",
                reference=_str_or_none(dependency.get("address")),
            )
        )
    for audit in _objects(review.get("audits")):
        items.append(
            EvidenceObservation(
                "audit",
                str(audit.get("author")),
                f"{audit.get('date')} {audit.get('scope')}",
                reference=_str_or_none(audit.get("url")),
            )
        )
    for update in _objects(review.get("activity")):
        items.append(
            EvidenceObservation(
                "update",
                str(update.get("contractName")),
                str(update.get("type")),
                observed_at=_optional_time(update, "timestamp"),
                reference=_str_or_none(update.get("address")),
            )
        )
    return tuple(items)


def _has_delay(function: Mapping[str, object]) -> bool:
    return any(
        mitigation.get("type") == DELAY_MITIGATION
        for mitigation in _objects(function.get("mitigations"))
    )


def _objects(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _optional_time(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceFormatError(f"{key} must be an ISO timestamp")
    return parse_timestamp(value)


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    return Decimal(value)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
