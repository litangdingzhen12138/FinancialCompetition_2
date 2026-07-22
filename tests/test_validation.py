from __future__ import annotations

import pytest

from text2sql.errors import ResultValidationError, SQLSafetyError, ValidationError
from text2sql.models import QueryPlan, QueryResult
from text2sql.sql_compiler import compile_global_rank_sql
from text2sql.validators import ResultValidator, SQLGuard


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


def test_rank_alignment_accepts_window_rank_filter_as_top_n(service):
    plan = QueryPlan(
        source="llm",
        query_type="rank",
        operation="rank",
        organizations=(),
        organization_scope="all",
        metrics=("ZB002",),
        current_date="2025-05-31",
        sort_direction="asc",
        limit=3,
    )
    sql = """
        WITH ranked AS (
            SELECT org_id, metric_value,
                   ROW_NUMBER() OVER (ORDER BY metric_value ASC) AS metric_rank
            FROM metric_values
            WHERE data_date = DATE '2025-05-31' AND metric_id = 'ZB002'
        )
        SELECT org_id, metric_value, metric_rank
        FROM ranked
        WHERE metric_rank <= 3
        ORDER BY metric_rank
    """
    expression = service.sql_guard.parse_one(sql)
    service.alignment_validator.validate(plan, sql, expression)


def test_result_validator_rejects_metrics_outside_the_plan():
    plan = QueryPlan(
        source="llm",
        query_type="risk_metrics",
        operation="multi_condition",
        organizations=("ORG006",),
        organization_scope="selected",
        metrics=("ZB013", "ZB015", "ZB016", "ZB017"),
        current_date="2025-04-30",
    )
    result = QueryResult(
        columns=("org_id", "metric_id", "metric_value"),
        rows=(("ORG006", "ZB013", 1.32), ("ORG006", "ZB001", 104.26)),
    )
    with pytest.raises(ResultValidationError, match="计划外指标"):
        ResultValidator().validate(plan, result)


def test_deterministic_global_rank_compiler_handles_multi_metric_change(service):
    plan = QueryPlan(
        source="llm",
        query_type="rank_change",
        operation="multi_condition",
        organizations=("ORG011",),
        organization_scope="selected",
        metrics=("ZB001", "ZB002", "ZB013", "ZB011"),
        current_date="2026-04-30",
        comparison_date="2024-12-31",
        assumptions=("rank_population_all",),
    )
    sql = compile_global_rank_sql(plan)
    _, result = service._validated_execute(plan, sql)
    rows = {row[3]: row for row in result.rows}
    assert set(rows) == {"各项存款余额", "各项贷款余额", "不良贷款率", "净利润"}
    assert rows["净利润"][7:10] == (11, 9, 2)


def test_deterministic_global_rank_compiler_returns_only_requested_snapshot_metrics(service):
    plan = QueryPlan(
        source="llm",
        query_type="risk_metrics",
        operation="multi_condition",
        organizations=("ORG006",),
        organization_scope="selected",
        metrics=("ZB013", "ZB015", "ZB017", "ZB016"),
        current_date="2025-04-30",
        assumptions=("rank_population_all",),
    )
    sql = compile_global_rank_sql(plan)
    _, result = service._validated_execute(plan, sql)
    assert len(result.rows) == 4
    assert {row[2] for row in result.rows} == set(plan.metrics)
