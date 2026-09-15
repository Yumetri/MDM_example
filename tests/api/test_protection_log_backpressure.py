import asyncio
import base64
import json
import threading
from typing import Literal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mdm.application.request_protection import AuthAction
from mdm.auth_protection import build_auth_protection
from mdm.infrastructure.operational_events import JsonLineOperationalEventSink
from mdm.infrastructure.settings import Settings


class BlockedStream:
    def __init__(self, phase: Literal["write", "flush"]) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.phase = phase
        self.lines: list[str] = []

    def write(self, value: str) -> int:
        if self.phase == "write":
            self.entered.set()
            self.release.wait(timeout=5)
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        if self.phase == "flush":
            self.entered.set()
            self.release.wait(timeout=5)


@pytest.mark.api
@pytest.mark.parametrize("phase", ["write", "flush"])
async def test_default_protection_logging_does_not_stall_other_requests(
    monkeypatch: pytest.MonkeyPatch,
    phase: Literal["write", "flush"],
) -> None:
    stream = BlockedStream(phase)
    monkeypatch.setattr(
        "mdm.auth_protection.JsonLineOperationalEventSink",
        lambda **_: JsonLineOperationalEventSink(destination="stderr", stderr=stream),
    )
    components = build_auth_protection(
        Settings(
            _env_file=None,
            database_url="postgresql://local",
            auth_ip_hmac_secret=base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        )
    )
    app = FastAPI(lifespan=components.lifespan)
    router = components.router(AuthAction.LOGIN)

    @router.post("/login")
    async def login() -> dict[str, str]:
        raise AssertionError("missing Origin must reject before the endpoint")

    @app.get("/other")
    async def other() -> dict[str, bool]:
        return {"stream_still_blocked": not stream.release.is_set()}

    app.include_router(router)
    # A real thread releases the writer even if the event loop is blocked by the bug.
    watchdog = threading.Timer(1, stream.release.set)
    watchdog.start()
    async with app.router.lifespan_context(app):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                rejected = asyncio.create_task(client.post("/login"))
                assert await asyncio.to_thread(stream.entered.wait, 2)
                response = await client.get("/other")
                assert response.json() == {"stream_still_blocked": True}
                assert (await rejected).status_code == 403
        finally:
            stream.release.set()
            watchdog.cancel()
            watchdog.join()
    assert len(stream.lines) == 1
    assert json.loads(stream.lines[0])["event"] == "ORIGIN_VALIDATION_FAILED"
