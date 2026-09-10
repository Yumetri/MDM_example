import pytest

from mdm.domain.dimensions import DimensionValidationError, MemoryUnit, MemoryValue


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
