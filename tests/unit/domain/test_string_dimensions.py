import pytest

from mdm.domain.dimensions import (
    BrandValue,
    CategoryValue,
    CountryValue,
    DimensionValidationError,
    ModelValue,
)

STRING_VALUE_TYPES = (ModelValue, BrandValue, CountryValue, CategoryValue)


@pytest.mark.unit
@pytest.mark.parametrize("value_type", STRING_VALUE_TYPES)
def test_string_dimension_values_share_the_canonical_normalization(value_type: type) -> None:
    value = value_type("  galaxy__s24   ultra  ")

    assert value.value == "GALAXY_S24_ULTRA"


@pytest.mark.unit
@pytest.mark.parametrize("value_type", STRING_VALUE_TYPES)
@pytest.mark.parametrize(
    "raw",
    ["", "___", "GALAXY\tS24", "GALAXY-S24", "갤럭시", 2026],
)
def test_string_dimension_values_reject_noncanonical_inputs(value_type: type, raw: object) -> None:
    with pytest.raises(DimensionValidationError) as captured:
        value_type(raw)

    assert captured.value.field == "value"
