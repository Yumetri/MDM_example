import io
import json
from datetime import UTC, datetime
from typing import Literal

import pytest

from mdm.application.auth import OperationalEvent
from mdm.infrastructure.operational_events import JsonLineOperationalEventSink


@pytest.mark.unit
@pytest.mark.parametrize("destination", ["stdout", "stderr"])
def test_operational_event_sink_writes_one_sanitized_json_line(
    destination: Literal["stdout", "stderr"],
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    sink = JsonLineOperationalEventSink(
        destination=destination,
        stdout=stdout,
        stderr=stderr,
    )

    sink.emit(
        OperationalEvent(
            name="INVALID_ACCESS_TOKEN",
            occurred_at=datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC),
            request_id="request-123",
        )
    )

    output = stdout.getvalue() if destination == "stdout" else stderr.getvalue()
    assert output.endswith("\n")
    assert output.count("\n") == 1
    assert json.loads(output) == {
        "event": "INVALID_ACCESS_TOKEN",
        "occurred_at": "2033-05-18T03:33:20Z",
        "request_id": "request-123",
    }
    assert (stderr if destination == "stdout" else stdout).getvalue() == ""


@pytest.mark.unit
def test_operational_event_sink_omits_absent_request_id() -> None:
    output = io.StringIO()
    sink = JsonLineOperationalEventSink(destination="stderr", stderr=output)

    sink.emit(
        OperationalEvent(
            name="INVALID_ACCESS_TOKEN",
            occurred_at=datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC),
        )
    )

    assert "request_id" not in json.loads(output.getvalue())


class BrokenStream:
    def write(self, value: str) -> int:
        del value
        raise OSError("broken stream")

    def flush(self) -> None:
        raise AssertionError("flush must not run after write fails")


@pytest.mark.unit
def test_operational_event_sink_never_propagates_output_failure() -> None:
    sink = JsonLineOperationalEventSink(destination="stderr", stderr=BrokenStream())

    sink.emit(
        OperationalEvent(
            name="INVALID_ACCESS_TOKEN",
            occurred_at=datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC),
        )
    )
