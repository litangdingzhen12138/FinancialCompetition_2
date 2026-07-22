from __future__ import annotations

import pytest

from text2sql.errors import QueryExecutionError, ResultValidationError, SQLSafetyError, ValidationError
from text2sql.llm_planner import SYSTEM_PROMPT
from text2sql.models import QueryPlan, QueryResult
from text2sql.sql_compiler import compile_global_rank_sql
from text2sql.validators import PlanValidator, ResultValidator, SQLGuard


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


@pytest.mark.parametrize("reserved_name", ["pivot", "unpivot"])
def test_sql_guard_rejects_unquoted_duckdb_reserved_cte_names(reserved_name):
    sql = f"WITH {reserved_name} AS (SELECT 1 AS value) SELECT value FROM {reserved_name}"
    with pytest.raises(SQLSafetyError, match=reserved_name):
        SQLGuard().validate_and_limit(sql)


def test_sql_guard_allows_quoted_duckdb_reserved_cte_name():
    approved, _ = SQLGuard().validate_and_limit(
        'WITH "pivot" AS (SELECT 1 AS value) SELECT value FROM "pivot"'
    )
    assert '"pivot"' in approved


def test_duckdb_parser_feedback_keeps_the_failing_token(service):
    with pytest.raises(QueryExecutionError) as error:
        service.executor.preflight(
            "WITH pivot AS (SELECT 1 AS value) SELECT value FROM pivot"
        )
    feedback = error.value.retry_feedback or ""
    assert "ParserException" in feedback
    assert "pivot" in feedback
    assert "保留字" in feedback


def test_llm_prompt_forbids_unquoted_duckdb_reserved_aliases():
    assert "pivot、unpivot" in SYSTEM_PROMPT
    assert "DuckDB保留字" in SYSTEM_PROMPT


def test_llm_prompt_and_catalog_contain_the_complete_derived_dimension_contract(service):
    expected_rules = {
        "较年初": "当日值 - 2024年12月31日值",
        "较上季": "当日值 - 上季度末值",
        "较上月": "当日值 - 上月月末值",
        "较同期": "当日值 - 去年同期值",
        "全省均值": "13家机构当日值的算数平均值",
        "排名": "不良贷款率/逾期贷款率/成本收入比按从低到高排名（越低越好），其余按从高到低排名，使用rank算法",
        "增量": "当日值 - 比较日值",
        "增幅": "(当日值-比较日值)/比较日值×100%，比率类指标（ZB012/013/015/016/017）不做增幅计算",
        "表现较好": "前三",
        "表现较差": "后四",
    }
    assert all(f"- {name}: {description}" in SYSTEM_PROMPT for name, description in expected_rules.items())
    assert service.catalog.rules == expected_rules
    schema_context = service.catalog.schema_context("")
    assert all(f"- {name}: {description}" in schema_context for name, description in expected_rules.items())


@pytest.mark.parametrize(
    ("metric", "wrong_direction"),
    [("ZB012", "desc"), ("ZB013", "desc"), ("ZB017", "desc"), ("ZB001", "asc")],
)
def test_plan_validator_rejects_ranking_direction_that_violates_the_contract(
    service, metric, wrong_direction
):
    plan = QueryPlan(
        source="llm",
        query_type="rank",
        operation="rank",
        organizations=(),
        organization_scope="all",
        metrics=(metric,),
        current_date="2025-05-31",
        sort_direction=wrong_direction,
    )

    with pytest.raises(ValidationError, match="排名方向违反衍生维度说明"):
        PlanValidator(service.catalog).validate(plan)


def test_llm_plan_rejects_dynamic_previous_year_for_fixed_year_beginning(service):
    plan = QueryPlan(
        source="llm",
        query_type="difference",
        operation="difference",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB001",),
        current_date="2029-08-31",
        comparison_date="2028-12-31",
    )

    with pytest.raises(ValidationError, match="应为2024-12-31"):
        service._augment_llm_plan("较年初增加了多少？", plan)


def test_province_average_result_is_checked_against_all_thirteen_banks(service):
    plan = QueryPlan(
        source="llm",
        query_type="province_average",
        operation="province_average_compare",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB013",),
        current_date="2026-04-30",
    )
    result = QueryResult(
        columns=("org_id", "metric_id", "province_average"),
        rows=(("ORG001", "ZB013", 999.0),),
    )

    with pytest.raises(ResultValidationError, match="13家机构算数平均值"):
        service._validate_province_average_semantics(plan, result)


def test_result_validator_enforces_top_three_and_bottom_four_labels():
    plan = QueryPlan(
        source="llm",
        query_type="profile",
        operation="profile",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB016",),
        current_date="2026-04-30",
    )
    result = QueryResult(
        columns=("metric_rank", "performance_label"),
        rows=((1, "中性"),),
    )

    with pytest.raises(ResultValidationError, match="前三/后四"):
        ResultValidator().validate(plan, result)


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


def test_rank_alignment_requires_rank_algorithm_for_top_n(service):
    plan = QueryPlan(
        source="llm",
        query_type="rank",
        operation="rank",
        organizations=(),
        organization_scope="all",
        metrics=("ZB013",),
        current_date="2025-05-31",
        sort_direction="asc",
        limit=3,
    )
    sql = """
        WITH ranked AS (
            SELECT org_id, metric_value,
                   ROW_NUMBER() OVER (ORDER BY metric_value ASC) AS metric_rank
            FROM metric_values
            WHERE data_date = DATE '2025-05-31' AND metric_id = 'ZB013'
        )
        SELECT org_id, metric_value, metric_rank
        FROM ranked
        WHERE metric_rank <= 3
        ORDER BY metric_rank
    """
    expression = service.sql_guard.parse_one(sql)
    with pytest.raises(ValidationError, match="RANK算法"):
        service.alignment_validator.validate(plan, sql, expression)


def test_ratio_alignment_accepts_case_based_zero_guard(service):
    plan = QueryPlan(
        source="llm",
        query_type="ratio",
        operation="ratio",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
        derived_formula="profit_deposit_ratio",
    )
    sql = """
        WITH components AS (
            SELECT
                MAX(CASE WHEN metric_id = 'ZB011' THEN metric_value END) AS numerator,
                MAX(CASE WHEN metric_id = 'ZB001' THEN metric_value END) AS denominator
            FROM metric_values
            WHERE data_date = DATE '2026-03-31'
              AND org_id = 'ORG003'
              AND metric_id IN ('ZB011', 'ZB001')
        )
        SELECT CASE
                   WHEN denominator IS NULL OR denominator = 0 THEN NULL
                   ELSE numerator / denominator
               END AS derived_value
        FROM components
    """
    expression = service.sql_guard.parse_one(sql)

    service.alignment_validator.validate(plan, sql, expression)


def test_ratio_alignment_rejects_unguarded_division(service):
    plan = QueryPlan(
        source="llm",
        query_type="ratio",
        operation="ratio",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
        derived_formula="profit_deposit_ratio",
    )
    sql = """
        SELECT
            MAX(CASE WHEN metric_id = 'ZB011' THEN metric_value END)
            / MAX(CASE WHEN metric_id = 'ZB001' THEN metric_value END) AS derived_value
        FROM metric_values
        WHERE data_date = DATE '2026-03-31'
          AND org_id = 'ORG003'
          AND metric_id IN ('ZB011', 'ZB001')
    """
    expression = service.sql_guard.parse_one(sql)

    with pytest.raises(ValidationError, match="分母为零保护"):
        service.alignment_validator.validate(plan, sql, expression)


def test_derived_output_ids_are_not_mistaken_for_unrequested_base_metrics():
    plan = QueryPlan(
        source="llm",
        query_type="ratio",
        operation="ratio",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
        derived_formula="profit_deposit_ratio",
    )
    result = QueryResult(
        columns=("metric_id", "result_value"),
        rows=(("profit_drop", 7.9), ("profit_deposit_ratio", 0.0242)),
    )

    ResultValidator().validate(plan, result)


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
