from __future__ import annotations

import pytest

from text2sql.errors import SQLSafetyError, ValidationError
from text2sql.models import QueryPlan
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


def test_alignment_ignores_irrelevant_range_candidates_for_point_condition(service):
    plan = QueryPlan(
        source="llm",
        query_type="joint_condition",
        operation="multi_condition",
        organizations=(),
        organization_scope="all",
        metrics=("ZB001", "ZB013"),
        current_date="2026-03-31",
        start_date="2026-01-01",
        end_date="2026-03-31",
        confidence=0.9,
    )
    sql = """
        SELECT org_id
        FROM metric_values
        WHERE data_date = DATE '2026-03-31'
          AND metric_id IN ('ZB001', 'ZB013')
    """
    expression = service.sql_guard.parse_one(sql)
    service.alignment_validator.validate(plan, sql, expression)


def test_alignment_still_requires_range_for_extrema(service):
    plan = QueryPlan(
        source="llm",
        query_type="period_extrema",
        operation="extrema",
        organizations=(),
        organization_scope="all",
        metrics=("ZB013",),
        current_date="2025-12-31",
        start_date="2025-01-01",
        end_date="2025-12-31",
        confidence=0.9,
    )
    sql = "SELECT org_id FROM metric_values WHERE data_date = DATE '2025-12-31' AND metric_id = 'ZB013'"
    expression = service.sql_guard.parse_one(sql)
    with pytest.raises(ValidationError, match="2025-01-01"):
        service.alignment_validator.validate(plan, sql, expression)
