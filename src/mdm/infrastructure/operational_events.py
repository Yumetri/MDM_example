"""Best-effort one-line JSON operational event output."""

import asyncio
import json
import math
import sys
from queue import Full, Queue, ShutDown
from threading import Thread
from typing import Literal, Protocol

from mdm.application.auth import OperationalEvent, OperationalEventSink


class TextStream(Protocol):
    """The minimal text output interface used by the JSON adapter."""

    def write(self, value: str, /) -> int:
        """Write text and return the number of characters accepted."""
        ...

    def flush(self) -> None:
        """Flush buffered text."""
        ...


class JsonLineOperationalEventSink:
    """Blocking writer; async callers must use QueuedOperationalEventSink."""

    def __init__(
        self,
        *,
        destination: Literal["stdout", "stderr"],
        stdout: TextStream | None = None,
        stderr: TextStream | None = None,
    ) -> None:
        self._destination = destination
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    def emit(self, event: OperationalEvent) -> None:
        payload = {
            "event": event.name,
            "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        }
        if event.request_id is not None:
            payload["request_id"] = event.request_id
        if event.client_ip is not None:
            payload["client_ip"] = str(event.client_ip)
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            stream = self._stdout if self._destination == "stdout" else self._stderr
            stream.write(f"{line}\n")
            stream.flush()
        except Exception:
            pass


class QueuedOperationalEventSink:
    """Bounded, best-effort handoff to one dedicated stream-writing thread."""

    def __init__(self, sink: OperationalEventSink, *, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("operational event queue capacity must be positive")
        self._sink = sink
        self._queue: Queue[OperationalEvent] = Queue(maxsize=capacity)
        self._worker = Thread(target=self._write_events, name="mdm-auth-log", daemon=True)
        self._accepting = False
        self._closed = False

    def start(self) -> None:
        """Start once during app startup, before accepting HTTP requests."""
        if self._closed:
            raise RuntimeError("operational event queue is already closed")
        self._worker.start()
        self._accepting = True

    def emit(self, event: OperationalEvent) -> None:
        """Drop new events when full or inactive; never write or wait for output here."""
        if not self._accepting:
            return
        try:
            self._queue.put_nowait(event)
        except (Full, ShutDown):
            pass

    async def aclose(self, *, grace_seconds: float) -> None:
        """Stop admission and bound drain time without joining on the event loop."""
        if not math.isfinite(grace_seconds) or grace_seconds < 0:
            raise ValueError("operational event shutdown grace must be finite and nonnegative")
        self._accepting = False
        self._closed = True
        self._queue.shutdown()
        try:
            if self._worker.ident is not None and grace_seconds > 0:
                await asyncio.wait_for(
                    asyncio.to_thread(self._worker.join, grace_seconds), timeout=grace_seconds
                )
        except TimeoutError:
            pass
        finally:
            # Cannot interrupt a blocked write; discard queued events and let the daemon
            # exit after its in-flight write returns, or when the process exits.
            self._queue.shutdown(immediate=True)

    def _write_events(self) -> None:
        while True:
            try:
                event = self._queue.get()
            except ShutDown:
                return
            try:
                self._sink.emit(event)
            except Exception:
                # Never recursively log an output failure to the same blocked stream.
                pass
            finally:
                self._queue.task_done()
