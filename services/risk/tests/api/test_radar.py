from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.radar.contracts import (
    ExposureCreateRequest,
    HealthStatus,
    IncidentSeverity,
    Notification,
    RadarEntityType,
    RadarRelationship,
)
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)
from tests.radar.repositories.builders import entity, health, incident, snapshot


NOW = datetime(2026, 10, 10, 14, 7, tzinfo=UTC)
VAULT = "vault:1:vault-a"
MARKET = "market:1:market-a"


@pytest.fixture
def radar_context(tmp_path):
    from app.api.radar import RadarContext

    database = RadarDatabase(tmp_path / "radar.sqlite3")
    database.migrate()
    return RadarContext(
        topology=TopologyRepository(database),
        states=StateRepository(database),
        incidents=IncidentRepository(database),
        exposures=ExposureRepository(database),
    )


@pytest.fixture
def client(radar_context):
    from app.api.radar import get_radar_context

    app.dependency_overrides[get_radar_context] = lambda: radar_context
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_graph(context) -> None:
    vault = entity(
        key="vault-a",
        catalog=True,
        source="https://user:secret@rpc.example.invalid/v2/private-key?token=secret",
    )
    market = entity(entity_type=RadarEntityType.MARKET, key="market-a", visible=False)
    context.topology.upsert_entities((vault, market), seen_at=NOW)
    context.topology.upsert_relationships(
        (
            RadarRelationship(
                source_entity_id=VAULT,
                target_entity_id=MARKET,
                relationship_type="allocated_to",
                weight=Decimal("1"),
            ),
        ),
        seen_at=NOW,
    )
    context.states.append_snapshot(snapshot(VAULT, NOW, source="https://rpc.example.invalid/key"))
    context.states.set_health(health(VAULT, NOW, HealthStatus.WARNING))


def test_empty_database_returns_stable_empty_resources(client):
    graph = client.get("/v1/graph")
    notifications = client.get("/v1/notifications")

    assert graph.status_code == 200
    assert graph.json()["entities"] == []
    assert graph.json()["active_incidents"] == []
    assert notifications.status_code == 200
    assert notifications.json()["notifications"] == []


def test_graph_filters_and_redacts_sources(client, radar_context):
    seed_graph(radar_context)

    response = client.get(
        "/v1/graph",
        params={"chain_id": 1, "entity_type": "vault", "status": "WARNING", "visible": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["entity_id"] for item in body["entities"]] == [VAULT]
    assert body["entities"][0]["source"] == "https://rpc.example.invalid"
    assert body["relationships"] == []


def test_history_is_paginated_and_unknown_entities_are_404(client, radar_context):
    seed_graph(radar_context)
    radar_context.states.append_snapshot(
        snapshot(
            VAULT,
            NOW + timedelta(minutes=5),
            source="https://rpc.example.invalid/key",
        )
    )

    first = client.get(f"/v1/entities/{VAULT}/history", params={"limit": 1})
    missing = client.get("/v1/entities/vault:1:missing/history")

    assert first.status_code == 200
    assert len(first.json()["snapshots"]) == 1
    assert first.json()["next_cursor"] is not None
    assert first.json()["snapshots"][0]["source"] == "https://rpc.example.invalid"
    assert missing.status_code == 404
    assert "missing" not in missing.text.lower()


def test_exposure_validation_and_impact_lookup(client, radar_context):
    seed_graph(radar_context)

    invalid = client.post(
        "/v1/exposures",
        json={"entity_id": "vault:1:unknown", "mode": "lender", "amount": "10", "numeraire": "USDC"},
    )
    created = client.post(
        "/v1/exposures",
        json={"entity_id": VAULT, "mode": "lender", "amount": "1000", "numeraire": "USDC"},
    )

    assert invalid.status_code == 404
    assert created.status_code == 201
    exposure_id = created.json()["exposure_id"]
    impact_response = client.get(f"/v1/exposures/{exposure_id}/impact")
    missing = client.get("/v1/exposures/exposure-missing/impact")
    assert impact_response.status_code == 200
    assert impact_response.json()["impacts"] == []
    assert missing.status_code == 404


def test_incident_graph_notification_pagination_and_read_are_idempotent(
    client, radar_context
):
    seed_graph(radar_context)
    active = incident(
        "incident-market",
        source_entity_id=MARKET,
        severity=IncidentSeverity.CRITICAL,
    )
    radar_context.incidents.upsert_active(active)
    radar_context.incidents.add_notification(
        Notification(
            notification_id="notification-1",
            incident_id=active.incident_id,
            severity=active.severity,
            title="Market critical",
            message="Market health crossed the critical threshold.",
            created_at=NOW,
        )
    )

    graph = client.get(f"/v1/graph/incidents/{active.incident_id}")
    first_page = client.get("/v1/notifications", params={"limit": 1})
    first_read = client.post("/v1/notifications/notification-1/read")
    second_read = client.post("/v1/notifications/notification-1/read")

    assert graph.status_code == 200
    assert graph.json()["incident"]["incident_id"] == active.incident_id
    assert first_page.status_code == 200
    assert len(first_page.json()["notifications"]) == 1
    assert first_read.status_code == 200
    assert first_read.json()["read_at"] is not None
    assert second_read.status_code == 200
    assert second_read.json()["read_at"] == first_read.json()["read_at"]
    assert client.post("/v1/notifications/unknown/read").status_code == 404


def test_similarity_requires_a_vault_and_incident_filter_rejects_unknown_id(
    client, radar_context
):
    seed_graph(radar_context)

    similarities = client.get(f"/v1/similarities/{VAULT}")
    market = client.get(f"/v1/similarities/{MARKET}")
    incident_filter = client.get("/v1/graph", params={"incident_id": "unknown"})

    assert similarities.status_code == 200
    assert similarities.json()["similarities"] == []
    assert market.status_code == 422
    assert incident_filter.status_code == 404


def test_fixture_radar_replay_opens_propagates_and_resolves_incident(
    client, radar_context, monkeypatch
):
    from app.fixture_mode import seed_fixture_radar

    monkeypatch.setenv("RISK0_DATA_MODE", "fixture")
    seed_fixture_radar(radar_context)
    seed_fixture_radar(radar_context)
    created = client.post("/v1/fixtures/radar/oracle-incident/open")

    graph = client.get("/v1/graph").json()
    assert created.status_code == 201
    incident_id = created.json()["incident_id"]
    incident_graph = client.get(
        f"/v1/graph/incidents/{incident_id}"
    ).json()
    assert len(graph["entities"]) == 8
    assert graph["active_incidents"][0]["status"] == "OPEN"
    assert any(impact["revealed"] for impact in incident_graph["impacts"])

    exposure = client.post(
        "/v1/exposures",
        json={"entity_id": "vault:1:0x1111111111111111111111111111111111111111", "mode": "lender", "amount": "100000", "numeraire": "USDC"},
    ).json()
    client.post("/v1/fixtures/radar/oracle-incident/open")
    exposure_impact = client.get(f"/v1/exposures/{exposure['exposure_id']}/impact").json()
    assert exposure_impact["impacts"][0]["estimated_affected_amount"] == "60000.00"

    resolved = client.post("/v1/fixtures/radar/oracle-incident/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"
    assert client.get("/v1/graph").json()["active_incidents"] == []


def test_fixture_radar_controls_are_not_available_in_live_mode(client):
    assert client.post("/v1/fixtures/radar/oracle-incident/open").status_code == 404


FIXTURE_CATALOG_VAULT = "vault:1:0x1111111111111111111111111111111111111111"


def test_new_exposure_is_propagated_against_active_incidents_at_creation(
    client, radar_context, monkeypatch
):
    monkeypatch.setenv("RISK0_DATA_MODE", "fixture")
    assert client.post("/v1/fixtures/radar/oracle-incident/open").status_code == 201

    exposure = client.post(
        "/v1/exposures",
        json={"entity_id": FIXTURE_CATALOG_VAULT, "mode": "lender", "amount": "100000", "numeraire": "USDC"},
    ).json()
    impact = client.get(f"/v1/exposures/{exposure['exposure_id']}/impact").json()

    assert impact["impacts"][0]["estimated_affected_amount"] == "60000.00"


def test_resolving_the_fixture_incident_clears_exposure_impacts(
    client, radar_context, monkeypatch
):
    monkeypatch.setenv("RISK0_DATA_MODE", "fixture")
    client.post("/v1/fixtures/radar/oracle-incident/open")
    exposure = client.post(
        "/v1/exposures",
        json={"entity_id": FIXTURE_CATALOG_VAULT, "mode": "lender", "amount": "100000", "numeraire": "USDC"},
    ).json()
    client.post("/v1/fixtures/radar/oracle-incident/open")

    client.post("/v1/fixtures/radar/oracle-incident/resolve")
    impact = client.get(f"/v1/exposures/{exposure['exposure_id']}/impact").json()

    assert impact["impacts"] == []


@pytest.mark.parametrize(
    ("entity_id", "payload", "expected"),
    [
        (VAULT, {"mode": "lender", "amount": "10"}, 201),
        (MARKET, {"mode": "lender", "amount": "10"}, 201),
        (MARKET, {"mode": "borrower", "amount": "75", "collateral_amount": "100", "debt_amount": "75", "ltv": "0.75"}, 201),
        (VAULT, {"mode": "borrower", "amount": "75", "collateral_amount": "100", "debt_amount": "75", "ltv": "0.75"}, 422),
        ("chain:1:ethereum", {"mode": "lender", "amount": "10"}, 422),
        (VAULT, {"mode": "lender", "amount": "10", "ltv": "0.5"}, 422),
        (MARKET, {"mode": "borrower", "amount": "75", "debt_amount": "75"}, 422),
        (VAULT, {"mode": "lender", "amount": "-1"}, 422),
    ],
)
def test_exposure_target_and_mode_fields_are_validated(
    client, radar_context, entity_id, payload, expected
):
    seed_graph(radar_context)
    radar_context.topology.upsert_entities(
        (entity(entity_type=RadarEntityType.CHAIN, key="ethereum"),), seen_at=NOW
    )

    response = client.post(
        "/v1/exposures", json={"entity_id": entity_id, "numeraire": "USDC", **payload}
    )

    assert response.status_code == expected, response.text

