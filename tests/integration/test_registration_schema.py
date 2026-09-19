from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.infrastructure.database import create_engine
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def registration_schema_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE registration_challenges CASCADE"))
        yield engine
    finally:
        await engine.dispose()


async def add_challenge(connection):
    return await connection.scalar(
        text(
            "INSERT INTO registration_challenges (normalized_email, created_at, expires_at) "
            "VALUES ('person@example.net', clock_timestamp(), "
            "clock_timestamp() + interval '1 day') "
            "RETURNING id"
        )
    )


async def test_registration_has_only_one_active_challenge_per_email(registration_schema_engine):
    async with registration_schema_engine.begin() as connection:
        first = await add_challenge(connection)
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await add_challenge(connection)
        await connection.execute(
            text("UPDATE registration_challenges SET status='EXPIRED' WHERE id=:id"), {"id": first}
        )
        assert await add_challenge(connection) != first


@pytest.mark.parametrize("kind", ["short_digest", "used_before_issued", "duplicate_digest"])
async def test_registration_token_constraints(registration_schema_engine, kind):
    async with registration_schema_engine.begin() as connection:
        challenge = await add_challenge(connection)
        issued = datetime.now(UTC)
        values = {"challenge": challenge, "digest": b"a" * 32, "issued": issued, "used": None}
        statement = text(
            "INSERT INTO registration_tokens (challenge_id, digest, issued_at, used_at) "
            "VALUES (:challenge, :digest, :issued, :used)"
        )
        await connection.execute(statement, values)
        if kind == "short_digest":
            values["digest"] = b"short"
        elif kind == "used_before_issued":
            values.update(digest=b"b" * 32, used=issued - timedelta(seconds=1))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(statement, values)


@pytest.mark.parametrize(
    "change", ["status='REVOKED'", "status='COMPLETED'", "expires_at=created_at"]
)
async def test_registration_challenge_state_constraints(registration_schema_engine, change):
    async with registration_schema_engine.begin() as connection:
        await add_challenge(connection)
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text(f"UPDATE registration_challenges SET {change}"))
