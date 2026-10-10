import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.radar.contracts import SchedulerRun


NOW = datetime(2026, 10, 10, 12, 10, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RecordingRunRepository:
    def __init__(self) -> None:
        self.runs: list[SchedulerRun] = []

    def record(self, run: SchedulerRun) -> None:
        self.runs.append(run)


def completed_run(loop: str, run_id: str) -> SchedulerRun:
    return SchedulerRun(
        run_id=run_id,
        loop=loop,
        status="completed",
        started_at=NOW,
        finished_at=NOW,
        processed_count=1,
        error_count=0,
        message="ok",
    )


class ImmediateService:
    def __init__(self) -> None:
        self.fast_calls = 0
        self.slow_calls = 0

    async def run_monitoring(self, now: datetime) -> SchedulerRun:
        self.fast_calls += 1
        return completed_run("fast", f"fast-{self.fast_calls}")

    async def run_discovery(self, now: datetime) -> SchedulerRun:
        self.slow_calls += 1
        return completed_run("slow", f"slow-{self.slow_calls}")


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.anyio
async def test_start_registers_both_intervals_and_start_stop_are_idempotent():
    from app.radar.scheduler import RadarScheduler

    service = ImmediateService()
    repository = RecordingRunRepository()
    observed_delays: list[float] = []
    sleeping = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        observed_delays.append(delay)
        if len(observed_delays) == 2:
            sleeping.set()
        await asyncio.Event().wait()

    scheduler = RadarScheduler(
        service,
        repository,
        clock=lambda: NOW,
        sleep=controlled_sleep,
    )

    await scheduler.start()
    await scheduler.start()
    await asyncio.wait_for(sleeping.wait(), timeout=1)

    assert sorted(observed_delays) == [300, 3600]
    assert service.fast_calls == 1
    assert service.slow_calls == 1

    await scheduler.stop()
    await scheduler.stop()


@pytest.mark.anyio
async def test_overlapping_fast_cycle_is_skipped_and_persisted_without_blocking_slow():
    from app.radar.scheduler import RadarScheduler

    fast_started = asyncio.Event()
    release_fast = asyncio.Event()

    class BlockingService(ImmediateService):
        async def run_monitoring(self, now: datetime) -> SchedulerRun:
            self.fast_calls += 1
            fast_started.set()
            await release_fast.wait()
            return completed_run("fast", "fast-completed")

    service = BlockingService()
    repository = RecordingRunRepository()
    scheduler = RadarScheduler(service, repository, clock=lambda: NOW)

    active_fast = asyncio.create_task(scheduler.run_fast_once())
    await asyncio.wait_for(fast_started.wait(), timeout=1)

    skipped = await scheduler.run_fast_once()
    slow = await scheduler.run_slow_once()

    assert skipped.status == "skipped"
    assert skipped.loop == "fast"
    assert skipped.started_at == NOW
    assert skipped.finished_at == NOW
    assert skipped.processed_count == 0
    assert skipped.error_count == 0
    assert "still running" in skipped.message
    assert repository.runs[-2] == skipped
    assert slow.status == "completed"
    assert service.slow_calls == 1

    release_fast.set()
    assert (await active_fast).status == "completed"


@pytest.mark.anyio
async def test_collector_error_is_persisted_and_does_not_prevent_future_cycles():
    from app.radar.scheduler import RadarScheduler

    class FlakyService(ImmediateService):
        async def run_monitoring(self, now: datetime) -> SchedulerRun:
            self.fast_calls += 1
            if self.fast_calls == 1:
                raise RuntimeError("collector unavailable")
            return completed_run("fast", "fast-recovered")

    service = FlakyService()
    repository = RecordingRunRepository()
    scheduler = RadarScheduler(service, repository, clock=lambda: NOW)

    failed = await scheduler.run_fast_once()
    recovered = await scheduler.run_fast_once()

    assert failed.status == "failed"
    assert failed.error_count == 1
    assert failed.finished_at == NOW
    assert "collector unavailable" in failed.message
    assert recovered.status == "completed"
    assert [run.status for run in repository.runs] == ["failed", "completed"]


@pytest.mark.anyio
async def test_partial_cycle_is_preserved_and_recorded():
    from app.radar.scheduler import RadarScheduler

    class PartialService(ImmediateService):
        async def run_discovery(self, now: datetime) -> SchedulerRun:
            return SchedulerRun(
                run_id="slow-partial",
                loop="slow",
                status="partial",
                started_at=now,
                finished_at=now,
                processed_count=8,
                error_count=1,
                message="one chain unavailable",
            )

    repository = RecordingRunRepository()
    scheduler = RadarScheduler(PartialService(), repository, clock=lambda: NOW)

    result = await scheduler.run_slow_once()

    assert result.status == "partial"
    assert result.processed_count == 8
    assert repository.runs == [result]


@pytest.mark.anyio
async def test_synchronous_service_cycles_run_without_blocking_the_contract():
    from app.radar.scheduler import RadarScheduler

    class SynchronousService:
        def run_monitoring(self, now: datetime) -> SchedulerRun:
            return completed_run("fast", "fast-sync")

        def run_discovery(self, now: datetime) -> SchedulerRun:
            return completed_run("slow", "slow-sync")

    repository = RecordingRunRepository()
    scheduler = RadarScheduler(SynchronousService(), repository, clock=lambda: NOW)

    result = await scheduler.run_fast_once()

    assert result.run_id == "fast-sync"
    assert repository.runs == [result]


def test_fastapi_lifespan_starts_and_stops_opt_in_scheduler(monkeypatch):
    from app import main

    class LifecycleScheduler:
        started = False
        stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    scheduler = LifecycleScheduler()
    monkeypatch.setenv(main.RADAR_ENABLED_ENV, "true")
    monkeypatch.setattr(main, "build_radar_scheduler", lambda: scheduler)

    with TestClient(main.app) as client:
        assert client.get("/v1/health").status_code == 200
        assert scheduler.started is True
        assert scheduler.stopped is False

    assert scheduler.stopped is True


def test_fastapi_lifespan_leaves_radar_disabled_by_default(monkeypatch):
    from app import main

    monkeypatch.delenv(main.RADAR_ENABLED_ENV, raising=False)
    monkeypatch.setattr(
        main,
        "build_radar_scheduler",
        lambda: pytest.fail("disabled radar must not be built"),
    )

    with TestClient(main.app) as client:
        assert client.get("/v1/health").status_code == 200
