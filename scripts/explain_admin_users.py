"""Measure actual user-list queries on an isolated temporary mdm_test table."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import event, literal, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker

from mdm.application.admin_users import UserCursor, UserFilters
from mdm.domain.auth import UserRole
from mdm.infrastructure.database import create_engine
from mdm.infrastructure.repositories.admin_users import SqlAlchemyAdminUserRepository
from mdm.infrastructure.settings import Settings

ROWS = 100_000


async def main() -> None:
    engine = create_engine(Settings().reveal_database_url())
    plans: dict[str, Any] = {}
    try:
        async with engine.connect() as connection:
            if await connection.scalar(text("SELECT current_database()")) != "mdm_test":
                raise RuntimeError("This experiment requires mdm_test")
            try:
                # The temporary users table shadows public.users only in this connection.
                await connection.execute(
                    text("CREATE TEMP TABLE users (LIKE public.users INCLUDING ALL) ON COMMIT DROP")
                )
                indexes = (
                    (
                        await connection.execute(
                            text("""
                            SELECT indexname FROM pg_indexes
                            WHERE tablename = 'users' AND schemaname LIKE 'pg_temp_%'
                              AND indexdef LIKE '%created_at DESC%'
                        """)
                        )
                    )
                    .scalars()
                    .all()
                )
                for name in indexes:
                    quoted = connection.dialect.identifier_preparer.quote_identifier(name)
                    await connection.execute(text(f"DROP INDEX pg_temp.{quoted}"))
                await connection.execute(
                    text("""
                        INSERT INTO users
                            (id, normalized_email, name, password_hash, role, status,
                             created_at, updated_at)
                        SELECT md5(g::text)::uuid, 'plan-' || g || '@example.com',
                               'Synthetic user', 'unused',
                               CASE WHEN g % 10 < 8 THEN 'USER'
                                    WHEN g % 10 = 8 THEN 'ADMIN' ELSE 'SUPER_ADMIN' END,
                               'ACTIVE',
                               '2026-01-01'::timestamptz + g * interval '1 second',
                               '2026-01-01'::timestamptz + g * interval '1 second'
                        FROM generate_series(1, :rows) g
                    """),
                    {"rows": ROWS},
                )
                await connection.execute(text("ANALYZE pg_temp.users"))
                repository = SqlAlchemyAdminUserRepository(
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    )
                )
                boundary_id = await connection.scalar(text("SELECT md5('50000')::uuid"))
                assert isinstance(boundary_id, UUID)
                boundary = UserCursor(
                    datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=50_000), boundary_id
                )
                for phase in ("before", "after"):
                    if phase == "after":
                        await connection.execute(
                            text(
                                "CREATE INDEX review_users_created_at_id "
                                "ON pg_temp.users (created_at DESC, id DESC)"
                            )
                        )
                        await connection.execute(
                            text(
                                "CREATE INDEX review_users_role_created_at_id "
                                "ON pg_temp.users (role, created_at DESC, id DESC)"
                            )
                        )
                        await connection.execute(text("ANALYZE pg_temp.users"))
                    for scope, visible_role, filters in (
                        ("all", None, UserFilters()),
                        ("admin_user_only", UserRole.USER, UserFilters()),
                        ("filter_admin", None, UserFilters(role=UserRole.ADMIN)),
                        ("filter_super_admin", None, UserFilters(role=UserRole.SUPER_ADMIN)),
                    ):
                        for page, after in (("first", None), ("deep", boundary)):
                            captured: list[tuple[str, Any]] = []

                            def record(
                                conn,
                                cursor,
                                statement,
                                parameters,
                                context,
                                executemany,
                                captured=captured,
                            ):
                                if statement.startswith("SELECT users."):
                                    captured.append((statement, parameters))

                            event.listen(
                                connection.sync_connection, "before_cursor_execute", record
                            )
                            try:
                                await repository.list_users(
                                    visible_role=visible_role,
                                    filters=filters,
                                    after=after,
                                    limit=50,
                                )
                            finally:
                                event.remove(
                                    connection.sync_connection, "before_cursor_execute", record
                                )
                            assert len(captured) == 1
                            statement, parameters = captured[0]
                            arguments = ", ".join(
                                str(
                                    literal(
                                        UUID(str(value)) if isinstance(value, UUID) else value
                                    ).compile(
                                        dialect=postgresql.dialect(),
                                        compile_kwargs={"literal_binds": True},
                                    )
                                )
                                for value in parameters
                            )
                            await connection.exec_driver_sql(
                                "PREPARE review_user_page AS " + statement
                            )
                            try:
                                for mode in ("force_custom_plan", "force_generic_plan"):
                                    await connection.execute(
                                        text(f"SET LOCAL plan_cache_mode = {mode}")
                                    )
                                    result = await connection.exec_driver_sql(
                                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                                        f"EXECUTE review_user_page ({arguments})"
                                    )
                                    plan = result.scalar_one()
                                    if isinstance(plan, str):
                                        plan = json.loads(plan)
                                    key = f"{phase}:{scope}:{page}:{mode}"
                                    plans[key] = plan
                                    node = plan[0]["Plan"]["Plans"][0]
                                    print(
                                        f"{key}: {node['Node Type']}, "
                                        f"{plan[0]['Execution Time']:.3f} ms"
                                    )
                            finally:
                                await connection.exec_driver_sql("DEALLOCATE review_user_page")
            finally:
                await connection.rollback()
        output = Path(".artifacts/admin-user-query-plans.json")
        output.parent.mkdir(exist_ok=True)
        await asyncio.to_thread(
            output.write_text, json.dumps({"rows": ROWS, "plans": plans}, indent=2) + "\n"
        )
        print(output)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
