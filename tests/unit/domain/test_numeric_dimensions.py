import pytest

from mdm.domain.dimensions import (
    DimensionValidationError,
    NetworkGeneration,
    YearValue,
)


@pytest.mark.unit
@pytest.mark.parametrize("raw", [2000, 2026, 2999])
def test_year_value_accepts_canonical_boundaries(raw: int) -> None:
    assert YearValue(raw).value == raw


@pytest.mark.unit
@pytest.mark.parametrize("raw", [1999, 3000, "2026", 2026.0, True, None])
def test_year_value_rejects_out_of_range_and_non_integer_inputs(raw: object) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        YearValue(raw)  # type: ignore[arg-type]

    assert captured.value.field == "value"


@pytest.mark.unit
@pytest.mark.parametrize("raw", [1, 2, 3, 4, 5])
def test_network_generation_accepts_only_canonical_members(raw: int) -> None:
    assert NetworkGeneration(raw).value == raw


@pytest.mark.unit
@pytest.mark.parametrize("raw", [0, 6, "5", "5G", 5.0, True, None])
def test_network_generation_rejects_out_of_range_and_non_integer_inputs(raw: object) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        NetworkGeneration(raw)

    assert captured.value.field == "value"
