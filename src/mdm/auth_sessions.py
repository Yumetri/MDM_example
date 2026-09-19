"""Composition and startup ownership for the browser session use cases."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.api.registrations import build_registration_router, unavailable_registration_router
from mdm.api.session_cookies import SessionCookies
from mdm.api.sessions import build_session_router, unavailable_session_router
from mdm.application.auth import HumanPrincipal
from mdm.application.browser_policy import BrowserProtectionPolicy
from mdm.application.registrations import CompleteRegistration, RequestRegistration
from mdm.application.sessions import GetCurrentProfile, SessionUseCases
from mdm.auth_protection import build_auth_protection
from mdm.domain.auth import PlainPassword
from mdm.infrastructure.email_delivery import build_smtp_email_sender
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.operational_events import (
    JsonLineOperationalEventSink,
    QueuedOperationalEventSink,
)
from mdm.infrastructure.passwords import build_password_hasher
from mdm.infrastructure.repositories.registrations import SqlAlchemyRegistrationRepository
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
    request_registration: RequestRegistration | None = None
    complete_registration: CompleteRegistration | None = None
    registration_repository = SqlAlchemyRegistrationRepository(session_factory)
    email_sender = (
        build_smtp_email_sender(settings) if settings.auth_registrations_enabled else None
    )
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
        nonlocal service, request_registration, complete_registration
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
            if email_sender is not None:
                request_registration = RequestRegistration(
                    registration_repository,
                    email_sender,
                    events,
                    allowed_domains=settings.auth_registration_allowed_domains,
                    clock=lambda: datetime.now(UTC),
                )
                complete_registration = CompleteRegistration(
                    registration_repository,
                    hasher,
                    signer,
                    allowed_domains=settings.auth_registration_allowed_domains,
                )
            yield
        finally:
            service = None
            request_registration = None
            complete_registration = None
            await events.aclose(grace_seconds=1)

    router = APIRouter()
    router.include_router(
        build_session_router(
            router_for=protection.router if protection is not None else unavailable_session_router,
            cookies=cookies,
            use_cases=lambda: service,
            profile=GetCurrentProfile(repository),
            principal_dependency=principal_dependency,
        )
    )
    router.include_router(
        build_registration_router(
            router_for=(
                protection.router
                if protection is not None and settings.auth_registrations_enabled
                else unavailable_registration_router
            ),
            cookies=cookies,
            request_use_case=lambda: request_registration,
            complete_use_case=lambda: complete_registration,
        )
    )
    return AuthSessionComponents(router=router, lifespan=lifespan)
