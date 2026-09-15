from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mdm.domain.dimensions import (
    CompanyValue,
    Dimension,
    DimensionCode,
    DimensionValidationError,
)

pytestmark = pytest.mark.unit


def _dimension() -> Dimension[CompanyValue]:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    return Dimension(uuid4(), DimensionCode("SAM"), CompanyValue("Samsung"), 1, now, now, None)


@given(st.integers(min_value=0, max_value=86400), st.integers(min_value=0, max_value=86400))
def test_delete_restore_preserves_identity_and_values_and_increments_once(
    delete_delay: int,
    restore_delay: int,
) -> None:
    current = _dimension()
    deleted = current.delete(changed_at=current.updated_at + timedelta(seconds=delete_delay))
    restored = deleted.restore(changed_at=deleted.updated_at + timedelta(seconds=restore_delay))
    assert not current.is_deleted
    assert deleted.is_deleted and deleted.deleted_at == deleted.updated_at
    assert not restored.is_deleted
    assert (current.version, deleted.version, restored.version) == (1, 2, 3)
    for result in (deleted, restored):
        assert (result.id, result.code, result.value, result.created_at) == (
            current.id,
            current.code,
            current.value,
            current.created_at,
        )


def test_lifecycle_rejects_repeated_transitions_and_invalid_timestamps() -> None:
    current = _dimension()
    deleted = current.delete(changed_at=current.updated_at)
    for state, action in ((current, "restore"), (deleted, "delete")):
        with pytest.raises(DimensionValidationError):
            getattr(state, action)(changed_at=state.updated_at)
    for state, action in ((current, "delete"), (deleted, "restore")):
        for timestamp in (state.updated_at - timedelta(seconds=1), datetime(2026, 9, 15)):
            with pytest.raises(DimensionValidationError):
                getattr(state, action)(changed_at=timestamp)
