from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mdm.application.audit_logs import AuditLogFilters, AuditLogPage, AuditLogQuery, ListAuditLogs
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionValidationError

pytestmark = pytest.mark.unit


class RecordingRepository:
    def __init__(self):
        self.calls = []

    async def list_logs(self, query, *, after, limit):
        self.calls.append((query, after, limit))
        return AuditLogPage((), False)


@pytest.mark.parametrize("role", list(UserRole))
async def test_audit_read_authorization_precedes_repository_access(role):
    repository = RecordingRepository()
    service = ListAuditLogs(repository, AuthorizationPolicy())
    principal = HumanPrincipal(UUID(int=1), role)
    query = AuditLogQuery(change_set_id=UUID(int=2))
    if role is UserRole.USER:
        with pytest.raises(AuthorizationDenied):
            await service.execute(principal, query, after=None, limit=50)
        assert repository.calls == []
    else:
        assert await service.execute(principal, query, after=None, limit=50) == AuditLogPage(
            (), False
        )
        assert repository.calls == [(query, None, 50)]


@pytest.mark.parametrize("end_offset", [0, -1])
def test_empty_or_reversed_time_interval_is_rejected(end_offset):
    start = datetime(2026, 9, 15, tzinfo=UTC)
    with pytest.raises(DimensionValidationError):
        AuditLogFilters(changed_from=start, changed_before=start + timedelta(seconds=end_offset))


def test_timezone_is_required_and_equivalent_offsets_are_normalized():
    with pytest.raises(DimensionValidationError):
        AuditLogFilters(changed_from=datetime(2026, 9, 15))
    offset = datetime.fromisoformat("2026-09-15T09:00:00+09:00")
    utc = datetime(2026, 9, 15, tzinfo=UTC)
    assert AuditLogFilters(changed_from=offset) == AuditLogFilters(changed_from=utc)


@pytest.mark.parametrize("timestamp", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"])
def test_unrepresentable_utc_boundary_is_a_validation_error(timestamp):
    with pytest.raises(DimensionValidationError):
        AuditLogFilters(changed_from=datetime.fromisoformat(timestamp))


def test_actor_id_with_nul_is_rejected_before_database_access():
    with pytest.raises(DimensionValidationError):
        AuditLogFilters(actor_id="invalid\x00actor")
