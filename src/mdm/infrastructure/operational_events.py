"""Best-effort one-line JSON operational event output."""

import json
import sys
from typing import Literal, Protocol

from mdm.application.auth import OperationalEvent


class TextStream(Protocol):
    """The minimal text output interface used by the JSON adapter."""

    def write(self, value: str, /) -> int:
        """Write text and return the number of characters accepted."""
        ...

    def flush(self) -> None:
        """Flush buffered text."""
        ...


class JsonLineOperationalEventSink:
    """Write sanitized events to a selected process stream without raising."""

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
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            stream = self._stdout if self._destination == "stdout" else self._stderr
            stream.write(f"{line}\n")
            stream.flush()
        except Exception:
            pass
