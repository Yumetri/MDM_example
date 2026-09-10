from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CompanyValue,
    CountryValue,
    Dimension,
    DimensionCode,
    MemoryUnit,
    MemoryValue,
    ModelValue,
    NetworkGeneration,
    YearValue,
)
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions, MasterCodeValidationError

NOW = datetime(2026, 9, 10, 1, 23, 45, tzinfo=UTC)


def _dimension[ValueT](identifier: int, code: str, value: ValueT) -> Dimension[ValueT]:
    return Dimension(
        id=UUID(f"00000000-0000-7000-8000-{identifier:012d}"),
        code=DimensionCode(code),
        value=value,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )


def test_master_code_composes_in_the_single_canonical_order() -> None:
    dimensions = MasterCodeDimensions(
        company=_dimension(1, "COM", CompanyValue("company")),
        brand=_dimension(2, "BRA", BrandValue("brand")),
        model=_dimension(3, "MOD", ModelValue("model")),
        category=_dimension(4, "CAT", CategoryValue("category")),
        year=_dimension(5, "YR2026", YearValue(2026)),
        memory=_dimension(6, "MEM128", MemoryValue(amount=128, unit=MemoryUnit.GB)),
        network=_dimension(7, "NET5", NetworkGeneration.GENERATION_5),
        country=_dimension(8, "KOR", CountryValue("korea")),
    )

    master_code = MasterCode.create(
        id=UUID("00000000-0000-7000-8000-000000000009"),
        dimensions=dimensions,
        created_at=NOW,
    )

    assert master_code.code == "COM-BRA-MOD-CAT-YR2026-MEM128-NET5-KOR"
    assert master_code.version == 1
    assert master_code.created_at == master_code.updated_at == NOW
    assert master_code.deleted_at is None


def test_master_code_uses_nnn_for_every_not_applicable_slot() -> None:
    master_code = MasterCode.create(
        id=UUID("00000000-0000-7000-8000-000000000009"),
        dimensions=MasterCodeDimensions(),
        created_at=NOW,
    )

    assert master_code.code == "NNN-NNN-NNN-NNN-NNN-NNN-NNN-NNN"
    assert (
        master_code.etag()
        == '"mc-1-659320934c6987cb1e86f80832fe7248649203e6a3a1b12bb3517d97ff40642e"'
    )


def test_master_code_rejects_inactive_or_wrong_typed_dimensions() -> None:
    deleted = Dimension(
        id=UUID("00000000-0000-7000-8000-000000000001"),
        code=DimensionCode("COM"),
        value=CompanyValue("company"),
        version=2,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=NOW,
    )

    with pytest.raises(MasterCodeValidationError, match="active"):
        MasterCodeDimensions(company=deleted)
    with pytest.raises(MasterCodeValidationError, match="Company"):
        MasterCodeDimensions(company=_dimension(2, "BRA", BrandValue("brand")))


def test_master_code_etag_changes_when_a_nested_dimension_version_changes() -> None:
    first_company = _dimension(1, "COM", CompanyValue("company"))
    changed_company = Dimension(
        id=first_company.id,
        code=first_company.code,
        value=CompanyValue("renamed_company"),
        version=2,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )
    first = MasterCode.create(
        id=UUID("00000000-0000-7000-8000-000000000009"),
        dimensions=MasterCodeDimensions(company=first_company),
        created_at=NOW,
    )
    changed = MasterCode.create(
        id=first.id,
        dimensions=MasterCodeDimensions(company=changed_company),
        created_at=NOW,
    )

    assert first.code == changed.code
    assert first.version == changed.version == 1
    assert first.etag() != changed.etag()
