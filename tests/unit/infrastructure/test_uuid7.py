import pytest

from mdm.infrastructure.uuid7 import Uuid7Generator


@pytest.mark.unit
def test_uuid7_generator_sets_rfc_version_and_variant_bits() -> None:
    generator = Uuid7Generator(
        millisecond_clock=lambda: 1_725_000_000_123,
        random_bits=lambda bit_count: (1 << bit_count) - 1,
    )

    generated = generator.new()

    assert generated.version == 7
    assert generated.variant == "specified in RFC 4122"
    assert generated.int >> 80 == 1_725_000_000_123
