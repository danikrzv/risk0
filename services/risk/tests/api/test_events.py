import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.events import RadarEventBroker, encode_sse
from app.main import app


def test_events_have_monotonic_sequences_and_no_graph_payload():
    broker = RadarEventBroker(capacity=10)

    first = broker.publish("graph.updated", "graph", {"processed_count": 12})
    second = broker.publish("incident.opened", "incident-1", {"severity": "CRITICAL"})

    assert first.sequence == 1
    assert second.sequence == 2
    assert [event.sequence for event in broker.events_after(0)] == [1, 2]
    payload = encode_sse(second)
    assert "id: 2" in payload
    assert "event: incident.opened" in payload
    assert "incident-1" in payload
    assert "entities" not in payload
    assert "relationships" not in payload


def test_reconnect_replays_only_events_after_last_event_id():
    broker = RadarEventBroker(capacity=10)
    broker.publish("graph.updated", "graph", {})
    broker.publish("incident.opened", "incident-1", {})
    broker.publish("notification.created", "notification-1", {})

    assert [event.sequence for event in broker.events_after(1)] == [2, 3]
    assert [event.sequence for event in broker.events_after(3)] == []


@pytest.mark.anyio
async def test_stream_emits_heartbeat_when_no_event_arrives():
    broker = RadarEventBroker(capacity=10, heartbeat_seconds=0, poll_seconds=0)
    stream = broker.stream(last_sequence=0)

    item = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert item == ": heartbeat\n\n"


def test_endpoint_rejects_invalid_last_event_id():
    response = TestClient(app).get(
        "/v1/events", headers={"Last-Event-ID": "not-a-sequence"}
    )

    assert response.status_code == 400
    assert "not-a-sequence" not in response.text


@pytest.mark.anyio
async def test_last_event_id_from_a_previous_process_replays_the_new_stream():
    # After a restart the sequence starts again at 1; a client reconnecting with
    # its old, higher Last-Event-ID must not miss the new process's events.
    broker = RadarEventBroker(capacity=10, heartbeat_seconds=60, poll_seconds=0)
    broker.publish("graph.updated", "graph", {})
    broker.publish("incident.opened", "incident-1", {})
    stream = broker.stream(last_sequence=57)

    first = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert first.startswith("id: 1\n")


def test_events_after_returns_only_newer_events_from_a_full_buffer():
    broker = RadarEventBroker(capacity=3)
    for index in range(7):
        broker.publish("graph.updated", "graph", {"index": index})

    assert [event.sequence for event in broker.events_after(0)] == [5, 6, 7]
    assert [event.sequence for event in broker.events_after(5)] == [6, 7]
    assert broker.events_after(7) == ()
    assert broker.events_after(100) == ()
