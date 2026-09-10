import pytest

from mdm.api.preconditions import parse_dimension_if_match, parse_dimension_if_match_values
from mdm.application.preconditions import InvalidIfMatch, PreconditionRequired


@pytest.mark.api
@pytest.mark.parametrize(("header", "version"), [('"1"', 1), ('"2147483647"', 2147483647)])
def test_parse_dimension_if_match_accepts_one_canonical_strong_version_tag(
    header: str, version: int
) -> None:
    assert parse_dimension_if_match(header) == version


@pytest.mark.api
def test_parse_dimension_if_match_requires_the_header() -> None:
    with pytest.raises(PreconditionRequired):
        parse_dimension_if_match(None)


@pytest.mark.api
def test_parse_dimension_if_match_rejects_repeated_header_lines() -> None:
    with pytest.raises(InvalidIfMatch):
        parse_dimension_if_match_values(['"1"', '"2"'])


@pytest.mark.api
@pytest.mark.parametrize(
    "header",
    [
        "*",
        'W/"1"',
        '"1", "2"',
        '"01"',
        '"0"',
        "1",
        ' "1"',
        '"2147483648"',
        f'"{"9" * 5000}"',
    ],
)
def test_parse_dimension_if_match_rejects_noncanonical_or_multiple_tags(header: str) -> None:
    with pytest.raises(InvalidIfMatch):
        parse_dimension_if_match(header)
