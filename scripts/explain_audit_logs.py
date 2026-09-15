"""Measure audit query plans on mdm_test; all fixture and index writes roll back."""

import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from mdm.application.audit_logs import AuditLogQuery, AuditSource
from mdm.infrastructure.database import create_engine
from mdm.infrastructure.repositories.audit_logs import build_audit_log_statement
from mdm.infrastructure.settings import Settings

ROWS = 10000


async def main() -> None:
    engine = create_engine(Settings().reveal_database_url())
    result: dict[str, object] = {"rows_added_per_table": ROWS, "plans": {}}
    plans = result["plans"]
    assert isinstance(plans, dict)
    try:
        async with engine.connect() as connection:
            if await connection.scalar(text("SELECT current_database()")) != "mdm_test":
                raise RuntimeError("This experiment requires mdm_test")
            result["postgresql_version"] = await connection.scalar(text("SELECT version()"))
            await connection.rollback()
            transaction = await connection.begin()
            try:
                change_set = await connection.scalar(
                    text("SELECT change_set_id FROM master_code_logs LIMIT 1")
                )
                if change_set is None:
                    raise RuntimeError(
                        "First run test_approved_change_set_pages_include_every_typed_log_once"
                    )
                for source in AuditSource:
                    table = (
                        "master_code_logs"
                        if source is AuditSource.MASTER_CODE
                        else f"dimension_{source.value.lower()}_logs"
                    )
                    await connection.execute(text(f"DROP INDEX IF EXISTS ix_{table}_changed_at_id"))
                    if source is AuditSource.MASTER_CODE:
                        await connection.execute(
                            text(f"""
                            INSERT INTO master_code_logs
                                (master_code_id, change_set_id, master_code_version, operation,
                                 old_state, new_state, reason, actor_kind,
                                 actor_id, actor_role, changed_at)
                            SELECT seed.master_code_id, uuidv7(), last_version + g, 'RECOMPOSE',
                                   seed.new_state,
                                   jsonb_set(seed.new_state, '{{code}}',
                                       to_jsonb('X' || (seed.new_state->>'code'))),
                                   seed.reason, seed.actor_kind, seed.actor_id, seed.actor_role,
                                   clock_timestamp() - g * interval '1 second'
                            FROM (SELECT * FROM master_code_logs ORDER BY id LIMIT 1) seed
                            CROSS JOIN (SELECT max(master_code_version) AS last_version
                                        FROM master_code_logs) latest
                            CROSS JOIN generate_series(1, {ROWS}) g
                        """)
                        )
                    else:
                        await connection.execute(
                            text(f"""
                            INSERT INTO {table}
                                (dimension_id, change_set_id, dimension_version,
                                 operation, field_name,
                                 old_value, new_value, reason, actor_kind,
                                 actor_id, actor_role, changed_at)
                            SELECT seed.dimension_id, uuidv7(), seed.dimension_version,
                                   seed.operation,
                                   seed.field_name, seed.old_value, seed.new_value, seed.reason,
                                   seed.actor_kind, seed.actor_id, seed.actor_role,
                                   clock_timestamp() - g * interval '1 second'
                            FROM (SELECT * FROM {table} WHERE field_name = 'CODE'
                                  ORDER BY id LIMIT 1) seed
                            CROSS JOIN generate_series(1, {ROWS}) g
                        """)
                        )
                    await connection.execute(text(f"ANALYZE {table}"))
                for phase in ("before", "after"):
                    for source in AuditSource:
                        query = AuditLogQuery(source)
                        compiled = build_audit_log_statement(query, after=None, limit=50).compile(
                            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                        )
                        plan = await connection.scalar(
                            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled))
                        )
                        plans[f"{phase}:{source.value}"] = (
                            json.loads(plan) if isinstance(plan, str) else plan
                        )
                    query = AuditLogQuery(change_set_id=change_set)
                    compiled = build_audit_log_statement(query, after=None, limit=50).compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                    plan = await connection.scalar(
                        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled))
                    )
                    plans[f"{phase}:CHANGE_SET"] = (
                        json.loads(plan) if isinstance(plan, str) else plan
                    )
                    if phase == "before":
                        for source in AuditSource:
                            table = (
                                "master_code_logs"
                                if source is AuditSource.MASTER_CODE
                                else f"dimension_{source.value.lower()}_logs"
                            )
                            await connection.execute(
                                text(
                                    f"CREATE INDEX ix_{table}_changed_at_id "
                                    f"ON {table} (changed_at, id)"
                                )
                            )
                output = Path(".artifacts/audit-query-plans.json")
                output.parent.mkdir(exist_ok=True)
                await asyncio.to_thread(
                    output.write_text, json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                )
                print(output)
                for name, value in plans.items():
                    plan = value[0]
                    print(f"{name}: {plan['Execution Time']:.3f} ms")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
