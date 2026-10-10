import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse


RadarEventType = Literal[
    "graph.updated",
    "incident.opened",
    "incident.updated",
    "incident.resolved",
    "notification.created",
]
EVENT_TYPES = {
    "graph.updated",
    "incident.opened",
    "incident.updated",
    "incident.resolved",
    "notification.created",
}


@dataclass(frozen=True, slots=True)
class RadarEvent:
    sequence: int
    event_type: RadarEventType
    resource_id: str
    summary: Mapping[str, object]


class RadarEventBroker:
    """Process-local bounded event replay for the local MVP SSE stream."""

    def __init__(
        self,
        *,
        capacity: int = 1_000,
        heartbeat_seconds: float = 15,
        poll_seconds: float = 0.25,
    ) -> None:
        if capacity <= 0:
            raise ValueError("event capacity must be positive")
        if heartbeat_seconds < 0 or poll_seconds < 0:
            raise ValueError("event intervals must be non-negative")
        self._events: deque[RadarEvent] = deque(maxlen=capacity)
        self._sequence = 0
        self._lock = threading.Lock()
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds

    def publish(
        self,
        event_type: RadarEventType,
        resource_id: str,
        summary: Mapping[str, object],
    ) -> RadarEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported radar event type: {event_type}")
        if not resource_id:
            raise ValueError("event resource_id must not be empty")
        with self._lock:
            self._sequence += 1
            event = RadarEvent(
                sequence=self._sequence,
                event_type=event_type,
                resource_id=resource_id,
                summary=dict(summary),
            )
            self._events.append(event)
            return event

    def events_after(self, sequence: int) -> tuple[RadarEvent, ...]:
        with self._lock:
            return tuple(event for event in self._events if event.sequence > sequence)

    async def stream(self, *, last_sequence: int) -> AsyncIterator[str]:
        sequence = last_sequence
        heartbeat_at = time.monotonic() + self._heartbeat_seconds
        while True:
            events = self.events_after(sequence)
            if events:
                for event in events:
                    sequence = event.sequence
                    yield encode_sse(event)
                heartbeat_at = time.monotonic() + self._heartbeat_seconds
                continue
            if time.monotonic() >= heartbeat_at:
                yield ": heartbeat\n\n"
                heartbeat_at = time.monotonic() + self._heartbeat_seconds
            await asyncio.sleep(self._poll_seconds)


def encode_sse(event: RadarEvent) -> str:
    data = json.dumps(
        {
            "sequence": event.sequence,
            "type": event.event_type,
            "resource_id": event.resource_id,
            "summary": event.summary,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"


_broker = RadarEventBroker()


def get_event_broker() -> RadarEventBroker:
    return _broker


router = APIRouter(prefix="/v1")


@router.get("/events")
async def events(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    broker: RadarEventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    try:
        last_sequence = 0 if last_event_id is None else int(last_event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.") from None
    if last_sequence < 0:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.")

    async def disconnected_stream() -> AsyncIterator[str]:
        async for item in broker.stream(last_sequence=last_sequence):
            if await request.is_disconnected():
                break
            yield item

    return StreamingResponse(
        disconnected_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
