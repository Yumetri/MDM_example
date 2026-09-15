"""Composition and startup ownership for the browser session use cases."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.api.session_cookies import SessionCookies
from mdm.api.sessions import build_session_router, unavailable_session_router
from mdm.application.auth import HumanPrincipal
from mdm.application.browser_policy import BrowserProtectionPolicy
from mdm.application.sessions import GetCurrentProfile, SessionUseCases
from mdm.auth_protection import build_auth_protection
from mdm.domain.auth import PlainPassword
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.operational_events import (
    JsonLineOperationalEventSink,
    QueuedOperationalEventSink,
)
from mdm.infrastructure.passwords import build_password_hasher
from mdm.infrastructure.repositories.sessions import SqlAlchemySessionRepository
from mdm.infrastructure.settings import Settings


@dataclass(frozen=True, slots=True)
class AuthSessionComponents:
    router: APIRouter
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_auth_sessions(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    principal_dependency: Callable[..., HumanPrincipal],
) -> AuthSessionComponents:
    repository = SqlAlchemySessionRepository(session_factory)
    service: SessionUseCases | None = None
    events = QueuedOperationalEventSink(
        JsonLineOperationalEventSink(destination="stderr"),
        capacity=settings.auth_log_queue_capacity,
    )
    protection = (
        build_auth_protection(settings, event_sink=events)
        if settings.auth_sessions_enabled
        else None
    )
    signer = build_access_jwt_codec(settings) if settings.auth_sessions_enabled else None
    hasher = build_password_hasher(settings)
    cookies = SessionCookies(
        BrowserProtectionPolicy(
            allowed_origins=settings.auth_allowed_origins,
            allow_insecure_local_cookies=settings.auth_allow_insecure_local_cookies,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal service
        del app
        if not settings.auth_sessions_enabled:
            yield
            return
        assert signer is not None
        events.start()
        try:
            # Once per process, using the exact same bounded adapter and settings as real users.
            fake_hash = await hasher.hash(PlainPassword("unused fake password for login timing"))
            service = SessionUseCases(
                repository,
                hasher,
                signer,
                events,
                fake_password_hash=fake_hash,
                clock=lambda: datetime.now(UTC),
            )
            yield
        finally:
            service = None
            await events.aclose(grace_seconds=1)

    return AuthSessionComponents(
        router=build_session_router(
            router_for=protection.router if protection is not None else unavailable_session_router,
            cookies=cookies,
            use_cases=lambda: service,
            profile=GetCurrentProfile(repository),
            principal_dependency=principal_dependency,
        ),
        lifespan=lifespan,
    )
