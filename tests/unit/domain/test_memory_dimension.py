from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.domain.dimensions import (
    Dimension,
    DimensionCode,
    DimensionValidationError,
    MemoryUnit,
    MemoryValue,
)

MEMORY_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")
CREATED_AT = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)
CHANGED_AT = datetime(2033, 5, 18, 3, 33, 21, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("amount", "raw_unit", "unit", "capacity_mb"),
    (
        (1, "MB", MemoryUnit.MB, 1),
        (128, " gb ", MemoryUnit.GB, 128_000),
        (1, "tb", MemoryUnit.TB, 1_000_000),
        (2_147_483_647, "PB", MemoryUnit.PB, 2_147_483_647_000_000_000),
    ),
)
def test_memory_value_normalizes_unit_and_derives_decimal_capacity(
    amount: int,
    raw_unit: str,
    unit: MemoryUnit,
    capacity_mb: int,
) -> None:
    value = MemoryValue.create(amount=amount, unit=raw_unit)

    assert value.amount == amount
    assert value.unit is unit
    assert value.capacity_mb == capacity_mb


@pytest.mark.unit
@pytest.mark.parametrize("amount", (0, -1, 2_147_483_648, True, 1.0, "1"))
def test_memory_value_rejects_non_positive_or_non_postgresql_integer_amount(amount: object) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        MemoryValue.create(amount=amount, unit="GB")  # type: ignore[arg-type]

    assert captured.value.field == "value.amount"


@pytest.mark.unit
@pytest.mark.parametrize("unit", ("GiB", "G B", "\tGB", "GB\n", "", True))
def test_memory_value_rejects_unknown_or_control_character_unit(unit: object) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        MemoryValue.create(amount=1, unit=unit)  # type: ignore[arg-type]

    assert captured.value.field == "value.unit"


@pytest.mark.unit
def test_memory_code_change_preserves_equivalent_stored_value_representation() -> None:
    current = Dimension(
        id=MEMORY_ID,
        code=DimensionCode("MEM1T"),
        value=MemoryValue(amount=1, unit=MemoryUnit.TB),
        version=1,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        deleted_at=None,
    )

    changed = current.change(
        code=DimensionCode("MEM1000G"),
        value=MemoryValue(amount=1000, unit=MemoryUnit.GB),
        changed_at=CHANGED_AT,
    )

    assert changed.code == DimensionCode("MEM1000G")
    assert changed.value is current.value
    assert changed.version == 2
