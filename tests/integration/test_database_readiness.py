import os

import pytest

from mdm.application.health import CheckReadiness
from mdm.infrastructure.database import SqlAlchemyDatabaseProbe, create_engine


@pytest.mark.integration
async def test_readiness_executes_against_postgresql() -> None:
    database_url = os.environ["MDM_DATABASE_URL"]
    engine = create_engine(database_url)
    try:
        await CheckReadiness(SqlAlchemyDatabaseProbe(engine)).execute()
    finally:
        await engine.dispose()
