from __future__ import annotations

import pytest

from text2sql.config import Settings
from text2sql.service import Text2SQLService


@pytest.fixture(scope="session")
def service() -> Text2SQLService:
    settings = Settings.from_env()
    return Text2SQLService(settings)

