from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mdm.domain.dimensions import CompanyValue, Dimension, DimensionCode
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions, MasterCodeValidationError

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _active():
    company = Dimension(uuid4(), DimensionCode("A1"), CompanyValue("A"), 1, NOW, NOW, None)
    return MasterCode.create(
        id=uuid4(), dimensions=MasterCodeDimensions(company=company), created_at=NOW
    )


def test_delete_and_restore_preserve_references_and_increment_version_once():
    active = _active()
    deleted = active.delete(changed_at=NOW + timedelta(seconds=1))
    assert deleted.dimensions == active.dimensions
    assert deleted.code == active.code
    assert deleted.created_at == active.created_at
    assert deleted.version == 2
    assert deleted.deleted_at == deleted.updated_at
    assert deleted.etag() != active.etag()
    restored = deleted.restore(changed_at=NOW + timedelta(seconds=2))
    assert restored.dimensions == active.dimensions
    assert restored.code == active.code
    assert restored.deleted_at is None
    assert restored.version == 3
    assert restored.etag() != deleted.etag()


def test_lifecycle_rejects_wrong_states_inactive_references_and_invalid_timestamps():
    active = _active()
    with pytest.raises(MasterCodeValidationError):
        active.restore(changed_at=NOW)
    deleted = active.delete(changed_at=NOW)
    with pytest.raises(MasterCodeValidationError):
        deleted.delete(changed_at=NOW)
    company = active.dimensions.company
    assert company is not None
    inactive = replace(
        deleted, dimensions=MasterCodeDimensions(company=company.delete(changed_at=NOW))
    )
    with pytest.raises(MasterCodeValidationError):
        inactive.restore(changed_at=NOW)
    for timestamp in (NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)):
        with pytest.raises(MasterCodeValidationError):
            active.delete(changed_at=timestamp)
        with pytest.raises(MasterCodeValidationError):
            deleted.restore(changed_at=timestamp)
