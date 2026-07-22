from __future__ import annotations

from text2sql.answerer import format_answer
from text2sql.models import QueryPlan, QueryResult


def test_generic_answer_uses_per_field_units_for_wide_multi_metric_result(service):
    plan = QueryPlan(
        source="llm",
        query_type="joint_condition",
        operation="multi_condition",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB001", "ZB013"),
        current_date="2026-03-31",
    )
    result = QueryResult(
        columns=("org_name", "deposit", "deposit_unit", "npl_rate", "npl_unit", "avg_npl_rate"),
        rows=(("江苏省C市农商行", 115.81, "亿元", 1.12, "%", 1.14),),
    )
    answer = format_answer(plan, result, service.catalog)
    assert "deposit=115.81亿元" in answer
    assert "npl_rate=1.12%" in answer
    assert "avg_npl_rate=1.14%" in answer
    assert "npl_rate=1.12亿元" not in answer


def test_generic_answer_preserves_small_nonzero_ratio_precision(service):
    plan = QueryPlan(
        source="llm",
        query_type="multi_calculation",
        operation="multi_condition",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
    )
    result = QueryResult(
        columns=("org_name", "result_value", "unit", "ratio_value", "ratio_unit"),
        rows=(("江苏省C市农商行", 7.9, "万元", 0.024173, "%"),),
    )

    answer = format_answer(plan, result, service.catalog)

    assert "result_value=7.9万元" in answer
    assert "ratio_value=0.0242%" in answer
    assert "ratio_value=0%" not in answer


def test_generic_answer_preserves_small_percentage_in_result_value(service):
    plan = QueryPlan(
        source="llm",
        query_type="multi_calculation",
        operation="multi_condition",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
    )
    result = QueryResult(
        columns=("org_name", "metric_name", "unit", "result_value"),
        rows=(("江苏省C市农商行", "净利润存款比", "%", 0.024173),),
    )

    answer = format_answer(plan, result, service.catalog)

    assert "result_value=0.0242%" in answer


def test_generic_multi_condition_answer_uses_business_labels(service):
    plan = QueryPlan(
        source="llm",
        query_type="joint_condition",
        operation="multi_condition",
        organizations=("ORG012",),
        organization_scope="selected",
        metrics=("ZB013", "ZB015", "ZB016", "ZB012"),
        current_date="2026-04-30",
    )
    result = QueryResult(
        columns=("metric_id", "metric_name", "unit", "metric_value", "average_value", "condition_met"),
        rows=(
            ("ZB013", "不良贷款率", "%", 0.85, 1.14, True),
            ("ZB015", "拨备覆盖率", "%", 197.87, 178.5, True),
            ("ZB016", "资本充足率", "%", 12.11, None, True),
            ("ZB012", "成本收入比", "%", 28.37, 32.11, True),
        ),
    )

    answer = format_answer(plan, result, service.catalog)

    assert "不良贷款率0.85%" in answer
    assert "综合判定：同时满足全部条件" in answer
    assert "condition_met" not in answer


def test_generic_multi_calculation_rows_are_rendered_as_natural_facts(service):
    plan = QueryPlan(
        source="llm",
        query_type="multi_calculation",
        operation="multi_condition",
        organizations=("ORG003",),
        organization_scope="selected",
        metrics=("ZB011", "ZB001"),
        current_date="2026-03-31",
    )
    result = QueryResult(
        columns=("org_name", "metric_name", "result_value", "unit"),
        rows=(
            ("江苏省C市农商行", "净利润回落额", 7.9, "万元"),
            ("江苏省C市农商行", "净利润/存款比", 0.024173, "%"),
        ),
    )

    answer = format_answer(plan, result, service.catalog)

    assert answer == "江苏省C市农商行：净利润回落额为7.9万元；净利润/存款比为0.0242%"


def test_wide_condition_answer_accepts_semantic_column_aliases(service):
    plan = QueryPlan(
        source="llm",
        query_type="joint_condition",
        operation="multi_condition",
        organizations=("ORG012",),
        organization_scope="selected",
        metrics=("ZB013", "ZB015", "ZB016", "ZB012"),
        current_date="2026-04-30",
    )
    result = QueryResult(
        columns=(
            "npl_rate", "npl_province_average", "coverage_value",
            "coverage_province_avg", "capital_adequacy_rate", "cost_income_ratio",
            "cost_income_province_avg", "meets_all_conditions",
        ),
        rows=((0.85, 1.14, 197.87, 178.5, 12.11, 28.37, 32.11, True),),
    )

    answer = format_answer(plan, result, service.catalog)

    assert "不良贷款率0.85%" in answer
    assert "成本收入比28.37%" in answer
    assert "综合判定：同时满足全部四项条件" in answer
