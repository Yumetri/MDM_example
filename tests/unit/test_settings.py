import pytest
from pydantic import ValidationError

from mdm.infrastructure.settings import Settings


@pytest.mark.unit
def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDM_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.unit
def test_database_url_is_hidden_from_repr() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost:5432/mdm",
        _env_file=None,
    )

    assert "secret" not in repr(settings)
