import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.master_codes import (
    ExistingDimension,
    InlineDimension,
    InlineDimensionConflict,
    InvalidDimensionReference,
    MasterCodeConflict,
    MasterCodeCreatePlan,
    MasterCodeCursor,
    NotApplicable,
)
from mdm.domain.audit import DimensionOperation, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CompanyValue,
    CountryValue,
    DimensionCode,
    MemoryUnit,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)
from mdm.domain.master_codes import MasterCodeDimensions
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

ACTOR_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")


@pytest.fixture
async def master_code_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE master_code_logs, master_codes, "
                "dimension_company_logs, dimension_companies, "
                "dimension_brand_logs, dimension_brands, "
                "dimension_model_logs, dimension_models, "
                "dimension_category_logs, dimension_categories, "
                "dimension_year_logs, dimension_years, "
                "dimension_memory_logs, dimension_memories, "
                "dimension_network_logs, dimension_networks, "
                "dimension_country_logs, dimension_countries CASCADE"
            )
        )
    yield engine
    await engine.dispose()


def _audit(*, inline: bool):
    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(user_id=ACTOR_ID, role=UserRole.ADMIN),
        dimension_operation=DimensionOperation.CREATE if inline else None,
        master_code_operation=MasterCodeOperation.CREATE,
        reason="통합 생성",
    )


def _all_not_applicable() -> MasterCodeCreatePlan:
    return MasterCodeCreatePlan(
        company=NotApplicable(),
        brand=NotApplicable(),
        model=NotApplicable(),
        category=NotApplicable(),
        year=NotApplicable(),
        memory=NotApplicable(),
        network=NotApplicable(),
        country=NotApplicable(),
    )


def _all_inline() -> MasterCodeCreatePlan:
    return MasterCodeCreatePlan(
        company=InlineDimension(DimensionCode("COM"), CompanyValue("company")),
        brand=InlineDimension(DimensionCode("BRA"), BrandValue("brand")),
        model=InlineDimension(DimensionCode("MOD"), ModelValue("model")),
        category=InlineDimension(DimensionCode("CAT"), CategoryValue("category")),
        year=InlineDimension(DimensionCode("YR2026"), YearValue(2026)),
        memory=InlineDimension(
            DimensionCode("MEM128"), MemoryValue(amount=128, unit=MemoryUnit.GB)
        ),
        network=InlineDimension(DimensionCode("NET5"), NetworkGeneration.GENERATION_5),
        country=InlineDimension(DimensionCode("KOR"), CountryValue("korea")),
    )


def _company_only(code: str, value: str) -> MasterCodeCreatePlan:
    return MasterCodeCreatePlan(
        company=InlineDimension(DimensionCode(code), CompanyValue(value)),
        brand=NotApplicable(),
        model=NotApplicable(),
        category=NotApplicable(),
        year=NotApplicable(),
        memory=NotApplicable(),
        network=NotApplicable(),
        country=NotApplicable(),
    )


@pytest.mark.integration
async def test_create_all_inline_reads_one_aggregate_and_writes_shared_audit(
    master_code_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(master_code_engine))
    audit = _audit(inline=True)

    created = await repository.create(_all_inline(), audit)
    fetched = await repository.get_active(created.id)
    page = await repository.list_active(after=None, limit=50)

    async with master_code_engine.connect() as connection:
        master_log = (
            (
                await connection.execute(
                    text(
                        "SELECT change_set_id, master_code_version, operation, old_state, "
                        "new_state, reason, actor_kind, actor_role, actor_id, changed_at "
                        "FROM master_code_logs WHERE master_code_id = :id"
                    ),
                    {"id": created.id},
                )
            )
            .mappings()
            .one()
        )
        dimension_change_sets = (
            (
                await connection.execute(
                    text(
                        "SELECT change_set_id FROM dimension_company_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_brand_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_model_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_category_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_year_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_memory_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_network_logs "
                        "UNION ALL SELECT change_set_id FROM dimension_country_logs"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert fetched == created
    assert page.items == (created,)
    assert page.has_more is False
    assert created.code == "COM-BRA-MOD-CAT-YR2026-MEM128-NET5-KOR"
    assert master_log["change_set_id"] == audit.change_set_id
    assert master_log["master_code_version"] == 1
    assert master_log["operation"] == "CREATE"
    assert master_log["old_state"] is None
    assert master_log["new_state"]["code"] == created.code
    assert master_log["new_state"]["deleted"] is False
    assert master_log["reason"] == "통합 생성"
    assert master_log["actor_kind"] == "HUMAN"
    assert master_log["actor_role"] == "ADMIN"
    assert master_log["actor_id"] == str(ACTOR_ID)
    assert master_log["changed_at"] == created.updated_at
    assert len(dimension_change_sets) == 16
    assert set(dimension_change_sets) == {audit.change_set_id}
    for dimension in created.dimensions.ordered():
        assert dimension is not None
        assert dimension.created_at == created.created_at


@pytest.mark.integration
async def test_reference_validation_and_nulls_not_distinct_conflict(
    master_code_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(master_code_engine))

    with pytest.raises(InvalidDimensionReference) as invalid:
        await repository.create(
            MasterCodeCreatePlan(
                **{
                    **{
                        slot: NotApplicable()
                        for slot in (
                            "brand",
                            "model",
                            "category",
                            "year",
                            "memory",
                            "network",
                            "country",
                        )
                    },
                    "company": ExistingDimension(UUID("01890f7c-8abc-7def-8abc-000000000001")),
                }
            ),
            _audit(inline=False),
        )
    assert invalid.value.fields == ("dimensions.company.id",)

    first = await repository.create(_all_not_applicable(), _audit(inline=False))
    with pytest.raises(MasterCodeConflict):
        await repository.create(_all_not_applicable(), _audit(inline=False))
    assert first.etag().startswith('"mc-1-')


@pytest.mark.integration
async def test_zero_through_eight_dimension_references_round_trip(
    master_code_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(master_code_engine))
    all_inline = await repository.create(_all_inline(), _audit(inline=True))
    dimension_by_slot = dict(
        zip(MasterCodeDimensions.ORDER, all_inline.dimensions.ordered(), strict=True)
    )
    created_by_count = {8: all_inline}

    for count in range(8):
        plan = MasterCodeCreatePlan(
            **{
                slot: (
                    ExistingDimension(dimension_by_slot[slot].id)
                    if index < count
                    else NotApplicable()
                )
                for index, slot in enumerate(MasterCodeDimensions.ORDER)
            }
        )
        created_by_count[count] = await repository.create(plan, _audit(inline=False))

    for count, created in created_by_count.items():
        fetched = await repository.get_active(created.id)
        assert sum(item is not None for item in fetched.dimensions.ordered()) == count
        assert fetched == created


@pytest.mark.integration
@pytest.mark.parametrize(
    ("code", "value", "code_conflict", "value_conflict"),
    [
        ("COM", "other_company", True, False),
        ("OTHER", "company", False, True),
        ("COM", "company", True, True),
    ],
)
async def test_inline_dimension_conflicts_are_classified(
    master_code_engine: AsyncEngine,
    code: str,
    value: str,
    code_conflict: bool,
    value_conflict: bool,
) -> None:
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(master_code_engine))
    await repository.create(_company_only("COM", "company"), _audit(inline=True))

    with pytest.raises(InlineDimensionConflict) as conflict:
        await repository.create(_company_only(code, value), _audit(inline=True))

    assert conflict.value.field == "company"
    assert conflict.value.code is code_conflict
    assert conflict.value.value is value_conflict


@pytest.mark.integration
async def test_concurrent_identical_master_code_create_has_one_winner(
    master_code_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(master_code_engine)
    results = await asyncio.gather(
        SqlAlchemyMasterCodeRepository(sessions).create(
            _all_not_applicable(), _audit(inline=False)
        ),
        SqlAlchemyMasterCodeRepository(sessions).create(
            _all_not_applicable(), _audit(inline=False)
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, MasterCodeConflict) for result in results) == 1


@pytest.mark.integration
async def test_concurrent_exact_inline_dimension_duplicate_keeps_multiple_conflicts(
    master_code_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(master_code_engine)
    results = await asyncio.gather(
        SqlAlchemyMasterCodeRepository(sessions).create(
            _company_only("COM", "company"), _audit(inline=True)
        ),
        SqlAlchemyMasterCodeRepository(sessions).create(
            _company_only("COM", "company"), _audit(inline=True)
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, InlineDimensionConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].field == "company"
    assert conflicts[0].code is True
    assert conflicts[0].value is True

    async with master_code_engine.connect() as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM dimension_companies), "
                    "(SELECT count(*) FROM dimension_company_logs), "
                    "(SELECT count(*) FROM master_codes), "
                    "(SELECT count(*) FROM master_code_logs)"
                )
            )
        ).one()
    assert counts == (1, 2, 1, 1)


@pytest.mark.integration
async def test_cursor_pagination_matches_dimension_api_order_without_duplicates(
    master_code_engine: AsyncEngine,
) -> None:
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(master_code_engine))
    created = [
        await repository.create(
            _company_only(f"COM{index}", f"COMPANY_{index}"),
            _audit(inline=True),
        )
        for index in range(1, 4)
    ]

    first = await repository.list_active(after=None, limit=2)
    cursor = MasterCodeCursor(
        created_at=first.items[-1].created_at,
        id=first.items[-1].id,
    )
    second = await repository.list_active(after=cursor, limit=2)

    assert first.has_more is True
    assert second.has_more is False
    assert first.items == (created[2], created[1])
    assert second.items == (created[0],)
    assert not set(item.id for item in first.items) & set(item.id for item in second.items)


@pytest.mark.integration
async def test_master_code_requires_context_and_audit_is_append_only(
    master_code_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(master_code_engine)
    repository = SqlAlchemyMasterCodeRepository(sessions)
    created = await repository.create(_all_not_applicable(), _audit(inline=False))

    for statement in (
        "INSERT INTO master_codes (code) VALUES ('NNN-NNN-NNN-NNN-NNN-NNN-NNN-NN1')",
        "INSERT INTO master_code_logs ("
        "master_code_id, change_set_id, master_code_version, operation, old_state, new_state, "
        "actor_kind, actor_role, actor_id, changed_at"
        ") VALUES ("
        ":id, '01890f7c-8abc-7def-8abc-000000000099', 2, 'REFERENCE_UPDATE', "
        "'{}'::jsonb, '{}'::jsonb, 'HUMAN', 'ADMIN', 'malformed-state-test', "
        "statement_timestamp()"
        ")",
        "UPDATE master_code_logs SET reason = '변조' WHERE master_code_id = :id",
        "DELETE FROM master_code_logs WHERE master_code_id = :id",
        "DELETE FROM master_codes WHERE id = :id",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"id": created.id})
