import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from app.radar.contracts import SchedulerRun


logger = logging.getLogger("risk0.radar.scheduler")

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
        self._cycles: set[asyncio.Task[SchedulerRun]] = set()
        self._discovered: asyncio.Event | None = None

    async def start(self) -> None:
        if self._tasks:
            return
        # Monitoring needs a discovery batch: the fast loop's first tick waits
        # for the first slow cycle to finish (whatever its outcome).
        discovered = asyncio.Event()
        self._discovered = discovered
        self._tasks = {
            "fast": asyncio.create_task(
                self._run_loop(
                    self.run_fast_once,
                    self.FAST_INTERVAL_SECONDS,
                    wait_for=discovered,
                ),
                name="radar-fast-loop",
            ),
            "slow": asyncio.create_task(
                self._run_loop(
                    self.run_slow_once,
                    self.SLOW_INTERVAL_SECONDS,
                    done=discovered,
                ),
                name="radar-slow-loop",
            ),
        }

    async def stop(self) -> None:
        if not self._tasks:
            return
        tasks = (*self._tasks.values(), *self._cycles)
        self._tasks = {}
        self._discovered = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cycles.clear()

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
        *,
        wait_for: asyncio.Event | None = None,
        done: asyncio.Event | None = None,
    ) -> None:
        if wait_for is not None:
            await wait_for.wait()
        while True:
            # Ticks are fixed-rate: a cycle runs as its own task, so a tick that
            # arrives while the previous cycle still runs is skipped and recorded
            # by `_run_once` instead of silently stretching the interval.
            task = asyncio.create_task(self._guarded(cycle, done))
            self._cycles.add(task)
            task.add_done_callback(self._cycles.discard)
            await self._sleep(interval_seconds)

    @staticmethod
    async def _guarded(
        cycle: Callable[[], Awaitable[SchedulerRun]], done: asyncio.Event | None
    ) -> SchedulerRun | None:
        try:
            return await cycle()
        except Exception as error:
            # Never let one failure (e.g. a locked database while recording)
            # end the loop; the next tick runs normally.
            logger.error("radar cycle crashed: %s", type(error).__name__)
            return None
        finally:
            if done is not None:
                done.set()

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
            logger.warning("radar %s cycle skipped: previous cycle still running", loop)
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
                    # Exception text can carry provider URLs or bodies (and so RPC
                    # credentials); persist and log only the exception type.
                    message=f"Cycle failed: {type(error).__name__}",
                )
                logger.error("radar %s cycle failed: %s", loop, type(error).__name__)
            self._run_repository.record(result)
            return result

    @staticmethod
    def _run_id(loop: LoopName) -> str:
        return f"{loop}-{uuid4().hex}"
