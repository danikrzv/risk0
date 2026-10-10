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
    assert "RuntimeError" in failed.message
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


class TickSleep:
    """Each loop's sleep returns only when the test releases a tick for it."""

    def __init__(self) -> None:
        self.ticks: dict[float, asyncio.Queue[None]] = {}
        self.waiting: dict[float, int] = {}

    def queue(self, delay: float) -> asyncio.Queue[None]:
        return self.ticks.setdefault(delay, asyncio.Queue())

    async def __call__(self, delay: float) -> None:
        self.waiting[delay] = self.waiting.get(delay, 0) + 1
        await self.queue(delay).get()

    def tick(self, delay: float) -> None:
        self.queue(delay).put_nowait(None)


@pytest.mark.anyio
async def test_loop_survives_a_run_repository_failure():
    from app.radar.scheduler import RadarScheduler

    class FailingOnceRepository(RecordingRunRepository):
        def record(self, run: SchedulerRun) -> None:
            if not self.runs and run.loop == "fast":
                self.runs.append(run.model_copy(update={"message": "lost"}))
                raise RuntimeError("database is locked")
            super().record(run)

    service = ImmediateService()
    repository = FailingOnceRepository()
    sleep = TickSleep()
    scheduler = RadarScheduler(service, repository, clock=lambda: NOW, sleep=sleep)

    await scheduler.start()
    await wait_until(lambda: sleep.waiting.get(300) == 1)
    sleep.tick(300)
    await wait_until(lambda: service.fast_calls == 2)
    await scheduler.stop()

    assert service.fast_calls == 2


@pytest.mark.anyio
async def test_tick_arriving_during_a_running_cycle_is_skipped_and_recorded():
    from app.radar.scheduler import RadarScheduler

    release = asyncio.Event()

    class SlowFastService(ImmediateService):
        async def run_monitoring(self, now: datetime) -> SchedulerRun:
            self.fast_calls += 1
            await release.wait()
            return completed_run("fast", f"fast-{self.fast_calls}")

    service = SlowFastService()
    repository = RecordingRunRepository()
    sleep = TickSleep()
    scheduler = RadarScheduler(service, repository, clock=lambda: NOW, sleep=sleep)

    await scheduler.start()
    await wait_until(lambda: service.fast_calls == 1 and sleep.waiting.get(300) == 1)
    sleep.tick(300)
    await wait_until(
        lambda: any(run.status == "skipped" for run in repository.runs)
    )
    release.set()
    await wait_until(lambda: any(run.run_id == "fast-1" for run in repository.runs))
    await scheduler.stop()

    fast_runs = [run for run in repository.runs if run.loop == "fast"]
    assert [run.status for run in fast_runs] == ["skipped", "completed"]
    assert service.fast_calls == 1


@pytest.mark.anyio
async def test_first_fast_cycle_waits_for_the_first_discovery():
    from app.radar.scheduler import RadarScheduler

    release_slow = asyncio.Event()
    order: list[str] = []

    class OrderedService(ImmediateService):
        async def run_discovery(self, now: datetime) -> SchedulerRun:
            await release_slow.wait()
            order.append("slow")
            return completed_run("slow", "slow-1")

        async def run_monitoring(self, now: datetime) -> SchedulerRun:
            order.append("fast")
            return completed_run("fast", "fast-1")

    scheduler = RadarScheduler(
        OrderedService(), RecordingRunRepository(), clock=lambda: NOW, sleep=TickSleep()
    )
    await scheduler.start()
    for _ in range(20):
        await asyncio.sleep(0)
    assert order == []
    release_slow.set()
    await wait_until(lambda: order == ["slow", "fast"])
    await scheduler.stop()


@pytest.mark.anyio
async def test_failed_cycle_message_never_contains_provider_details():
    from app.radar.scheduler import RadarScheduler

    class LeakyService(ImmediateService):
        async def run_monitoring(self, now: datetime) -> SchedulerRun:
            raise RuntimeError(
                "Client error for url 'https://eth-mainnet.example.invalid/v2/secret-key'"
            )

    repository = RecordingRunRepository()
    scheduler = RadarScheduler(LeakyService(), repository, clock=lambda: NOW)

    failed = await scheduler.run_fast_once()

    assert failed.status == "failed"
    assert "RuntimeError" in failed.message
    assert "secret-key" not in failed.message
    assert "example.invalid" not in failed.message


@pytest.mark.anyio
async def test_stop_cancels_in_flight_cycles_without_leaking_tasks():
    from app.radar.scheduler import RadarScheduler

    started = asyncio.Event()

    class HangingService(ImmediateService):
        async def run_discovery(self, now: datetime) -> SchedulerRun:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    scheduler = RadarScheduler(
        HangingService(), RecordingRunRepository(), clock=lambda: NOW, sleep=TickSleep()
    )
    before = asyncio.all_tasks()
    await scheduler.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await scheduler.stop()

    assert asyncio.all_tasks() == before


def test_fixture_mode_scheduler_never_builds_live_rpc_clients(monkeypatch, tmp_path):
    from app import main
    from app.api.radar import get_radar_context
    from app.fixture_mode import fixture_morpho_client

    monkeypatch.setenv("RISK0_DATA_MODE", "fixture")
    monkeypatch.setenv("RISK0_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("RISK0_RADAR_DB_PATH", str(tmp_path / "radar.sqlite3"))
    monkeypatch.setenv("RISK0_FIXTURE_CACHE_PATH", str(tmp_path / "fixture.sqlite3"))
    monkeypatch.setattr(
        main.RpcRegistry,
        "from_environment",
        classmethod(lambda cls, *args, **kwargs: pytest.fail("fixture mode opened RPC")),
    )
    get_radar_context.cache_clear()
    fixture_morpho_client.cache_clear()
    try:
        scheduler = main.build_radar_scheduler()
        chain = scheduler._service._chain_collector
        assert chain.collect_all((1,), NOW) == ()
    finally:
        get_radar_context.cache_clear()
        fixture_morpho_client.cache_clear()


def test_radar_chain_allowlist_limits_discovery_and_monitoring(monkeypatch, tmp_path):
    from app import main
    from app.api.radar import get_radar_context
    from app.fixture_mode import fixture_morpho_client

    monkeypatch.setenv("RISK0_DATA_MODE", "fixture")
    monkeypatch.setenv("RISK0_RADAR_CHAIN_IDS", "8453,1")
    monkeypatch.setenv("RISK0_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("RISK0_RADAR_DB_PATH", str(tmp_path / "radar.sqlite3"))
    monkeypatch.setenv("RISK0_FIXTURE_CACHE_PATH", str(tmp_path / "fixture.sqlite3"))
    get_radar_context.cache_clear()
    fixture_morpho_client.cache_clear()
    try:
        service = main.build_radar_scheduler()._service
        assert service._discovery._allowed == (1, 8453)
        assert service._chain_ids == (1, 8453)
    finally:
        get_radar_context.cache_clear()
        fixture_morpho_client.cache_clear()


def test_fastapi_lifespan_fails_fast_on_an_invalid_chain_allowlist(monkeypatch):
    from app import main

    monkeypatch.delenv(main.RADAR_ENABLED_ENV, raising=False)
    monkeypatch.setenv("RISK0_RADAR_CHAIN_IDS", "mainnet")

    with pytest.raises(ValueError, match="RISK0_RADAR_CHAIN_IDS"):
        with TestClient(main.app):
            pass
