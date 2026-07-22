from __future__ import annotations

from dataclasses import replace

import pytest

from text2sql.config import Settings
from text2sql.errors import PlanningError
from text2sql.models import QueryPlan
from text2sql.service import Text2SQLService


class FakeLLMPlanner:
    def plan(self, **kwargs):
        return QueryPlan(
            source="llm",
            query_type="point_query",
            operation="value",
            organizations=("ORG001",),
            organization_scope="selected",
            metrics=("ZB001",),
            current_date="2025-06-15",
            expected_shape="single_row",
            confidence=0.9,
            sql="""
                SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value
                FROM metric_values v
                JOIN organizations o ON o.org_id = v.org_id
                JOIN metrics m ON m.metric_id = v.metric_id
                WHERE v.data_date = DATE '2025-06-15'
                  AND v.metric_id = 'ZB001'
                  AND v.org_id = 'ORG001'
            """,
        )


def test_llm_fallback_uses_the_same_validation_path():
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=FakeLLMPlanner())
    response = service.ask("请处理一个规则无法识别的开放问题", "fake-llm")
    assert response.route == "llm"
    assert response.answer == "江苏省A市农商行：42.02亿元"


class FailingLLMPlanner:
    def plan(self, **kwargs):
        raise PlanningError("invalid generated plan")


def test_service_attaches_intermediate_diagnostics_to_final_error():
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=FailingLLMPlanner())

    with pytest.raises(PlanningError) as caught:
        service.ask("规则无法识别的开放问题", "diagnostics")

    stages = [event["stage"] for event in caught.value.diagnostics]
    assert "rule_decision" in stages
    assert "llm_attempt" in stages
    assert "llm_attempt_error" in stages
    assert stages[-1] == "failed"


class ReservedAliasRetryPlanner:
    def __init__(self) -> None:
        self.feedbacks: list[str | None] = []

    def plan(self, **kwargs):
        self.feedbacks.append(kwargs.get("feedback"))
        cte_name = "pivot" if len(self.feedbacks) == 1 else "pv"
        return QueryPlan(
            source="llm",
            query_type="point_query",
            operation="value",
            organizations=("ORG001",),
            organization_scope="selected",
            metrics=("ZB001",),
            current_date="2025-06-15",
            expected_shape="single_row",
            confidence=0.9,
            sql=f"""
                WITH {cte_name} AS (
                    SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value
                    FROM metric_values v
                    JOIN organizations o ON o.org_id = v.org_id
                    JOIN metrics m ON m.metric_id = v.metric_id
                    WHERE v.data_date = DATE '2025-06-15'
                      AND v.metric_id = 'ZB001'
                      AND v.org_id = 'ORG001'
                )
                SELECT org_id, org_name, metric_name, unit, metric_value FROM {cte_name}
            """,
        )


def test_reserved_alias_guard_feedback_makes_the_llm_retry_actionable():
    planner = ReservedAliasRetryPlanner()
    settings = replace(Settings.from_env(), llm_retries=1)
    service = Text2SQLService(settings=settings, llm_planner=planner)

    response = service.ask("请处理一个规则无法识别的开放问题", "reserved-alias-retry")

    assert response.route == "llm"
    assert response.answer == "江苏省A市农商行：42.02亿元"
    assert len(planner.feedbacks) == 2
    assert "pivot" in (planner.feedbacks[1] or "")
    assert "保留字" in (planner.feedbacks[1] or "")
