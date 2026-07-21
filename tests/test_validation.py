from __future__ import annotations

import pytest

from text2sql.errors import SQLSafetyError
from text2sql.validators import SQLGuard


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE metric_values",
        "SELECT * FROM read_csv_auto('secret.csv')",
        "SELECT * FROM metric_values; SELECT * FROM metrics",
        "SELECT * FROM unknown_table",
    ],
)
def test_sql_guard_rejects_unsafe_sql(sql):
    with pytest.raises(SQLSafetyError):
        SQLGuard().validate_and_limit(sql)


def test_sql_guard_adds_outer_limit():
    approved, _ = SQLGuard(default_limit=20, hard_limit=100).validate_and_limit(
        "SELECT org_id FROM organizations"
    )
    assert "LIMIT 20" in approved

