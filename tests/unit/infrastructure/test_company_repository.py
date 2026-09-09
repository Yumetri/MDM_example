import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from mdm.application.dimensions import CompanyRepositoryUnavailable
from mdm.infrastructure.repositories.companies import SqlAlchemyCompanyRepository


class TimeoutSessionFactory:
    def __call__(self):
        raise SQLAlchemyTimeoutError("connection pool timed out")


@pytest.mark.unit
async def test_temporary_database_failure_is_translated_without_diagnostics() -> None:
    repository = SqlAlchemyCompanyRepository(TimeoutSessionFactory())  # type: ignore[arg-type]

    with pytest.raises(CompanyRepositoryUnavailable) as captured:
        await repository.list_active(after=None, limit=50)

    assert str(captured.value) == ""
