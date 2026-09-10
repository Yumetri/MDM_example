from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.domain.dimensions import (
    CompanyValue,
    Dimension,
    DimensionCode,
    DimensionValidationError,
)


def test_company_code_and_value_are_normalized_into_immutable_values() -> None:
    code = DimensionCode("sam01")
    value = CompanyValue("  Samsung   Electronics__Korea  ")

    assert code.value == "SAM01"
    assert value.value == "SAMSUNG_ELECTRONICS_KOREA"

    with pytest.raises(AttributeError):
        code.value = "OTHER"  # type: ignore[misc]


@pytest.mark.parametrize("raw", ["N", "nnn", " ABC ", "A_B", "A-B", "한글", "ß", ""])
def test_dimension_code_rejects_reserved_or_noncanonical_input(raw: str) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        DimensionCode(raw)

    assert captured.value.field == "code"


@pytest.mark.parametrize("raw", ["", "   ", "A\tB", "A\nB", "A+B", "한글", "straße", "A" * 129])
def test_company_value_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        CompanyValue(raw)

    assert captured.value.field == "value"


def test_dimension_requires_version_one_and_equal_creation_timestamps() -> None:
    timestamp = datetime(2033, 5, 18, tzinfo=UTC)
    dimension = Dimension(
        id=UUID("01890f7c-8abc-7def-8abc-0123456789ab"),
        code=DimensionCode("SAM"),
        value=CompanyValue("Samsung"),
        version=1,
        created_at=timestamp,
        updated_at=timestamp,
        deleted_at=None,
    )

    assert dimension.version == 1
    assert dimension.is_deleted is False

    with pytest.raises(DimensionValidationError):
        Dimension(
            id=dimension.id,
            code=dimension.code,
            value=dimension.value,
            version=0,
            created_at=timestamp,
            updated_at=timestamp,
            deleted_at=None,
        )
