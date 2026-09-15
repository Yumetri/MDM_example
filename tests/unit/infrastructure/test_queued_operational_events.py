import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from mdm.application.auth import OperationalEvent
from mdm.infrastructure.operational_events import QueuedOperationalEventSink


def event(request_id: str) -> OperationalEvent:
    return OperationalEvent(
        name="ORIGIN_VALIDATION_FAILED",
        occurred_at=datetime(2033, 5, 18, tzinfo=UTC),
        request_id=request_id,
    )


class ControlledWriter:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[OperationalEvent] = []
        self.thread_ids: list[int] = []
        self.fail_first = fail_first

    def emit(self, value: OperationalEvent) -> None:
        self.thread_ids.append(threading.get_ident())
        if not self.events:
            self.entered.set()
            self.release.wait(timeout=5)
        self.events.append(value)
        if self.fail_first and len(self.events) == 1:
            raise OSError("output failed")


@pytest.mark.unit
@pytest.mark.parametrize("fail_first", [False, True])
async def test_queue_drops_new_overflow_preserves_order_and_survives_writer_failure(
    fail_first: bool,
) -> None:
    writer = ControlledWriter(fail_first=fail_first)
    sink = QueuedOperationalEventSink(writer, capacity=2)
    sink.start()
    try:
        sink.emit(event("in-flight"))
        assert await asyncio.to_thread(writer.entered.wait, 2)
        sink.emit(event("first-pending"))
        sink.emit(event("second-pending"))
        for _ in range(100):
            sink.emit(event("dropped"))
    finally:
        writer.release.set()
        await sink.aclose(grace_seconds=1)

    sink.emit(event("after-close"))
    assert [value.request_id for value in writer.events] == [
        "in-flight",
        "first-pending",
        "second-pending",
    ]
    assert len(set(writer.thread_ids)) == 1
    assert writer.thread_ids[0] != threading.get_ident()
    assert not sink._worker.is_alive()


@pytest.mark.unit
async def test_shutdown_does_not_wait_indefinitely_or_block_other_coroutines() -> None:
    writer = ControlledWriter()
    sink = QueuedOperationalEventSink(writer, capacity=2)
    sink.start()
    sink.emit(event("in-flight"))
    assert await asyncio.to_thread(writer.entered.wait, 2)
    sink.emit(event("discard-at-deadline"))
    watchdog = threading.Timer(2, writer.release.set)
    watchdog.start()
    try:
        closing = asyncio.create_task(sink.aclose(grace_seconds=0.1))
        await asyncio.sleep(0.01)
        assert not closing.done(), "joining must allow other coroutines to run during grace"
        await asyncio.wait_for(closing, timeout=1)
        assert not writer.release.is_set(), "shutdown must finish while output remains blocked"
        sink.emit(event("after-close"))
    finally:
        writer.release.set()
        watchdog.cancel()
        watchdog.join()
        await asyncio.to_thread(sink._worker.join, 1)
    assert [value.request_id for value in writer.events] == ["in-flight"]
    assert not sink._worker.is_alive()


@pytest.mark.unit
async def test_unused_queue_owns_no_running_thread_and_drops_inactive_events() -> None:
    writer = ControlledWriter()
    sink = QueuedOperationalEventSink(writer, capacity=1)
    sink.emit(event("before-start"))
    assert sink._worker.ident is None
    await sink.aclose(grace_seconds=0)
    assert writer.events == []
    with pytest.raises(RuntimeError, match="closed"):
        sink.start()


@pytest.mark.unit
async def test_shutdown_stops_an_idle_worker() -> None:
    sink = QueuedOperationalEventSink(ControlledWriter(), capacity=1)
    sink.start()
    await sink.aclose(grace_seconds=1)
    assert not sink._worker.is_alive()


@pytest.mark.unit
@pytest.mark.parametrize("capacity", [0, -1])
def test_queue_requires_a_positive_bound(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        QueuedOperationalEventSink(ControlledWriter(), capacity=capacity)


@pytest.mark.unit
def test_shutdown_deadline_includes_waiting_for_the_shared_executor() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = asyncio.Event()
        release = threading.Event()

        def occupy_executor() -> None:
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)

        busy = loop.run_in_executor(None, occupy_executor)
        await asyncio.wait_for(entered.wait(), timeout=2)
        sink = QueuedOperationalEventSink(ControlledWriter(), capacity=1)
        sink.start()
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        try:
            await sink.aclose(grace_seconds=0.05)
            assert not release.is_set(), "deadline must include waiting for executor admission"
        finally:
            release.set()
            watchdog.cancel()
            watchdog.join()
            await busy

    # Own a separate loop so the saturated default executor cannot leak into other tests.
    asyncio.run(scenario())
