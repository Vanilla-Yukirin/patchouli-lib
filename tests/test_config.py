import pytest
from pydantic import ValidationError

from patchouli_lib.config import Settings


def test_rejects_non_sqlite_database() -> None:
    with pytest.raises(ValidationError, match="supports SQLite only"):
        Settings.model_validate({"database_url": "postgresql://example.invalid/db"})
