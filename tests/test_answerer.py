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
