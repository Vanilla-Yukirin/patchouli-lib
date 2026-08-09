from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from patchouli_lib.app import create_app
from patchouli_lib.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = (tmp_path / "test.db").as_posix()
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{database_path}",
        }
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
