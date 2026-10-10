import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from app.radar.contracts import SchedulerRun


FAST_INTERVAL_SECONDS = 300
SLOW_INTERVAL_SECONDS = 3600

LoopName = Literal["fast", "slow"]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
Cycle = Callable[[datetime], SchedulerRun | Awaitable[SchedulerRun]]


class RadarCycleService(Protocol):
    def run_monitoring(self, now: datetime) -> SchedulerRun | Awaitable[SchedulerRun]: ...

    def run_discovery(self, now: datetime) -> SchedulerRun | Awaitable[SchedulerRun]: ...


class SchedulerRunSink(Protocol):
    def record(self, run: SchedulerRun) -> None: ...


class RadarScheduler:
    FAST_INTERVAL_SECONDS = FAST_INTERVAL_SECONDS
    SLOW_INTERVAL_SECONDS = SLOW_INTERVAL_SECONDS

    def __init__(
        self,
        service: RadarCycleService,
        run_repository: SchedulerRunSink,
        *,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._service = service
        self._run_repository = run_repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._locks = {
            "fast": asyncio.Lock(),
            "slow": asyncio.Lock(),
        }
        self._tasks: dict[LoopName, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = {
            "fast": asyncio.create_task(
                self._run_loop(
                    self.run_fast_once,
                    self.FAST_INTERVAL_SECONDS,
                ),
                name="radar-fast-loop",
            ),
            "slow": asyncio.create_task(
                self._run_loop(
                    self.run_slow_once,
                    self.SLOW_INTERVAL_SECONDS,
                ),
                name="radar-slow-loop",
            ),
        }

    async def stop(self) -> None:
        if not self._tasks:
            return
        tasks = tuple(self._tasks.values())
        self._tasks = {}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_fast_once(self) -> SchedulerRun:
        return await self._run_once(
            loop="fast",
            cycle=self._service.run_monitoring,
        )

    async def run_slow_once(self) -> SchedulerRun:
        return await self._run_once(
            loop="slow",
            cycle=self._service.run_discovery,
        )

    async def _run_loop(
        self,
        cycle: Callable[[], Awaitable[SchedulerRun]],
        interval_seconds: int,
    ) -> None:
        while True:
            await cycle()
            await self._sleep(interval_seconds)

    async def _run_once(self, *, loop: LoopName, cycle: Cycle) -> SchedulerRun:
        lock = self._locks[loop]
        if lock.locked():
            now = self._clock()
            skipped = SchedulerRun(
                run_id=self._run_id(loop),
                loop=loop,
                status="skipped",
                started_at=now,
                finished_at=now,
                processed_count=0,
                error_count=0,
                message=f"previous {loop} cycle still running",
            )
            self._run_repository.record(skipped)
            return skipped

        async with lock:
            started_at = self._clock()
            try:
                if inspect.iscoroutinefunction(cycle):
                    result = await cycle(started_at)
                else:
                    result = await asyncio.to_thread(cycle, started_at)
                    if inspect.isawaitable(result):
                        result = await result
            except Exception as error:
                result = SchedulerRun(
                    run_id=self._run_id(loop),
                    loop=loop,
                    status="failed",
                    started_at=started_at,
                    finished_at=self._clock(),
                    processed_count=0,
                    error_count=1,
                    message=str(error) or error.__class__.__name__,
                )
            self._run_repository.record(result)
            return result

    @staticmethod
    def _run_id(loop: LoopName) -> str:
        return f"{loop}-{uuid4().hex}"
