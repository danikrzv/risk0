from datetime import timedelta
from decimal import Decimal

import pytest

from app.contracts.models import PositionMode
from app.radar.contracts import ExposureCreateRequest, RadarEntityType
from tests.radar.repositories.builders import (
    CATALOG_VAULT,
    NOW,
    ORACLE,
    entity,
    impact,
    incident,
)


@pytest.fixture
def exposures(database):
    from app.radar.repositories import (
        ExposureRepository,
        IncidentRepository,
        TopologyRepository,
    )

    TopologyRepository(database).upsert_entities(
        (entity(RadarEntityType.ORACLE, ORACLE.rsplit(":", 1)[1]), entity()),
        seen_at=NOW,
    )
    IncidentRepository(database).upsert_active(incident())
    return ExposureRepository(database)


def test_lender_exposure_round_trips_with_exact_decimals(exposures):
    request = ExposureCreateRequest(
        entity_id=CATALOG_VAULT,
        mode=PositionMode.LENDER,
        amount=Decimal("1000000.123456789012345678"),
        numeraire="USD",
    )

    created = exposures.create(request, exposure_id="exposure-001", created_at=NOW)

    assert created.exposure_id == "exposure-001"
    assert created.created_at == NOW
    assert exposures.get("exposure-001") == created
    assert exposures.get("exposure-001").amount == Decimal("1000000.123456789012345678")
    assert exposures.get("missing") is None


def test_borrower_exposure_keeps_collateral_debt_and_ltv(exposures):
    request = ExposureCreateRequest(
        entity_id="market:1:0x" + "ab" * 32,
        mode=PositionMode.BORROWER,
        amount=Decimal("10"),
        numeraire="USD",
        collateral_amount=Decimal("10"),
        debt_amount=Decimal("20000"),
        ltv=Decimal("0.62"),
    )

    created = exposures.create(request, created_at=NOW)

    stored = exposures.get(created.exposure_id)
    assert created.exposure_id
    assert stored.ltv == Decimal("0.62")
    assert stored.debt_amount == Decimal("20000")
    assert exposures.list_exposures() == (stored,)


def test_duplicate_exposure_ids_are_rejected(exposures):
    request = ExposureCreateRequest(
        entity_id=CATALOG_VAULT, mode=PositionMode.LENDER, amount=Decimal("1"), numeraire="USD"
    )
    exposures.create(request, exposure_id="exposure-001", created_at=NOW)

    with pytest.raises(ValueError):
        exposures.create(
            request, exposure_id="exposure-001", created_at=NOW + timedelta(minutes=1)
        )


def test_exposure_impacts_are_replaced_per_exposure(exposures):
    request = ExposureCreateRequest(
        entity_id=CATALOG_VAULT, mode=PositionMode.LENDER, amount=Decimal("1"), numeraire="USD"
    )
    exposures.create(request, exposure_id="exposure-001", created_at=NOW)
    exposure_impact = impact(target_entity_id="position:1:exposure-001")

    exposures.replace_impacts("exposure-001", (exposure_impact,))
    exposures.replace_impacts("exposure-001", (exposure_impact,))

    assert exposures.impacts("exposure-001") == (exposure_impact,)
    exposures.replace_impacts("exposure-001", ())
    assert exposures.impacts("exposure-001") == ()
