from __future__ import annotations

import pytest

from text2sql.config import Settings
from text2sql.service import Text2SQLService


@pytest.fixture(autouse=True)
def allow_header_auth_for_tests(monkeypatch) -> None:
    """Production defaults to Bearer auth; tests may use explicit demo headers."""

    monkeypatch.setenv("TEXT2SQL_ALLOW_HEADER_AUTH", "true")


@pytest.fixture(scope="session")
def service() -> Text2SQLService:
    settings = Settings.from_env()
    return Text2SQLService(settings)
